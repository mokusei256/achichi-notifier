"""Changed thumbnails of this channel's streams (slots and archives), videos and shorts.

Every thumbnail on i.ytimg.com has a version, a Unix time, given as its ETag. A URL with
?v=<version in hex> is answered only once the thumbnail is at least that new (HTTP 404 before,
cached for 30 s) and is a cache key of its own, while plain URLs may serve an old copy for up to
2 hours. So "changed since version V?" is one small HEAD request (YouTubeClient.thumb_probe).

Shared state (State.thumbs): video_id -> {
    v: the version announced last (at first the one found when tracking began; 0 is the frame
       YouTube picked itself, answered for ?v=0 only),
    seen: the newest version found (newer than v: a notice is due until it is sent),
    since: when tracking began (missing while a listed video waits for its first look, see
       _baselines; a newer version made before that is taken over silently),
    tried: when its first look last failed,
    found: when seen last grew, tab, title, listed: last time a listing showed it, checked}.
Local, per runner (cfg.thumb_dir): the images of versions v and seen, for the "before" side of a
notice (a runner without it sends the new image alone).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import render
from .state import THUMB_KEEP_DAYS, State, as_int, parse_time
from .youtube import BASE, THUMBS, ChannelMeta, ThumbUnreachable, VideoRef, YouTubeError

log = logging.getLogger(__name__)

CHECKS_PER_RUN = 25  # small HEAD requests
BASELINES_PER_RUN = 10  # first downloads (100-300 KB each): about 100 videos take 10 runs
RECHECK_UPCOMING = timedelta(minutes=5)  # a slot not on air yet: every run
RECHECK_LISTED = timedelta(minutes=30)  # on the first page of a tab this run
RECHECK_OTHER = timedelta(hours=2)  # older videos, known from the full listing
FULL_LISTING_EVERY = timedelta(hours=24)
FULL_LISTING_RETRY = timedelta(hours=1)
LISTED_RECENTLY = timedelta(days=2)  # no listing has shown it since: deleted or hidden, not checked
LISTING_ALERT_AFTER = timedelta(hours=30)  # no full listing has worked for this long: tell #管理
TIME_BUDGET = 60  # seconds per run for all of this: the run must still send and save in time
RUN_SLACK = timedelta(minutes=1)  # runs start a few seconds apart from the 5-minute grid
# This many changes found within MASS_WINDOW look like YouTube redoing thumbnails, not the
# channel: taken over without notices (and reported to #管理) rather than flooding the server.
MASS_COUNT = 10
MASS_WINDOW = timedelta(hours=3)
TABS = ("streams", "videos", "shorts")
KIND_OF_TAB = {"streams": "thumb_live", "videos": "thumb_video", "shorts": "thumb_short"}
_CACHE_FILE = re.compile(r"([A-Za-z0-9_-]{11})_([0-9a-f]{1,8})\.jpg")


@dataclass
class Notice:
    video_id: str
    version: int
    kind: str
    sort_key: datetime
    payload: dict  # with attachment:// images (files)
    retry_payload: dict  # links only: per-destination retries carry no files
    files: list[tuple[str, bytes]] = field(default_factory=list)


def _tracked(entry: dict) -> bool:
    return bool(entry.get("since"))


def tab_of(ref: VideoRef) -> str | None:
    if "streams" in ref.sources:
        return "streams"
    if "shorts" in ref.sources or ref.is_short:
        return "shorts"
    if "videos" in ref.sources:
        return "videos"
    return None  # RSS only: a tab lists it soon


def watch_url(video_id: str, tab: str | None) -> str:
    return f"{BASE}/shorts/{video_id}" if tab == "shorts" else f"{BASE}/watch?v={video_id}"


def notice_url(video_id: str, tab: str | None, version: int) -> str:
    """The notice's link. The fragment makes it differ from the video's own notices and from
    other changes of the same thumbnail, for the "already posted?" check."""
    return f"{watch_url(video_id, tab)}#thumbnail-{version:x}"


class ThumbnailWatch:
    def __init__(self, yt, now: datetime, cache_dir: Path | None, dry_run: bool = False,
                 max_age: timedelta = timedelta(hours=72)):
        self.yt = yt
        self.now = now
        self.stamp = now.replace(microsecond=0).isoformat()
        self.cache_dir = cache_dir
        self.dry_run = dry_run
        self.max_age = max_age  # a change not announced for this long is taken over quietly
        self.deadline = time.monotonic() + TIME_BUDGET
        self.stopped = False  # out of time, or i.ytimg.com unreachable: the rest waits
        self.problems: list[str] = []

    @property
    def problem(self) -> str | None:
        """What #管理 should hear about (see notifier._thumb_alert), or None."""
        return "、".join(dict.fromkeys(self.problems)) or None

    def _report(self, problem: str) -> None:
        self.problems.append(problem)

    def _may_ask(self) -> bool:
        """Whether another request to i.ytimg.com fits into this run."""
        if not self.stopped and time.monotonic() > self.deadline:
            log.warning("thumbnails: out of time for this run; the rest waits for the next")
            self.stopped = True
            self._report("確認が時間内に終わりません（i.ytimg.com が遅い可能性）")
        return not self.stopped

    def _unreachable(self, exc: YouTubeError) -> None:
        log.warning("thumbnails: i.ytimg.com unreachable (%s); the rest waits for the next run", exc)
        self.stopped = True
        self._report("i.ytimg.com（サムネイル画像のサーバー）に接続できません")

    # ------------------------------------------------------------------ run

    def run(self, state: State, refs: list[VideoRef], meta: ChannelMeta | None,
            excluded: Callable[[str], bool]) -> list[Notice]:
        page = {ref.video_id: (tab, ref) for ref in refs if (tab := tab_of(ref))}
        for video_id, (tab, ref) in page.items():
            self._listed(state, video_id, tab, ref)
        for video_id, (tab, ref) in self._full_listing(state).items():
            self._listed(state, video_id, tab, ref)
        self._listing_health(state)
        new = self._baselines(state, page)
        self._probe(state, page, new)
        notices = self._notices(state, page, meta, excluded)
        if not self.dry_run:
            self._prune_cache(state)
        return notices

    def _listed(self, state: State, video_id: str, tab: str, ref: VideoRef) -> None:
        entry = state.thumbs.setdefault(video_id, {})  # new: waits for its first look
        entry.update(listed=self.stamp, tab=tab)
        if ref.title:
            entry["title"] = ref.title[:120]

    def _full_listing(self, state: State) -> dict[str, tuple[str, VideoRef]]:
        """Every video of every tab, once a day (the first pages are read every run anyway)."""
        last = parse_time(state.thumbs_listed)
        if last and self.now - last < FULL_LISTING_EVERY - RUN_SLACK:
            return {}
        everything: dict[str, tuple[str, VideoRef]] = {}
        try:
            for tab in TABS:
                for ref in self.yt.fetch_tab_all(tab):
                    everything.setdefault(ref.video_id, (tab, ref))
        except YouTubeError as exc:
            log.warning("thumbnails: could not list every video (%s); trying again in an hour", exc)
            state.thumbs_listed = (self.now - FULL_LISTING_EVERY + FULL_LISTING_RETRY).isoformat()
            return {}
        state.thumbs_listed = state.thumbs_listed_ok = self.stamp
        log.info("thumbnails: %d videos listed", len(everything))
        return everything

    def _listing_health(self, state: State) -> None:
        """Videos past the first pages are known only from the full listing: one that keeps
        failing stops their checks after LISTED_RECENTLY, so say so (every run, so that the
        alert's run streak builds up although the listing is tried only once an hour)."""
        last = parse_time(state.thumbs_listed_ok) or min(
            (t for e in state.thumbs.values() if (t := parse_time(e.get("since")))), default=None)
        if last and self.now - last > LISTING_ALERT_AFTER:
            self._report("チャンネルの全動画の一覧を取得できていません（古い動画のサムネイルを確認できません）")

    def _baselines(self, state: State, page: dict) -> set[str]:
        """Start tracking listed videos not tracked yet: their current version, and its image.
        This run's first pages first (open slots first of all), then those of the full listing."""
        # Never tried before those that failed (oldest failure first), so a few videos without
        # an image never hold up the rest.
        new = sorted((vid for vid, e in state.thumbs.items() if not _tracked(e)
                      and (t := parse_time(e.get("listed"))) and self.now - t <= LISTED_RECENTLY),
                     key=lambda vid: (str(state.thumbs[vid].get("tried") or ""), vid not in page,
                                      vid not in page or page[vid][1].live_hint != "upcoming", vid))
        done: set[str] = set()
        failures = 0
        # A minute count as the version: always older than any real one, so the current image
        # comes back, and a new cache key every minute, so no cache serves an old copy.
        buster = int(self.now.timestamp()) // 60 % 0xFFFFF + 1
        for video_id in new[:BASELINES_PER_RUN]:
            if not self._may_ask():
                break
            try:
                try:
                    version, image, _ = self.yt.thumb_image(video_id, buster)
                except ThumbUnreachable:
                    raise
                except YouTubeError:
                    # Version 0 (a frame YouTube picked) is newer than no number: the plain URL.
                    version, image, _ = self.yt.thumb_image(video_id, None)
            except ThumbUnreachable as exc:
                self._unreachable(exc)
                break
            except YouTubeError as exc:
                log.info("thumbnails: no thumbnail for %s yet (%s)", video_id, exc)
                state.thumbs[video_id]["tried"] = self.stamp
                failures += 1
                if failures >= 3:
                    break
                continue
            self._save(video_id, version, image)
            state.thumbs[video_id].pop("tried", None)
            state.thumbs[video_id].update(v=version, seen=version, since=self.stamp, checked=self.stamp)
            done.add(video_id)
        if failures >= 3 and not done:
            self._report("サムネイル画像を取得できません（i.ytimg.com の仕様変更の可能性）")
        if len(new) > BASELINES_PER_RUN:
            log.info("thumbnails: %d more videos to start tracking in the next runs",
                     len(new) - BASELINES_PER_RUN)
        return done

    def _probe(self, state: State, page: dict, new: set[str]) -> None:
        due = []
        for video_id, entry in state.thumbs.items():
            listed = parse_time(entry.get("listed"))
            if (video_id in new or not _tracked(entry)
                    or not listed or self.now - listed > LISTED_RECENTLY):
                continue
            hint = page[video_id][1].live_hint if video_id in page else None
            if hint == "live":
                continue  # on air: looked at again once it is over
            interval = (RECHECK_UPCOMING if hint == "upcoming"
                        else RECHECK_LISTED if video_id in page else RECHECK_OTHER)
            checked = parse_time(entry.get("checked"))
            if checked and self.now - checked < interval - RUN_SLACK:
                continue
            due.append((hint != "upcoming", str(entry.get("checked") or ""), video_id))
        due.sort()
        unchanged = None
        failures = answered = 0
        for _, _, video_id in due[:CHECKS_PER_RUN]:
            if not self._may_ask():
                break
            entry = state.thumbs[video_id]
            base = max(as_int(entry.get("seen")), as_int(entry.get("v")))
            entry["checked"] = self.stamp  # also when it fails: the others get their turn
            try:
                newer = self.yt.thumb_probe(video_id, base)
            except ThumbUnreachable as exc:
                self._unreachable(exc)
                break
            except YouTubeError as exc:
                log.info("thumbnails: could not check %s (%s)", video_id, exc)
                failures += 1
                if failures >= 3:
                    break
                continue
            answered += 1
            if newer is not None and newer > base:
                log.info("thumbnail of %s changed: version %d -> %d", video_id, base, newer)
                entry["seen"] = newer
                entry["found"] = self.stamp
            elif unchanged is None:
                unchanged = (video_id, base)
        if len(due) > CHECKS_PER_RUN:
            log.info("thumbnails: %d checks left for the next runs", len(due) - CHECKS_PER_RUN)
        if failures and not answered:
            self._report("サムネイルの確認がすべて失敗しています（i.ytimg.com の仕様変更の可能性）")
        if unchanged and self._may_ask():
            # "Unchanged" is an HTTP 404, as for a thumbnail that is gone: make sure the
            # current version is still answered, or nothing would ever be found again.
            video_id, base = unchanged
            try:
                if self.yt.thumb_probe(video_id, base - 1) is None:
                    self._report(f"サムネイルの確認方法が使えなくなった可能性があります"
                                 f"（{video_id} の現在の版が見つかりません）")
            except ThumbUnreachable as exc:
                self._unreachable(exc)
            except YouTubeError as exc:
                self._report(f"サムネイルの確認方法が使えなくなった可能性があります（{exc}）")

    def _notices(self, state: State, page: dict, meta: ChannelMeta | None,
                 excluded: Callable[[str], bool]) -> list[Notice]:
        due = [(vid, e) for vid, e in state.thumbs.items()
               if _tracked(e) and as_int(e.get("seen")) > as_int(e.get("v"))]
        recent = sum(1 for e in state.thumbs.values()
                     if (t := parse_time(e.get("found"))) and self.now - t < MASS_WINDOW)
        mass = recent >= MASS_COUNT
        if mass and due:
            log.warning("thumbnails: %d changes within %s; taken over without notices", recent, MASS_WINDOW)
            self._report(f"{MASS_WINDOW.seconds // 3600} 時間で {recent} 本のサムネイルが変わったため、"
                         "YouTube 側の作り直しとみなして通知しませんでした")
        notices = []
        for video_id, entry in due:
            v, seen = as_int(entry.get("v")), as_int(entry.get("seen"))
            tab = entry.get("tab")
            title = entry.get("title") or video_id
            changed = datetime.fromtimestamp(seen, timezone.utc)
            since = parse_time(entry.get("since"))
            found = parse_time(entry.get("found"))
            quiet = (mass or since is None or changed < since  # made before tracking began
                     or (found is not None and self.now - found > self.max_age)  # never delivered
                     or (tab == "streams" and excluded(title)))  # like its stream notices
            if not self._may_ask():
                break  # announced next run
            try:
                got, after, name = self.yt.thumb_image(video_id, seen)
            except ThumbUnreachable as exc:
                self._unreachable(exc)
                break
            except YouTubeError as exc:
                log.info("thumbnails: could not download the new thumbnail of %s (%s)", video_id, exc)
                if quiet:
                    entry["v"] = seen
                continue  # tried again next run
            self._save(video_id, got, after)
            if got > seen:
                entry["seen"] = seen = got
            before = self._load(video_id, v)
            if quiet or before == after:  # the same picture again: nothing to show
                entry["v"] = seen
                continue
            hint = page[video_id][1].live_hint if video_id in page else None
            label = {"streams": "配信予定" if hint == "upcoming" else "配信", "videos": "動画",
                     "shorts": "ショート"}.get(tab or "", "動画")
            kind = KIND_OF_TAB.get(tab or "", "thumb_video")
            url = notice_url(video_id, tab, seen)
            files = [("before.jpg", before)] if before else []
            files.append(("after.jpg", after))
            notices.append(Notice(
                video_id=video_id, version=seen, kind=kind, sort_key=changed,
                payload=render.thumb_payload(kind, title, url, label, meta, changed,
                                             "attachment://after.jpg",
                                             "attachment://before.jpg" if before else None),
                retry_payload=render.thumb_payload(kind, title, url, label, meta, changed,
                                                   f"{THUMBS}/{video_id}/{name}?v={seen:x}", None),
                files=files,
            ))
        return notices

    # ------------------------------------------------------------------ local images

    def _path(self, video_id: str, version: int) -> Path | None:
        return self.cache_dir / f"{video_id}_{version:x}.jpg" if self.cache_dir else None

    def _save(self, video_id: str, version: int, image: bytes) -> None:
        path = self._path(video_id, version)
        if path is None or path.exists():
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(image)
            tmp.replace(path)
        except OSError as exc:
            log.warning("thumbnails: could not keep the image of %s (%s)", video_id, exc)

    def _load(self, video_id: str, version: int) -> bytes | None:
        path = self._path(video_id, version)
        try:
            return path.read_bytes() if path and path.is_file() else None
        except OSError:
            return None

    def _prune_cache(self, state: State) -> None:
        """Keep only the images of each tracked video's versions v and seen (State.prune drops
        the videos no listing has shown for THUMB_KEEP_DAYS when this run's state is saved)."""
        if not self.cache_dir or not self.cache_dir.is_dir():
            return
        gone = self.now - timedelta(days=THUMB_KEEP_DAYS)
        for path in self.cache_dir.iterdir():
            match = _CACHE_FILE.fullmatch(path.name)
            if match is None:
                if path.suffix == ".tmp":
                    path.unlink(missing_ok=True)
                continue
            entry = state.thumbs.get(match.group(1))
            version = int(match.group(2), 16)
            listed = parse_time(entry.get("listed")) if entry else None
            if (entry is None or (listed is not None and listed < gone)
                    or version not in (as_int(entry.get("v")), as_int(entry.get("seen")))):
                try:
                    path.unlink()
                except OSError:
                    pass
