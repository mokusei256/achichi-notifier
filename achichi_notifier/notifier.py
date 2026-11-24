"""One check cycle: fetch YouTube, diff against shared state, post to Discord."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from . import control
from . import destinations as dests
from . import render
from .config import Config, ConfigError
from .discord_api import DiscordClient, DiscordError, mark_preview
from .state import (BACKLOG_PREFIX, DiscordStateStore, FileStateStore, State, as_int, bump_reg, next_reg,
                    now_iso, parse_time)
from .thumbnails import ThumbnailWatch
from .youtube import (
    VIDEO_TABS, ChannelMeta, Post, PostsPage, VideoInfo, VideoRef, YouTubeClient, YouTubeError,
    approx_age_hours,
)

log = logging.getLogger(__name__)

ROLES = ("primary", "fallback")
FEATURES = ("destinations", "preview_mark", "pause", "live_start")
COOKIE_ALERT_INTERVAL = timedelta(hours=24)
# Consecutive failed members-only checks before the state channel is told (~30 min on the
# Windows primary, ~1h on the Mac). Covers temporary page problems and Firefox-profile reads
# that fail while someone is logging in again.
FETCH_FAIL_ALERT_STREAK = 6
STREAK_STATUSES = ("fetch_failed", "read_failed")
BROWSER_REFRESH_ALERT_FAILURES = 3
# Stream start notices (Notifier.check_live_starts). A tracked slot's watch page is checked
# every run from LIVE_START_LEAD before its start time; if the stream has not begun
# LIVE_WAIT after it, only a "live" badge on the channel tab makes the bot look again.
LIVE_START_LEAD = timedelta(minutes=10)
LIVE_WAIT = timedelta(hours=12)
# Watch pages looked at per run for this channel's slots, and as many again for `watch` streams.
LIVE_CHECKS_PER_RUN = 5
# A stream registered with `watch` is also looked at this often before its window, so a start
# moved earlier (or a host going live early) is noticed: it has no tab badge to go by.
WATCH_RECHECK = timedelta(minutes=30)
# A watched stream whose page cannot be read (deleted, made private) is given up once it is
# LIVE_WAIT past its start and has stayed unreadable this long (not on one failed fetch).
WATCH_UNREADABLE = timedelta(hours=1)
RUN_SLACK = timedelta(minutes=1)  # runs start a few seconds apart from the 5-minute grid
LIVE_FINAL = ("started", "ended", "excluded", "not_live", "removed")
START_HEADLINE = render.HEADLINES["live_now"]


@dataclass
class Notification:
    kind: str  # key of render.HEADLINES
    item_type: str  # "video" | "post" | "live_start" (a slot announced earlier went on air) | "thumbnail"
    item_id: str  # for a thumbnail: "<video id>@<its new version>"
    sort_key: datetime
    payload: dict
    files: list = field(default_factory=list)  # (name, bytes) uploaded with the payload
    retry_payload: dict | None = None  # filed for per-destination retries (they carry no files)

    @property
    def group(self) -> str:
        return render.GROUPS[self.kind]

    @property
    def url(self) -> str | None:
        embeds = self.payload.get("embeds") or [{}]
        return embeds[0].get("url")


@dataclass
class CollectResult:
    notifications: list[Notification] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Source problems that left nothing uncovered: logged, but the run still counts as a success
    # (so they never make the fallback take over or raise the "primary is down" alert).
    tolerated: list[str] = field(default_factory=list)
    meta: ChannelMeta | None = None
    # Members-only check outcome. Cookie problems never count as errors: they must not
    # stop video/public-post notifications or make the fallback take over everything.
    member_status: str | None = None  # None | ok | unreadable | read_failed | logged_out | not_member | fetch_failed
    member_detail: str | None = None
    cookie_source: str | None = None  # firefox | file | env
    thumb_problem: str | None = None  # thumbnail checks need a look (see _thumb_alert)
    queued: set = field(default_factory=set)

    def queue(self, note: Notification) -> None:
        key = (note.item_type, note.item_id)
        if key not in self.queued:  # one notification per item per run
            self.queued.add(key)
            self.notifications.append(note)


def make_store(cfg: Config, discord: DiscordClient | None):
    if cfg.state_backend == "discord":
        if discord is None or not cfg.state_channel_id:
            raise ValueError("discord state backend needs a Discord client and DISCORD_STATE_CHANNEL_ID")
        return DiscordStateStore(discord, cfg.state_channel_id)
    return FileStateStore(cfg.state_file)


def comparable_public_page(public_page: PostsPage | None,
                           member_page: PostsPage | None) -> PostsPage | None:
    """The logged-out page can tell members-only posts apart only if it is a real logged-out
    post list. An empty list proves nothing while the member sees unbadged posts."""
    if public_page is None or not public_page.valid or public_page.logged_in:
        return None
    if (not public_page.posts and member_page is not None
            and any(not p.members_only for p in member_page.posts)):
        return None
    return public_page


def member_page_problem(member_page: PostsPage, public_page: PostsPage | None) -> str | None:
    """Reasons not to trust the member's view this run (checked on real pages: every post a
    logged-out viewer sees also appears in the member's view)."""
    if not member_page.valid or (not member_page.posts and not member_page.locked_without_id):
        return "member view had no post list"
    if public_page is not None:
        member_ids = {p.post_id for p in member_page.posts}
        if any(p.post_id not in member_ids for p in public_page.posts):
            return "member view is missing posts that logged-out viewers see"
    return None


def classify_member_posts(member_page: PostsPage,
                          public_page: PostsPage | None) -> tuple[list[Post], list[Post]]:
    """Split the member's view into (badge-marked members-only posts, posts that are
    members-only only by comparison with the logged-out page).

    public_page must come from comparable_public_page(); without it only badge-marked posts
    are reported. Badge-marked posts win even if logged-out viewers see them (fail-closed).
    Every returned post has members_only=True so its content is never rendered.
    """
    public_ids = {p.post_id for p in public_page.posts} if public_page is not None else None
    badge: list[Post] = []
    by_comparison: list[Post] = []
    for post in member_page.posts:
        if post.members_only:
            badge.append(post)
        elif public_ids is not None and post.post_id not in public_ids:
            by_comparison.append(replace(post, members_only=True))
    if public_ids is not None and any(p.post_id in public_ids for p in badge):
        log.warning("a badge-marked members-only post is also visible logged out; announcing it link-only")
    if by_comparison:
        log.warning("%d post(s) hidden from logged-out viewers carry no members-only badge; "
                    "they are announced after a second confirming check", len(by_comparison))
    return badge, by_comparison


@lru_cache(maxsize=8)
def _title_pattern(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


def excluded_stream(cfg: Config, kind: str, title: str | None) -> bool:
    """True for a stream (slot) whose title matches EXCLUDE_LIVE_TITLES, e.g. a free chat
    room. Only stream kinds are filtered: a video that merely mentions "free chat" still counts.
    The title is tried as-is (so text pasted from a title keeps matching) and NFKC-normalised
    (so an ASCII / full-width-kana pattern also covers half-width kana and full-width letters).
    The pattern itself is never normalised: that would turn （）［］？ into regex syntax."""
    if not cfg.exclude_live_titles or not title or render.GROUPS.get(kind) != "live":
        return False
    pattern = _title_pattern(cfg.exclude_live_titles)
    return bool(pattern.search(title) or pattern.search(unicodedata.normalize("NFKC", title)))


def _wait_live(state: State, video_id: str, scheduled: datetime | None) -> None:
    old = state.live.get(video_id, {})
    entry = {**old,  # keeps e.g. "external" (watch CLI)
             "scheduled": scheduled.isoformat() if scheduled else None, "done": None, "at": now_iso()}
    if old.get("external") and old.get("done") in ("stale", "unreadable"):
        # A `watch` entry the bot gave up on comes back (e.g. listed here with a new start
        # time): a newer "reg" keeps the stored given-up copy from winning the merge ("started"
        # still wins). A removal by the user is not undone this way.
        entry["reg"] = next_reg(old)
    state.live[video_id] = entry


def _adopt_removed(state: State, video_id: str) -> dict:
    """A `watch` entry the user removed whose stream is listed in this channel's tabs: a plain
    entry of this channel from now on (looked at like any of its slots while listed, not watched
    once unlisted). The smallest "reg" step makes what is learned from now on win over the stored
    removed copy on merge, but not over a `watch` / `watch --remove` saved meanwhile (next_reg).
    (One the bot gave up on is looked at like a slot of this channel while listed, and a readable
    look makes it a waiting `watch` again: _wait_live.)"""
    old = state.live[video_id]
    entry = {k: v for k, v in old.items() if k not in ("external", "checked", "unreadable_since")}
    entry.update(done=None, reg=bump_reg(old))
    state.live[video_id] = entry
    return entry


def _waiting_since(entry: dict, scheduled: datetime | None) -> datetime | None:
    """When the wait for a start began to count, for giving up after LIVE_WAIT: the start time,
    or for a `watch` entry its registration if that is later (registering again restarts it)."""
    registered = parse_time(entry.get("reg")) if entry.get("external") else None
    times = [t for t in (scheduled, registered) if t]
    return max(times) if times else None


def _settle_live(state: State, video_id: str, why: str) -> None:
    state.live[video_id] = {**state.live.get(video_id, {}), "done": why, "at": now_iso()}


def _mark_handled(state: State, note: Notification) -> None:
    """The item is announced (or deliberately not): never queue it again. Only here is a start
    recorded as announced, so a notice that reached nobody is tried again next run."""
    if note.item_type == "live_start":
        _settle_live(state, note.item_id, "started")
        return
    if note.item_type == "thumbnail":
        video_id, _, version = note.item_id.partition("@")
        entry = state.thumbs.get(video_id)
        if entry is not None:  # versions only grow: never announced again (State.merge_from)
            entry["v"] = max(as_int(entry.get("v")), as_int(version))
            entry["seen"] = max(as_int(entry.get("seen")), entry["v"])
        return
    (state.videos if note.item_type == "video" else state.posts)[note.item_id] = now_iso()
    state.pending.pop(note.item_id, None)
    if note.kind == "live_now":  # first seen on air: this notice says it began
        _settle_live(state, note.item_id, "started")


def _guard_headline(kind: str | None) -> str | None:
    """A "stream started" notice shares its link with the slot notice, so the "already
    posted?" check also looks for its headline (also for a stream first seen on air: its slot
    notice may have reached Discord although the send looked failed). Everything else is
    matched by link alone."""
    return START_HEADLINE if kind in ("live_now", "collab_live") else None


def _merge_refs(groups: list[list[VideoRef]]) -> list[VideoRef]:
    merged: dict[str, VideoRef] = {}
    for refs in groups:
        for ref in refs:
            if ref.video_id in merged:
                merged[ref.video_id].merge(ref)
            else:
                merged[ref.video_id] = ref
    return list(merged.values())


class Notifier:
    def __init__(self, cfg: Config, yt: YouTubeClient, now: datetime | None = None):
        self.cfg = cfg
        self.yt = yt
        self.now = now or datetime.now(timezone.utc)
        self.max_age = timedelta(hours=cfg.max_age_hours)
        self.listed: set[str] = set()  # video ids in this run's channel tabs / RSS
        self.listing_complete = False  # every tab answered with videos this run
        self.refs: list[VideoRef] = []  # this run's listing (for check_thumbnails)
        self.dry_run = False

    # ------------------------------------------------------------------ videos

    def collect_video_refs(self, result: CollectResult) -> list[VideoRef]:
        groups: list[list[VideoRef]] = []
        rss_error = None
        try:
            refs, _ = self.yt.fetch_rss()
            groups.append(refs)
        except YouTubeError as exc:
            rss_error = f"rss: {exc}"
        covered = True  # every tab answered with a non-empty list
        for tab in VIDEO_TABS:
            try:
                refs, meta = self.yt.fetch_tab(tab)
                groups.append(refs)
                result.meta = result.meta or meta
                if not refs:
                    covered = False  # an empty list proves nothing (layout change, odd variant)
            except YouTubeError as exc:
                result.errors.append(f"{tab}: {exc}")
                covered = False
        self.listing_complete = covered
        if rss_error:
            # The feed answers HTTP 404 now and then. The /videos, /shorts and /streams tabs
            # list everything it does, so it only counts as an error when they did not all
            # come back with videos.
            (result.tolerated if covered else result.errors).append(rss_error)
        return _merge_refs(groups)

    @staticmethod
    def news_time(info: VideoInfo) -> datetime | None:
        """When the item became worth announcing, for the max-age check (None: always new).
        A stream's publish date is when its slot was created, which can be days before the slot
        is made public: a slot created on 11-08, made public on 11-17 for an 11-18 stream, was
        once skipped as "10 days old". So streams and premieres go by their stream time."""
        if info.kind == "live_now":
            return None  # on air right now
        if info.kind in ("live_upcoming", "premiere"):
            return info.scheduled_start  # in the future for a real slot; long past for a stale one
        if info.kind == "live_archive":
            return info.ended or info.published
        return info.published

    def check_videos(self, state: State, result: CollectResult) -> None:
        refs = self.refs = self.collect_video_refs(result)
        if not refs:
            if "videos" in state.seeded:
                self.check_live_starts(state, [], result)  # streams registered with `watch` still
            return
        if "videos" not in state.seeded:
            for ref in refs:
                state.videos.setdefault(ref.video_id, now_iso())
                if ref.live_hint == "live":  # on air before the bot knew the channel
                    state.live.setdefault(ref.video_id, {"scheduled": None, "done": "started",
                                                         "at": now_iso()})
            state.seeded["videos"] = now_iso()
            log.info("initial run: marked %d existing videos as seen (no notifications)", len(refs))
            return
        new = [r for r in refs if r.video_id not in state.videos]
        if len(new) > self.cfg.max_new_per_run:
            log.warning("%d new videos; handling %d this run", len(new), self.cfg.max_new_per_run)
            new = new[: self.cfg.max_new_per_run]
        for ref in new:
            info = self.yt.fetch_video_info(ref)
            entry = state.live.get(ref.video_id) or {}
            if entry.get("external") and entry.get("done") == "removed":
                entry = _adopt_removed(state, ref.video_id)  # first listed here
            if entry.get("external"):
                entry["checked"] = self.now.isoformat()  # its watch page was just looked at
            if render.GROUPS.get(info.kind) == "live" and entry.get("external") and entry.get("done") == "started":
                # Its start already went out through `watch`, and now it shows up in this
                # channel's tabs (on air, as an archive, or with a stale "upcoming" badge).
                log.info("stream %s was already announced as started; marking seen", ref.video_id)
                state.videos[ref.video_id] = now_iso()
                continue
            if excluded_stream(self.cfg, info.kind, info.title):
                log.info("stream %s is excluded by EXCLUDE_LIVE_TITLES (%s); marking seen without notifying",
                         ref.video_id, info.title)
                state.videos[ref.video_id] = now_iso()
                _settle_live(state, ref.video_id, "excluded")
                continue
            news_time = self.news_time(info)
            if news_time and self.now - news_time > self.max_age:
                log.info("%s %s is old (%s); marking seen without notifying",
                         info.kind, ref.video_id, news_time.date())
                state.videos[ref.video_id] = now_iso()
                if render.GROUPS.get(info.kind) == "live":
                    _settle_live(state, ref.video_id, "ended" if info.kind == "live_archive" else "stale")
                continue
            log.info("new video %s kind=%s members_only=%s", ref.video_id, info.kind, info.members_only)
            result.queue(Notification(
                kind=info.kind,
                item_type="video",
                item_id=ref.video_id,
                sort_key=info.published or self.now,
                payload=render.video_payload(info, result.meta, None),  # mentions are per destination
            ))
            if info.kind == "live_upcoming":
                _wait_live(state, ref.video_id, info.scheduled_start)  # announce its start later
            elif info.kind == "live_now" and entry.get("external"):
                # Keep a `watch` entry due every run until this notice is delivered, also if the
                # tabs stop listing it. It is settled as "started" once delivered (_mark_handled).
                _wait_live(state, ref.video_id, info.started or self.now)
        self.check_live_starts(state, refs, result)

    def check_live_starts(self, state: State, refs: list[VideoRef], result: CollectResult) -> None:
        """Announce a stream when it goes on air, also when its slot was announced earlier
        (the first sighting gives one notice per video id; this is the second one).

        Looked at again (on its watch page, which is what decides): a seen stream the channel
        tab marks as live, and a tracked slot from LIVE_START_LEAD before its start time on, so
        a missing tab badge never loses the start. A stream found already over gets nothing.
        Streams registered with `watch` are handled by _check_watched, except that this
        channel's tabs count for them too when they are listed here (they are then looked at
        here, with this channel's slots). Up to LIVE_CHECKS_PER_RUN pages are looked at here per
        run, and as many again by _check_watched, so this channel's slots never use up the looks
        for streams elsewhere."""
        checks, looked = 0, set()
        self.listed = {ref.video_id for ref in refs}
        for ref in refs:
            video_id = ref.video_id
            if video_id not in state.videos:
                continue  # new this run: check_videos announces it (or it waits for a retry)
            entry = state.live.get(video_id) or {}
            if entry.get("external") and entry.get("done") == "removed":
                entry = _adopt_removed(state, video_id)
            done = entry.get("done")
            if entry.get("external"):
                if done == "unreadable" or (done == "stale" and not entry.get("scheduled")):
                    # Given up (unreadable, or no start time for LIVE_WAIT, e.g. in a run whose
                    # listing missed it), but listed here: looked at like a slot of this channel.
                    done = None
                elif not done:
                    scheduled = parse_time(entry.get("scheduled"))
                    in_window = scheduled is not None and self.now >= scheduled - LIVE_START_LEAD
                    if ref.live_hint != "live" and not in_window:
                        continue  # _check_watched rechecks it by its start time
            if done in LIVE_FINAL:
                continue
            if ref.live_hint == "live":
                pass  # the tab says it is on air
            elif done:
                continue  # stale (never began): only a live badge brings it back
            elif not entry or not entry.get("scheduled"):
                # Start time unknown: a slot seen before start tracking existed, or one whose
                # watch page could not be read. Learn it: again LIVE_START_LEAD after a readable
                # look (which moves "at"); every run while the page stays unreadable.
                last = parse_time(entry.get("at"))
                if ref.live_hint != "upcoming" or (last and self.now - last < LIVE_START_LEAD):
                    continue
            else:
                scheduled = parse_time(entry.get("scheduled"))
                if scheduled is None or self.now < scheduled - LIVE_START_LEAD:
                    continue
            if excluded_stream(self.cfg, "live_now", ref.title):
                _settle_live(state, video_id, "excluded")
                continue
            if checks >= LIVE_CHECKS_PER_RUN:
                log.info("more than %d streams to check for a start; the rest wait for the next run",
                         LIVE_CHECKS_PER_RUN)
                break  # a waiting `watch` stream left out here: _check_watched, on its schedule
            checks += 1
            looked.add(video_id)
            if entry.get("external"):
                entry["checked"] = self.now.isoformat()
            self._live_status(state, result, ref, self.yt.fetch_video_info(ref))
        self._check_watched(state, result, looked)

    def _check_watched(self, state: State, result: CollectResult, looked: set) -> None:
        """Streams registered with `watch` (e.g. a collab on another channel): no tab lists
        them, so they are looked at by their start time (every run from LIVE_START_LEAD before
        it), and every WATCH_RECHECK before that (every LIVE_START_LEAD when the start time is
        unknown, or after an unreadable look) in case the start moved earlier."""
        never = datetime.min.replace(tzinfo=timezone.utc)
        window: list[tuple[timedelta, str]] = []
        rechecks: list[tuple[timedelta, str]] = []
        for video_id, entry in state.live.items():
            if (not entry.get("external") or entry.get("done") or video_id in looked
                    or ("video", video_id) in result.queued or ("live_start", video_id) in result.queued):
                continue  # settled, or already looked at / queued this run through this channel's tabs
            scheduled = parse_time(entry.get("scheduled"))
            last = parse_time(entry.get("checked") or entry.get("at")) or never
            in_window = scheduled is not None and self.now >= scheduled - LIVE_START_LEAD
            gap = timedelta(0) if in_window else LIVE_START_LEAD if scheduled is None else WATCH_RECHECK
            overdue = self.now - last - gap
            if in_window:
                window.append((overdue, video_id))
            elif overdue >= -RUN_SLACK:
                rechecks.append((overdue, video_id))
        # Streams in their window first, the longest unchecked first (so they take turns when
        # there are more than the cap); one look is kept for the most overdue recheck, so
        # streams waiting in their window never hold back noticing an early start elsewhere.
        # These looks are their own: this channel's slots (check_live_starts) never use them up.
        window.sort(key=lambda d: (-d[0], d[1]))
        rechecks.sort(key=lambda d: (-d[0], d[1]))
        keep = LIVE_CHECKS_PER_RUN - 1 if rechecks else LIVE_CHECKS_PER_RUN
        order = window[:keep] + rechecks[:1] + window[keep:] + rechecks[1:]
        checks = 0
        for _, video_id in order:
            if checks >= LIVE_CHECKS_PER_RUN:
                log.info("more than %d streams to check for a start; the rest wait for the next run",
                         LIVE_CHECKS_PER_RUN)
                return
            checks += 1
            # The run's start time (what the gap above is measured with); _live_status moves it
            # back after an unreadable look so the next one comes sooner.
            state.live[video_id]["checked"] = self.now.isoformat()
            ref = VideoRef(video_id, title=state.live[video_id].get("title"))
            self._live_status(state, result, ref, self.yt.fetch_video_info(ref))

    def _live_status(self, state: State, result: CollectResult, ref: VideoRef, info: VideoInfo) -> None:
        video_id = ref.video_id
        if info.from_watch_page and video_id in state.live:
            state.live[video_id].pop("unreadable_since", None)
        if excluded_stream(self.cfg, info.kind, info.title):
            _settle_live(state, video_id, "excluded")
        elif info.kind == "live_now":
            # Due every run from now on, in case this notice cannot be sent this time; run()
            # settles it once it is sent (or held back by the emergency stop).
            _wait_live(state, video_id, info.started or self.now)
            log.info("stream %s is on air; announcing its start", video_id)
            meta = result.meta
            external = bool((state.live.get(video_id) or {}).get("external"))
            if external and info.channel_title:
                meta = ChannelMeta(info.channel_title, None, info.channel_url)  # the host's channel
            # A `watch` stream on another channel goes to the destinations taking "collab";
            # one on this channel, or listed in its tabs (whose slot notice went everywhere),
            # is announced like its own streams.
            here = (info.channel_id == self.cfg.youtube_channel_id
                    or video_id in self.listed or video_id in state.videos)
            result.queue(Notification(
                kind="collab_live" if external and not here else "live_now",
                item_type="live_start", item_id=video_id,
                sort_key=info.started or self.now,
                payload=render.video_payload(info, meta, None),
            ))
        elif not info.from_watch_page:
            # Nothing learned: look again next run. A watched stream on another channel whose page
            # stays unreadable (deleted or made private) is given up once it is LIVE_WAIT past its
            # start and has been unreadable for WATCH_UNREADABLE.
            entry = state.live.get(video_id)
            if entry is not None and entry.get("external"):
                if entry.get("scheduled"):  # look again in LIVE_START_LEAD rather than WATCH_RECHECK
                    entry["checked"] = (self.now - WATCH_RECHECK + LIVE_START_LEAD).isoformat()
                since = parse_time(entry.setdefault("unreadable_since", now_iso()))
                waiting = _waiting_since(entry, parse_time(entry.get("scheduled")))
                # Not while this channel's tabs list it: as before `watch` existed, a listed
                # slot is looked at again for as long as it is listed.
                if (self.listing_complete and video_id not in self.listed
                        and waiting and self.now - waiting > LIVE_WAIT
                        and since and self.now - since >= WATCH_UNREADABLE):
                    log.info("watched stream %s has been unreadable since %s; no longer checked",
                             video_id, entry["unreadable_since"])
                    _settle_live(state, video_id, "unreadable")
        elif info.kind == "live_upcoming":
            entry = state.live.get(video_id) or {}
            scheduled = info.scheduled_start or parse_time(entry.get("scheduled"))
            # No start time yet while listed here: keep learning it, as for a slot of this channel
            # (a `watch` registration's own time would otherwise count; it may be long past).
            waiting = None if scheduled is None and video_id in self.listed else _waiting_since(entry, scheduled)
            if waiting and self.now - waiting > LIVE_WAIT:
                log.info("stream %s has not begun %s after its start time; no longer checked",
                         video_id, LIVE_WAIT)
                _settle_live(state, video_id, "stale")
            else:
                _wait_live(state, video_id, scheduled)
        elif info.kind == "live_archive":
            log.info("stream %s was already over when checked; no start notice", video_id)
            _settle_live(state, video_id, "ended")
        else:
            _settle_live(state, video_id, "not_live")

    # ------------------------------------------------------------------ thumbnails

    def check_thumbnails(self, state: State, result: CollectResult) -> None:
        """Changed thumbnails (see thumbnails.py). A secondary feature: whatever goes wrong
        here is logged and reported, never stops this run's other notifications."""
        if "videos" not in state.seeded:
            return  # no listing has ever worked: nothing to go by
        watch = ThumbnailWatch(self.yt, self.now, self.cfg.thumb_dir, dry_run=self.dry_run,
                               max_age=self.max_age)
        try:
            notices = watch.run(state, self.refs, result.meta,
                                lambda title: excluded_stream(self.cfg, "live_upcoming", title))
        except Exception as exc:  # noqa: BLE001
            log.exception("thumbnail check failed")
            result.thumb_problem = f"確認中にエラーが起きました（{exc.__class__.__name__}）"
            return
        result.thumb_problem = watch.problem
        for notice in notices:
            result.queue(Notification(
                kind=notice.kind, item_type="thumbnail", item_id=f"{notice.video_id}@{notice.version}",
                sort_key=notice.sort_key, payload=notice.payload, files=notice.files,
                retry_payload=notice.retry_payload,
            ))

    # ------------------------------------------------------------------ posts

    def _post_age_ok(self, post: Post) -> bool:
        age = approx_age_hours(post.published_text)
        return age is None or age <= self.max_age.total_seconds() / 3600

    def _post_sort_key(self, post: Post, index: int) -> datetime:
        age = approx_age_hours(post.published_text) or 0
        return self.now - timedelta(hours=age) + timedelta(microseconds=index)

    def _queue_post(self, result: CollectResult, post: Post, kind: str, index: int) -> None:
        result.queue(Notification(
            kind=kind, item_type="post", item_id=post.post_id,
            sort_key=self._post_sort_key(post, index),
            payload=render.post_payload(post, result.meta, None),  # mentions are per destination
        ))

    def _process_public(self, state: State, result: CollectResult, page: PostsPage,
                        exclude: set[str]) -> None:
        if "posts" not in state.seeded:
            if not page.valid and not page.posts:
                log.warning("logged-out posts page had no post list; posts not seeded yet")
                return
            for post in page.posts:
                state.posts.setdefault(post.post_id, now_iso())
            state.seeded["posts"] = now_iso()
            log.info("initial posts run: marked %d existing posts as seen", len(page.posts))
            return
        new = [p for p in page.posts if p.post_id not in state.posts and p.post_id not in exclude]
        for index, post in enumerate(reversed(new)):  # listed newest first; announce oldest first
            if not self._post_age_ok(post):
                log.debug("post %s is old; marking seen without notifying", post.post_id)
                state.posts[post.post_id] = now_iso()
                continue
            # A post the logged-out page itself flags as members-only (e.g. a future locked
            # teaser) is still announced link-only.
            kind = "member_post" if post.members_only else "post"
            if kind == "post":
                log.info("new post %s", post.post_id)  # public post; never log any post text
            self._queue_post(result, post, kind, index)

    def _process_members(self, state: State, result: CollectResult, member_page: PostsPage,
                         public_page: PostsPage | None) -> None:
        badge, by_comparison = classify_member_posts(member_page, public_page)
        if member_page.locked_without_id:
            log.warning("%d locked members-only post(s) had no recognizable id and were skipped",
                        member_page.locked_without_id)
        seeding = False
        if "posts_member" not in state.seeded:
            # Badge-marked posts need no comparison, so this baseline never waits for the
            # logged-out page.
            for post in badge:
                state.posts.setdefault(post.post_id, now_iso())
            state.seeded["posts_member"] = now_iso()
            log.info("initial members-only check: existing members-only posts marked as seen")
            seeding = True
        if public_page is not None and "posts_member_diff" not in state.seeded:
            # First usable comparison: posts hidden from logged-out viewers right now are an
            # existing backlog. They go to pending and are settled silently once confirmed,
            # because one of them could still be a public post that is not visible yet.
            for post in by_comparison:
                if post.post_id not in state.posts:
                    state.pending[post.post_id] = BACKLOG_PREFIX + now_iso()
            state.seeded["posts_member_diff"] = now_iso()
            seeding = True
        if seeding:
            return
        announce: list[Post] = [p for p in badge if p.post_id not in state.posts]
        for post in by_comparison:
            if post.post_id in state.posts:
                continue
            marker = state.pending.get(post.post_id)
            if marker is None:
                state.pending[post.post_id] = now_iso()
            elif marker.startswith(BACKLOG_PREFIX):
                state.posts[post.post_id] = now_iso()  # existed before the baseline: settle silently
                del state.pending[post.post_id]
            else:
                announce.append(post)  # second observation: confirmed
        announced = 0
        for index, post in enumerate(reversed(announce)):
            if not self._post_age_ok(post):
                state.posts[post.post_id] = now_iso()
                state.pending.pop(post.post_id, None)
                continue
            announced += 1
            log.debug("new members-only post %s", post.post_id)
            self._queue_post(result, replace(post, members_only=True), "member_post", index)
        if announced:
            log.info("%d new members-only post(s)", announced)  # no ids/text in public logs

    def _fetch_member_page(self, result: CollectResult) -> PostsPage | None:
        if not self.yt.cookies_configured:
            return None
        result.cookie_source = getattr(self.yt, "cookie_source", None)
        if self.yt.cookie_error or not self.yt.has_cookies:
            status = "read_failed" if getattr(self.yt, "cookie_transient", False) else "unreadable"
            result.member_status, result.member_detail = status, self.yt.cookie_error
            return None
        try:
            page = self.yt.fetch_posts_page(member=True)
        except YouTubeError as exc:
            result.member_status, result.member_detail = "fetch_failed", str(exc)
            return None
        if not page.logged_in:
            result.member_status = "logged_out"
            return None
        if page.membership == "not_member":
            result.member_status = "not_member"  # a non-member sees no members-only posts
            return None
        result.member_status = "ok"
        return page

    def check_posts(self, state: State, result: CollectResult, include_public: bool = True) -> None:
        # The member page is fetched first and the logged-out page second, so a public post
        # published in between is never mistaken for a members-only one.
        member_page = self._fetch_member_page(result)
        public_page = None
        if include_public or member_page is not None:
            try:
                public_page = self.yt.fetch_posts_page(member=False)
            except YouTubeError as exc:
                if include_public:
                    result.errors.append(f"posts: {exc}")
                else:
                    log.warning("logged-out posts page unavailable: %s", exc)
        if public_page is not None and public_page.logged_in:
            # Must never happen (the logged-out session carries no account cookies).
            result.errors.append("posts: logged-out page came back logged in")
            public_page = None
        for page in (public_page, member_page):
            if page is not None:
                result.meta = result.meta or page.meta
        comparable = comparable_public_page(public_page, member_page)
        if member_page is not None and (problem := member_page_problem(member_page, comparable)):
            result.member_status, result.member_detail = "fetch_failed", problem
            member_page = None
        badge_ids = {p.post_id for p in member_page.posts if p.members_only} if member_page else set()
        if include_public and public_page is not None:
            self._process_public(state, result, public_page, exclude=badge_ids)
        if member_page is not None:
            self._process_members(state, result, member_page, comparable)

    def collect(self, state: State, mode: str = "full") -> CollectResult:
        result = CollectResult()
        if mode == "full":
            self.check_videos(state, result)
            self.check_posts(state, result, include_public=True)
            self.check_thumbnails(state, result)
        elif mode == "members":
            self.check_posts(state, result, include_public=False)
        else:
            raise ValueError(f"unknown mode {mode!r}")
        result.notifications.sort(key=lambda n: n.sort_key)
        return result


# ---------------------------------------------------------------------- entry points


def _fresh(value: str | None, now: datetime, minutes: float) -> bool:
    last = parse_time(value)
    return bool(last and now - last < timedelta(minutes=minutes))


def _primary_is_fresh(state: State, cfg: Config, now: datetime) -> bool:
    return _fresh(state.runners.get("primary", {}).get("last_success"), now, cfg.fallback_after_minutes)


MEMBER_SELF_CHECK_HOURS = 24


def _member_check_due(state: State, cfg: Config, yt: YouTubeClient, now: datetime) -> bool:
    """Fallback-only: members-only checks run on their own schedule while the primary is
    healthy. If the primary already has working member cookies, the fallback still checks
    once a day so its own (spare) cookies are known to work."""
    if not yt.cookies_configured:
        return False
    last = state.runners.get("fallback", {}).get("last_member_check")
    primary = state.runners.get("primary", {})
    if _fresh(primary.get("last_member_success"), now, cfg.fallback_after_minutes):
        return not _fresh(last, now, MEMBER_SELF_CHECK_HOURS * 60)
    return not _fresh(last, now, cfg.member_check_minutes)


def _in_github() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _runner_label(role: str) -> str:
    if _in_github():
        return "GitHub Actions"
    return "Mac（予備系）" if role == "fallback" else "主系の PC"


COOKIE_PROBLEMS = {
    "unreadable": "Cookie を読み込めません（{detail}）。",
    "logged_out": "Cookie でログイン状態になりません。Cookie が失効した可能性があります。",
    "not_member": "Cookie のアカウントはログインできていますが、チャンネルのメンバーではありません"
                  "（メンバーシップが切れた可能性があります）。",
    "fetch_failed": "メン限投稿のチェックが {streak} 回続けて失敗しています（{detail}）。"
                    "YouTube の一時的な不調か、ページの形が変わった可能性があります。",
    "read_failed": "Bot 用 Firefox プロファイルから YouTube のログイン Cookie を {streak} 回続けて取得できません（{detail}）。",
}

PLATFORM = sys.platform  # the runner sending an alert is the one to fix (tests override this)


def _login_launcher(platform: str) -> str:
    """Where the runner's installer put the 'open the bot Firefox profile' launcher."""
    if platform == "darwin":
        return "Mac のホームの「アプリケーション」（~/Applications）にある「achichi-notifier YouTube login」"
    return "Windows のスタートメニューの「achichi-notifier YouTube login」"


def _firefox_login_fix(platform: str) -> str:
    return (f"{_login_launcher(platform)}を開き、通常ウィンドウのままメンバー加入済みのアカウントで YouTube に"
            "ログインしてください。パスワードは保存せず、ログアウトせずにメニューから Firefox を終了します。"
            "次の確認（数分以内）で自動的に戻ります。何も表示されないときは 3 分待ってもう一度（毎日の自動更新中の可能性）。")


def _cookie_fix(role: str, status: str, source: str | None = None, platform: str | None = None) -> str:
    platform = platform or PLATFORM
    if status == "fetch_failed":
        return ("しばらく様子を見て、続くようなら、この PC にインストールしたアプリで check を実行してください"
                "（Windows: `& \"$env:LOCALAPPDATA\\achichi-notifier\\venv\\Scripts\\python.exe\" -m achichi_notifier "
                "--env-file \"$env:LOCALAPPDATA\\achichi-notifier\\.env\" check`、Mac: "
                "`cd ~/Library/Application\\ Support/achichi-notifier && ./venv/bin/python -m achichi_notifier check`）。")
    if source == "firefox":
        if status == "unreadable":
            if platform == "darwin":
                return ("Mac で `git pull` → `./deploy/mac/install.sh` を実行してください。"
                        "Firefox の更新で保存形式が変わった可能性があるときは、アプリの更新が必要です。")
            return ("主系の PC で `git pull` → `deploy\\windows\\install.ps1 -FirefoxProfile` を実行してください。"
                    "Firefox の更新で保存形式が変わった可能性があるときは、アプリの更新が必要です。"
                    "急ぐ場合は、従来どおり cookies.txt を書き出して置き、`install.ps1 -NoFirefoxProfile` で切り替えられます。")
        if status == "not_member":
            return (f"{_login_launcher(platform)}で開き、右上のアカウントを確認して、"
                    "メンバー加入済みのアカウントでログインし直してください。")
        return _firefox_login_fix(platform)
    if _in_github():
        return ("Cookie はクラウドに置かない運用です。GitHub の Secret `YOUTUBE_COOKIES_B64` を "
                "`gh secret delete YOUTUBE_COOKIES_B64` で削除してください。")
    if role == "fallback":
        return ("Cookie を書き出し直して `~/Documents/DiscordBot_achichi/cookies.txt` に置き、"
                "`./deploy/mac/install.sh` を再実行してください。")
    return ("Cookie を書き出し直して、この PC のリポジトリフォルダに `cookies.txt` として置き、"
            "インストールをやり直してください（Windows: `deploy\\windows\\install.ps1`、Mac: `./deploy/mac/install.sh`）。")


def _cookie_alert(cfg: Config, discord: DiscordClient | None, state: State, role: str,
                  status: str, detail: str | None, dry_run: bool, streak: int = 0,
                  source: str | None = None) -> None:
    if status not in COOKIE_PROBLEMS:
        return
    # Keep public Actions logs generic; the details go to the private state channel only.
    shown = "see the state channel" if _in_github() else f"{status} ({detail})" if detail else status
    log.warning("members-only check degraded: %s", shown)
    if status in STREAK_STATUSES and streak < FETCH_FAIL_ALERT_STREAK:
        return
    if dry_run or discord is None or cfg.state_backend != "discord" or not cfg.state_channel_id:
        return
    key = f"cookie:{role}:{status}"
    if _fresh(state.alerts.get(key), datetime.now(timezone.utc), COOKIE_ALERT_INTERVAL.total_seconds() / 60):
        return
    message = (f"⚠️ **メン限投稿のチェックが止まっています**（{_runner_label(role)}）\n"
               + COOKIE_PROBLEMS[status].format(detail=detail or "詳細不明", streak=streak)
               + "\n動画・公開投稿の通知はこのまま続きます。\n対処: " + _cookie_fix(role, status, source))
    try:
        discord.send(cfg.state_channel_id, {"content": message, "allowed_mentions": {"parse": []}})
        state.alerts[key] = now_iso()
    except DiscordError as exc:
        log.warning("could not send cookie alert: %s", exc)


PRIMARY_DOWN_ALERT_INTERVAL = timedelta(hours=12)


def _primary_down_alert(cfg: Config, discord: DiscordClient | None, state: State,
                        now: datetime, dry_run: bool) -> None:
    """Fallback took over: say so in the private state channel (at most every 12h)."""
    primary = state.runners.get("primary", {})
    last_success, last_run = parse_time(primary.get("last_success")), parse_time(primary.get("last_run"))
    if last_success is None and last_run is None:
        return  # no primary has ever run (e.g. Mac-only setup)
    if dry_run or discord is None or cfg.state_backend != "discord" or not cfg.state_channel_id:
        return
    if _fresh(state.alerts.get("primary_down"), now, PRIMARY_DOWN_ALERT_INTERVAL.total_seconds() / 60):
        return
    since = f"<t:{int((last_success or last_run).timestamp())}:R>"
    if last_run and now - last_run < timedelta(minutes=cfg.fallback_after_minutes):
        cause = ("主系の PC は動いていますが、確認が失敗し続けています。"
                 "主系の PC の `notifier.log` を確認してください。")
    else:
        cause = ("主系の PC が動いていません。電源・スリープ・サインアウト・"
                 "再起動後の未サインインなどを確認してください。")
    message = (f"⚠️ **主系の PC が止まっています**（最終成功 {since}）。いまは Mac（予備系）が代わりに確認しています。\n"
               + cause)
    try:
        discord.send(cfg.state_channel_id, {"content": message, "allowed_mentions": {"parse": []}})
        state.alerts["primary_down"] = now_iso()
    except DiscordError as exc:
        log.warning("could not send the primary-down alert: %s", exc)


def _browser_refresh_alert(cfg: Config, discord: DiscordClient | None, state: State,
                           role: str = "primary", platform: str | None = None) -> None:
    """The daily headless visit keeps failing (read from browser-refresh.json). Each runner
    reports its own Firefox profile, under its own throttle key."""
    platform = platform or PLATFORM
    key = "browser_refresh" if role == "primary" else f"browser_refresh:{role}"
    from .browser_refresh import read_status
    status = read_status(cfg.firefox_profile)
    failures = int(status.get("consecutive_failures") or 0)
    stuck = status.get("last_result") == "stuck-in-use"
    if failures < BROWSER_REFRESH_ALERT_FAILURES and not stuck:
        return
    if discord is None or cfg.state_backend != "discord" or not cfg.state_channel_id:
        return
    now = datetime.now(timezone.utc)
    if _fresh(state.alerts.get(key), now, COOKIE_ALERT_INTERVAL.total_seconds() / 60):
        return
    attempt, alerted = parse_time(status.get("last_attempt")), parse_time(state.alerts.get(key))
    if attempt and alerted and attempt <= alerted:
        return  # no new refresh attempt since the last alert (the user may have fixed it)
    last = str(status.get("last_result") or "?")[:60]
    mac = platform == "darwin"
    where = _runner_label(role)
    if stuck:
        days = int(status.get("in_use_streak") or 0)
        message = (f"⚠️ **Bot 用 Firefox が {days} 日続けて「使用中」で、毎日の自動更新ができていません**（{where}）。\n"
                   f"{_login_launcher(platform)}で開いた Firefox を開いたままに"
                   "していないか確認し、閉じてください。"
                   + ("ウィンドウが見当たらない場合はアクティビティモニタで firefox を終了するか、Mac を再起動してください。"
                      if mac else
                      "ウィンドウが見当たらない場合はタスク マネージャーで firefox.exe を終了するか、PC を再起動してください。"))
    else:
        message = (f"⚠️ **Bot 用 Firefox の毎日の自動更新が {failures} 回続けて失敗しています**（{where}、最後: `{last}`）。\n"
                   "メン限投稿のチェックはまだ動いていますが、このままだとログインが切れるおそれがあります。\n"
                   + ("対処: Mac で `~/Library/Application Support/achichi-notifier/browser-refresh.log` を確認し、"
                      "Firefox（/Applications/Firefox.app）が入っているか確かめてください。手動で 1 回動かす場合は "
                      "`launchctl kickstart gui/$(id -u)/com.achichi.notifier.browser`。"
                      if mac else
                      "対処: 主系の PC で `%LOCALAPPDATA%\\achichi-notifier\\browser-refresh.log` を確認し、Firefox が"
                      "インストールされているか確かめてください。手動で 1 回動かす場合は "
                      "`Start-ScheduledTask -TaskName achichi-notifier-browser`。"))
    try:
        discord.send(cfg.state_channel_id, {"content": message, "allowed_mentions": {"parse": []}})
        state.alerts[key] = now_iso()
    except DiscordError as exc:
        log.warning("could not send the browser refresh alert: %s", exc)


THUMB_ALERT_STREAK = 6  # runs in a row (half an hour) before #管理 hears about it
THUMB_ALERT_INTERVAL = timedelta(hours=24)


def _thumb_alert(cfg: Config, discord: DiscordClient | None, state: State, runner: dict,
                 problem: str | None) -> None:
    """Thumbnail checks keep running into trouble (or skipped a burst of changes): tell #管理,
    at most once a day."""
    runner["thumb_problem_streak"] = int(runner.get("thumb_problem_streak") or 0) + 1 if problem else 0
    if not problem or (runner["thumb_problem_streak"] < THUMB_ALERT_STREAK and "通知しませんでした" not in problem):
        return
    if discord is None or cfg.state_backend != "discord" or not cfg.state_channel_id:
        return
    now = datetime.now(timezone.utc)
    if _fresh(state.alerts.get("thumbnails"), now, THUMB_ALERT_INTERVAL.total_seconds() / 60):
        return
    message = (f"⚠️ **サムネイル変更の確認で問題が起きています**: {problem}\n"
               "動画・配信の通知には影響しません。続くようなら `notifier.log` の thumbnail の行を確認してください。")
    try:
        discord.send(cfg.state_channel_id, {"content": message, "allowed_mentions": {"parse": []}})
        state.alerts["thumbnails"] = now_iso()
    except DiscordError as exc:
        log.warning("could not send the thumbnail alert: %s", exc)


DESTINATION_ALERT_INTERVAL = timedelta(hours=12)


UNDELIVERED_RETRY_HOURS = 2


def _targets_by_channel(targets_all, kind: str) -> dict[str, tuple]:
    """channel -> (first destination, combined mention). Destinations that share a channel get
    one message carrying every mention, instead of the second one being silently dropped."""
    out: dict[str, tuple] = {}
    for dest in targets_all:
        channel = dest.channel_for(kind) if dest.wants(kind) else None
        if not channel:
            continue
        mention = dest.mention_for(kind)
        if channel in out:
            first, combined = out[channel]
            parts = (combined or "").split()
            for part in (mention or "").split():
                if part not in parts:
                    parts.append(part)
            out[channel] = (first, " ".join(parts) or None)
        else:
            out[channel] = (dest, mention)
    return out


def _retry_undelivered(state: State, discord: DiscordClient, recent, targets_all=()) -> int:
    """Re-send notifications that missed one channel because of temporary trouble.
    Uses the settings as they are now: if no destination takes this kind on this channel any
    more (kinds changed, channel moved, disabled, removed) the retry is dropped, and the mention
    is the one configured now (combined when destinations share the channel)."""
    sent = 0
    now = datetime.now(timezone.utc)
    for entry in state.undelivered.values():
        if entry.get("done"):
            continue
        channel, url, kind = entry.get("channel"), entry.get("url"), str(entry.get("kind") or "")
        payload = entry.get("payload")
        current = _targets_by_channel(targets_all, kind).get(channel) if kind in render.GROUPS else None
        if current is None or not isinstance(payload, dict):
            entry["done"] = now_iso()  # nobody takes this kind on this channel now: drop quietly
            continue
        dest, mention = current
        entry["dest"] = dest.name  # who takes this channel now (for the #管理 report)
        headline = _guard_headline(kind)
        if recent is not None and recent.contains(channel, url, headline):
            entry["done"] = now_iso()  # an earlier try did reach Discord after all
            continue
        first = parse_time(entry.get("first")) or now
        if first.tzinfo is None:
            first = first.replace(tzinfo=timezone.utc)
        if now - first > timedelta(hours=UNDELIVERED_RETRY_HOURS):
            tries = int(entry.get("tries") or 1)
            entry["done"] = entry["lost"] = now_iso()  # reported by _lost_alert
            held = int(entry.get("held") or 0)
            entry["why"] = (f"最初の失敗から {UNDELIVERED_RETRY_HOURS} 時間を過ぎました。"
                            f"送信を試したのは {tries} 回"
                            + (f"（Bot を止めていた間の {held} 回は送り直していません）" if held
                               else "（PC が止まっていた可能性があります）" if tries <= 2 else ""))
            continue
        try:
            resent = render.apply_mention(payload, mention)
            discord.send(channel, resent)
            entry["done"] = now_iso()
            sent += 1
            if recent is not None:
                recent.add(channel, url, resent.get("content"))
            log.info("re-sent a %s notification to %s", kind, dest.name)
        except DiscordError as exc:
            entry["tries"] = int(entry.get("tries") or 0) + 1
            if not exc.transient:  # e.g. the bot lost access meanwhile
                entry["done"] = entry["lost"] = now_iso()
                entry["why"] = str(exc)[:120]
    return sent


def _lost_alert(cfg: Config, discord: DiscordClient | None, state: State) -> None:
    """List every notification that was given up for a destination. Not time-throttled
    (each one is reported once), so no lost notification goes unmentioned."""
    if discord is None or cfg.state_backend != "discord" or not cfg.state_channel_id:
        return
    by_dest: dict[str, list[dict]] = {}
    for key, entry in state.undelivered.items():
        if entry.get("lost") and not entry.get("reported"):
            by_dest.setdefault(str(entry.get("dest") or key.split("|", 1)[0]), []).append(entry)
    for name, entries in sorted(by_dest.items()):
        lines = "\n".join(f"・<{e.get('url') or '-'}>（チャンネル {e.get('channel') or '?'}: "
                          f"{str(e.get('why') or '?')[:120]}）" for e in entries[:10])
        more = f"\nほか {len(entries) - 10} 件" if len(entries) > 10 else ""
        message = (f"⚠️ **送り先「{name}」に次の通知を届けられず、送り直しを打ち切りました**\n"
                   f"{lines}{more}\n必要ならそのサーバーに手動で投稿してください。"
                   "権限のエラー（HTTP 403 など）なら `uv run python -m achichi_notifier dest check "
                   f"{name}` で確認できます。")
        try:
            discord.send(cfg.state_channel_id, {"content": message, "allowed_mentions": {"parse": []}})
        except DiscordError as exc:
            log.warning("could not send the lost-notification alert: %s", exc)
            continue  # stays unreported: tried again next run
        stamp = now_iso()
        for entry in entries:
            entry["reported"] = stamp


def _destination_alert(cfg: Config, discord: DiscordClient | None, state: State,
                       failures: dict[tuple[str, str], list[tuple[str, str | None]]]) -> None:
    """Some destination could not be posted to. Temporary and permanent problems are
    throttled separately (12h); notifications given up for good are listed by _lost_alert."""
    if discord is None or cfg.state_backend != "discord" or not cfg.state_channel_id:
        return
    now = datetime.now(timezone.utc)
    for (name, category), details in failures.items():
        key = f"destination:{name}:{'permanent' if category == 'permanent' else 'temporary'}"
        if _fresh(state.alerts.get(key), now, DESTINATION_ALERT_INTERVAL.total_seconds() / 60):
            continue
        if category == "temporary":
            cause = ("一時的なエラーのようです。ほかの送り先には届いています。この送り先にだけ、最初の失敗から"
                     f"最大 {UNDELIVERED_RETRY_HOURS} 時間、自動でもう一度送ります（すでに届いた送り先には重複しません）。")
        elif category == "everywhere":
            cause = ("一時的なエラーのようです。どの送り先にも届かなかったので、次の確認からすべての送り先に"
                     f"もう一度送ります（届くまで毎回。公開から（配信は開始予定・終了の時刻から）{cfg.max_age_hours:g} 時間を過ぎたものは送りません）。")
        else:
            cause = ("**この送り先には届いていません。** Bot がそのサーバーにいるか、チャンネルの権限"
                     "（見る・送信・埋め込みリンク・履歴を読む）を確認してください。"
                     "\n確認: `uv run python -m achichi_notifier dest check " + name + "`")
        # The link stays outside the cut so a long Discord error body can never hide it.
        lines = "\n".join(f"・<{url or '-'}> `{detail[:160]}`" for detail, url in details[:5])
        message = f"⚠️ **送り先「{name}」に通知を送れませんでした**\n{lines}\n{cause}"
        try:
            discord.send(cfg.state_channel_id, {"content": message, "allowed_mentions": {"parse": []}})
            state.alerts[key] = now_iso()
        except DiscordError as exc:
            log.warning("could not send the destination alert: %s", exc)


class _RecentlyPosted:
    """Last-line guard against duplicates (e.g. two runners racing on the shared state):
    skip a notification whose link already appears in the channel's latest messages
    (with `headline` also in the message text, for a start notice: see _guard_headline)."""

    def __init__(self, discord: DiscordClient):
        self.discord = discord
        self.cache: dict[str, list[tuple[str, str]]] = {}

    def contains(self, channel_id: str, url: str | None, headline: str | None = None) -> bool:
        if not url:
            return False
        if channel_id not in self.cache:
            try:
                self.cache[channel_id] = list(self.discord.recent_notices(channel_id))
            except DiscordError as exc:
                log.warning("could not read recent messages for the duplicate check: %s", exc)
                self.cache[channel_id] = []
        return any(link == url and (headline is None or headline in text)
                   for link, text in self.cache[channel_id])

    def add(self, channel_id: str, url: str | None, text: str | None = None) -> None:
        if url and channel_id in self.cache:
            self.cache[channel_id].append((url, text or ""))


def run(cfg: Config, role: str = "primary", dry_run: bool = False) -> int:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    discord = DiscordClient(cfg.discord_token) if cfg.discord_token else None
    if not dry_run:
        cfg.require_discord()
    if dry_run and cfg.state_backend == "discord" and discord is None:
        raise ConfigError("dry-run with Discord state needs DISCORD_BOT_TOKEN (or pass --state-file)")
    store = make_store(cfg, discord)
    yt = YouTubeClient.from_config(cfg)

    state = store.load()
    now = datetime.now(timezone.utc)
    mode = "full"
    if role == "fallback" and _primary_is_fresh(state, cfg, now):
        if not _member_check_due(state, cfg, yt, now):
            log.debug("primary runner is healthy; fallback skipped")
            runner = state.runners.setdefault(role, {})
            if runner.get("features") != list(FEATURES) and not dry_run:
                runner["features"] = list(FEATURES)  # once after an update, so `dest` stops warning
                state.owner_role = role
                store.save(state)
            return 0
        mode = "members"

    notifier = Notifier(cfg, yt, now)
    notifier.dry_run = dry_run
    result = notifier.collect(state, mode)
    for error in result.errors:
        log.warning("source error: %s", error)
    for problem in result.tolerated:
        log.info("source error ignored (the channel tabs covered it): %s", problem)

    # Emergency stop (`stop` marker or a 🛑 reaction in #管理, read live): YouTube was checked
    # and the state is kept as usual, but nothing at all is sent. Read after the (slow) YouTube
    # fetch so a stop made meanwhile still counts. `sender` is None while stopped.
    stopped = None if dry_run else control.pause_reason(cfg, discord)
    if stopped:
        log.warning("stopped (%s): nothing is sent to Discord this run", stopped)
    sender = None if stopped else discord

    state.owner_role = role
    if role == "fallback" and mode == "full":
        _primary_down_alert(cfg, sender, state, now, dry_run)
    runner = state.runners.setdefault(role, {})
    if stopped:
        for entry in state.undelivered.values():
            if not entry.get("done"):
                entry["held"] = int(entry.get("held") or 0) + 1  # a retry skipped: said when given up
    if result.member_status is not None:
        runner["last_member_check"] = now_iso()
        if result.member_status == "ok":
            runner["last_member_success"] = now_iso()
        if result.member_status in STREAK_STATUSES:
            runner["member_fail_streak"] = int(runner.get("member_fail_streak") or 0) + 1
        else:
            runner["member_fail_streak"] = 0
        _cookie_alert(cfg, sender, state, role, result.member_status, result.member_detail,
                      dry_run, streak=int(runner.get("member_fail_streak") or 0),
                      source=result.cookie_source)
    if cfg.firefox_profile and not dry_run:
        _browser_refresh_alert(cfg, sender, state, role)
    if mode == "full" and not dry_run:
        _thumb_alert(cfg, sender, state, runner, result.thumb_problem)

    targets_all = dests.resolve(cfg, state)
    recent = _RecentlyPosted(sender) if sender is not None and not dry_run else None
    send_failures = skipped = delivered_total = muted_total = 0
    failed_destinations: dict[tuple[str, str], list[tuple[str, str | None]]] = {}

    def record_failure(name: str, category: str, detail: str, url: str | None) -> None:
        """category: "permanent", "temporary" (retried for this channel only) or
        "everywhere" (reached no destination: the whole item is tried again next run)."""
        failed_destinations.setdefault((name, category), []).append((detail, url))

    if not dry_run and sender is not None:
        delivered_total += _retry_undelivered(state, sender, recent, targets_all)
    for note in result.notifications:
        targets = _targets_by_channel(targets_all, note.kind)
        if stopped:
            if targets:  # would have gone out: list it for `resume`, never send it later
                muted_total += 1
                embed = (note.payload.get("embeds") or [{}])[0]
                key = note.url or f"{note.item_type}:{note.item_id}"
                if note.item_type == "live_start":
                    key += "#start"  # the slot notice may be listed under the same link
                state.muted[key] = {
                    "kind": note.kind, "title": str(embed.get("title") or "")[:120], "at": now_iso(),
                    "why": stopped,
                }
                log.warning("stopped: did not send the %s notification %s", note.kind, note.item_id)
            _mark_handled(state, note)  # never sent later, also after resume
            continue
        sent_or_present = 0
        temporary: list[tuple[str, str, dict, DiscordError]] = []
        for channel, (dest, mention) in targets.items():
            payload = render.apply_mention(note.payload, mention)
            if dry_run:
                print(f"--- [dry-run] {note.kind} -> {dest.name} channel {channel}")
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                sent_or_present += 1
            elif recent is not None and recent.contains(channel, note.url, _guard_headline(note.kind)):
                skipped += 1
                sent_or_present += 1
                log.info("this bot already posted that %s notification to %s", note.kind, dest.name)
            else:
                try:
                    try:
                        if note.files:
                            sender.send(channel, payload, files=note.files)
                        else:
                            sender.send(channel, payload)
                    except DiscordError as exc:
                        if not note.files or not note.retry_payload or exc.transient:
                            raise
                        # e.g. no "Attach Files" permission in that channel: send the version
                        # that links to the images instead
                        log.warning("could not upload the images of a %s notification to %s (%s); "
                                    "sending it with links only", note.kind, dest.name, exc)
                        payload = render.apply_mention(note.retry_payload, mention)
                        sender.send(channel, payload)
                    sent_or_present += 1
                    delivered_total += 1
                    if recent is not None:
                        recent.add(channel, note.url, payload.get("content"))
                except DiscordError as exc:
                    log.error("failed to send a %s notification to %s: %s", note.kind, dest.name, exc)
                    if exc.transient:
                        temporary.append((dest.name, channel, payload, exc))
                    else:
                        record_failure(dest.name, "permanent", f"{channel}: {exc}", note.url)
        if targets and not sent_or_present:
            # Delivered nowhere: keep the item unseen so the next run tries every destination.
            send_failures += 1
            for name, channel, _payload, exc in temporary:
                record_failure(name, "everywhere", f"{channel}: {exc}", note.url)
            continue
        for name, channel, _payload, exc in temporary:
            # Delivered elsewhere: retry only this destination for a while (no duplicates).
            # The payload is kept without mention: the retry adds the mention configured then.
            state.undelivered[f"{name}|{note.item_type}|{note.item_id}"] = {
                "channel": channel, "payload": note.retry_payload or note.payload, "url": note.url,
                "kind": note.kind,
                "item_type": note.item_type,
                "first": now_iso(), "tries": 1, "done": None, "lost": None, "reported": None,
            }
            record_failure(name, "temporary", f"{channel}: {exc}", note.url)
        _mark_handled(state, note)
        if note.kind != "member_post":
            log.info("%s %s %s -> %d destination(s)", "would notify" if dry_run else "notified",
                     "live start" if note.item_type == "live_start" else note.kind, note.item_id,
                     sent_or_present)
    if failed_destinations and not dry_run:
        _destination_alert(cfg, sender, state, failed_destinations)
    if not dry_run:
        _lost_alert(cfg, sender, state)

    runner["last_run"] = now_iso()
    runner["host"] = "github-actions" if _in_github() else "local"
    runner["features"] = list(FEATURES)  # lets `dest` warn about runners still on an old version
    if mode == "full" and not result.errors and not send_failures:
        runner["last_success"] = now_iso()

    if dry_run:
        # Local file state is still updated so repeated dry-runs behave like real runs;
        # the shared Discord state is never touched by a dry-run.
        if isinstance(store, FileStateStore):
            store.save(state)
        log.info("dry-run (%s): %d notification(s) printed, nothing posted", mode, len(result.notifications))
        return 0
    store.save(state)
    log.info("done (%s): %d message(s) sent, %d already posted, %d source error(s), %d undelivered%s", mode,
             delivered_total, skipped, len(result.errors), send_failures,
             f", stopped: {muted_total} held back" if stopped else "")
    return 1 if send_failures else 0


def outdated_runners(state: State, feature: str, now: datetime | None = None) -> list[str]:
    """Roles that ran in the last 24 hours on a version without this feature."""
    now = now or datetime.now(timezone.utc)
    old = []
    for role, runner in sorted(state.runners.items()):
        last = parse_time(runner.get("last_run"))
        if last and now - last < timedelta(hours=24) and feature not in (runner.get("features") or []):
            old.append(role)
    return old


def preview(cfg: Config, send: bool = False, dest_name: str = dests.MAIN, force: bool = False) -> int:
    """Render the latest item of each type regardless of state (to check formatting)."""
    yt = YouTubeClient.from_config(cfg)
    notifier = Notifier(cfg, yt)
    result = CollectResult()
    refs = notifier.collect_video_refs(result)
    picked: dict[str, VideoInfo] = {}
    for ref in refs:
        group = "short" if ref.is_short else ("live" if "streams" in ref.sources else "video")
        if group in picked:
            continue
        if group == "live" and ref.live_hint not in ("upcoming", "live"):
            continue  # stream previews show an open slot or a running stream, never an old archive
        info = yt.fetch_video_info(ref)
        if excluded_stream(cfg, info.kind, info.title):
            log.info("preview: stream %s is excluded by EXCLUDE_LIVE_TITLES (%s)", ref.video_id, info.title)
            continue
        picked[group] = info
    notes: list[Notification] = []
    for info in picked.values():
        notes.append(Notification(info.kind, "video", info.video_id, notifier.now,
                                  render.video_payload(info, result.meta, None)))
    member_page = notifier._fetch_member_page(result)
    try:
        public_page = yt.fetch_posts_page(member=False)
    except YouTubeError as exc:
        result.errors.append(f"posts: {exc}")
        public_page = None
    if public_page is not None and not public_page.logged_in:
        result.meta = result.meta or public_page.meta
        first_public = next((p for p in public_page.posts if not p.members_only), None)
        if first_public:
            notes.append(Notification("post", "post", first_public.post_id, notifier.now,
                                      render.post_payload(first_public, result.meta, None)))
    if member_page is not None:
        comparable = comparable_public_page(public_page, member_page)
        if not member_page_problem(member_page, comparable):
            badge, by_comparison = classify_member_posts(member_page, comparable)
            members = badge or by_comparison
            if members:
                notes.append(Notification("member_post", "post", members[0].post_id, notifier.now,
                                          render.post_payload(replace(members[0], members_only=True),
                                                              result.meta, None)))
    if result.member_status and result.member_status != "ok":
        log.warning("members-only check: %s", result.member_status)
    for error in result.errors:
        log.warning("source error: %s", error)
    for problem in result.tolerated:
        log.info("source error ignored (the channel tabs covered it): %s", problem)

    if send:
        cfg.require_discord()
        discord = DiscordClient(cfg.discord_token)
        state = make_store(cfg, discord).load()
        targets = [d for d in dests.resolve(cfg, state) if d.name == dest_name.lower()]
        if not targets:
            raise ConfigError(f"unknown destination: {dest_name}")
        old = outdated_runners(state, "preview_mark")
        if old and not force:
            # An older runner's "already posted?" check would take the preview for the real
            # notification and skip it for good (e.g. a members-only post awaiting confirmation).
            print(f"⚠️  [!!] {', '.join(old)} の PC が古い版のため、プレビューを送りませんでした。"
                  "古い版はプレビューを本物の通知と見分けられず、本物の通知を送らなくなることがあります。"
                  "Windows は git pull → install.ps1、Mac は ./deploy/mac/install.sh で更新してから、"
                  "もう一度実行してください（それでも送るときは --force）。")
            return 2
        stopped = control.pause_reason(cfg, discord)
        if stopped:
            print(f"⏸ Bot を止めているため（{stopped}）、プレビューを送りませんでした。{control.RESUME_HINT}。")
            return 2
        dest = targets[0]
        for note in notes:
            channel = dest.channel_for(note.kind)
            if not channel or not dest.wants(note.kind):
                continue
            payload = render.apply_mention(note.payload, None)  # previews never ping anyone
            # Sent exactly like the real notification (sound and push included); an invisible
            # mark keeps the "already posted?" check from mistaking it for the real thing.
            payload["content"] = mark_preview(payload["content"])
            discord.send(channel, payload)
            log.info("sent preview %s to %s", note.kind, dest.name)
    else:
        for note in notes:
            print(f"--- [preview] {note.kind}")
            print(json.dumps(note.payload, ensure_ascii=False, indent=2))
    return 1 if result.errors and not notes else 0
