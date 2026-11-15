"""One check cycle: fetch YouTube, diff against shared state, post to Discord."""

from __future__ import annotations

import json
import logging
import os
import platform
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import render
from .config import Config, ConfigError
from .discord_api import DiscordClient, DiscordError
from .state import DiscordStateStore, FileStateStore, State, now_iso, parse_time
from .youtube import (
    VIDEO_TABS, ChannelMeta, Post, VideoRef, YouTubeClient, YouTubeError, approx_age_hours,
)

log = logging.getLogger(__name__)

ROLES = ("primary", "fallback")
COOKIE_ALERT_INTERVAL = timedelta(hours=24)


@dataclass
class Notification:
    kind: str  # key of render.HEADLINES
    item_type: str  # "video" | "post"
    item_id: str
    sort_key: datetime
    payload: dict

    @property
    def group(self) -> str:
        return render.GROUPS[self.kind]


@dataclass
class CollectResult:
    notifications: list[Notification] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    meta: ChannelMeta | None = None
    posts_logged_in: bool | None = None


def make_store(cfg: Config, discord: DiscordClient | None):
    if cfg.state_backend == "discord":
        if discord is None or not cfg.state_channel_id:
            raise ValueError("discord state backend needs a Discord client and DISCORD_STATE_CHANNEL_ID")
        return DiscordStateStore(discord, cfg.state_channel_id)
    return FileStateStore(cfg.state_file)


def _mention(cfg: Config, kind: str) -> str | None:
    if kind == "member_post":
        return cfg.mentions.get("member_post") or cfg.mentions.get("post")
    return cfg.mentions.get(render.GROUPS[kind])


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

    # ------------------------------------------------------------------ videos

    def collect_video_refs(self, result: CollectResult) -> list[VideoRef]:
        groups: list[list[VideoRef]] = []
        try:
            refs, _ = self.yt.fetch_rss()
            groups.append(refs)
        except YouTubeError as exc:
            result.errors.append(f"rss: {exc}")
        for tab in VIDEO_TABS:
            try:
                refs, meta = self.yt.fetch_tab(tab)
                groups.append(refs)
                result.meta = result.meta or meta
            except YouTubeError as exc:
                result.errors.append(f"{tab}: {exc}")
        return _merge_refs(groups)

    def check_videos(self, state: State, result: CollectResult) -> None:
        refs = self.collect_video_refs(result)
        if not refs:
            return
        if "videos" not in state.seeded:
            for ref in refs:
                state.videos.setdefault(ref.video_id, now_iso())
            state.seeded["videos"] = now_iso()
            log.info("initial run: marked %d existing videos as seen (no notifications)", len(refs))
            return
        new = [r for r in refs if r.video_id not in state.videos]
        if len(new) > self.cfg.max_new_per_run:
            log.warning("%d new videos; handling %d this run", len(new), self.cfg.max_new_per_run)
            new = new[: self.cfg.max_new_per_run]
        for ref in new:
            info = self.yt.fetch_video_info(ref)
            if info.published and self.now - info.published > self.max_age:
                log.info("video %s is old (%s); marking seen without notifying",
                         ref.video_id, info.published.date())
                state.videos[ref.video_id] = now_iso()
                continue
            log.info("new video %s kind=%s members_only=%s", ref.video_id, info.kind, info.members_only)
            result.notifications.append(Notification(
                kind=info.kind,
                item_type="video",
                item_id=ref.video_id,
                sort_key=info.published or self.now,
                payload=render.video_payload(info, result.meta, _mention(self.cfg, info.kind)),
            ))

    # ------------------------------------------------------------------ posts

    def check_posts(self, state: State, result: CollectResult) -> None:
        try:
            page = self.yt.fetch_posts()
        except YouTubeError as exc:
            result.errors.append(f"posts: {exc}")
            return
        result.posts_logged_in = page.logged_in
        result.meta = result.meta or page.meta
        scope = "posts_member" if page.logged_in else "posts"
        if scope not in state.seeded:
            for post in page.posts:
                state.posts.setdefault(post.post_id, now_iso())
            state.seeded[scope] = now_iso()
            log.info("initial %s run: marked %d existing posts as seen", scope, len(page.posts))
            return
        new = [p for p in page.posts if p.post_id not in state.posts]
        # Posts are listed newest first; announce oldest first.
        for index, post in enumerate(reversed(new)):
            age = approx_age_hours(post.published_text)
            if age is not None and age > self.max_age.total_seconds() / 3600:
                log.info("post %s is old; marking seen without notifying", post.post_id)
                state.posts[post.post_id] = now_iso()
                continue
            kind = "member_post" if post.members_only else "post"
            # Never log post text: Actions logs of a public repo are public.
            log.info("new post %s members_only=%s", post.post_id, post.members_only)
            result.notifications.append(Notification(
                kind=kind,
                item_type="post",
                item_id=post.post_id,
                sort_key=self.now - timedelta(hours=age or 0) + timedelta(microseconds=index),
                payload=render.post_payload(post, result.meta, _mention(self.cfg, kind)),
            ))

    def collect(self, state: State) -> CollectResult:
        result = CollectResult()
        self.check_videos(state, result)
        self.check_posts(state, result)
        result.notifications.sort(key=lambda n: n.sort_key)
        return result


# ---------------------------------------------------------------------- entry points


def _primary_is_fresh(state: State, cfg: Config, now: datetime) -> bool:
    last = parse_time(state.runners.get("primary", {}).get("last_success"))
    return bool(last and now - last < timedelta(minutes=cfg.fallback_after_minutes))


def _cookie_alert(cfg: Config, discord: DiscordClient | None, state: State, dry_run: bool) -> None:
    msg = ("YouTube の Cookie が無効になっています（ログイン状態になりません）。"
           "メン限投稿は検知できません。Cookie を書き出し直して更新してください。")
    log.warning(msg)
    last = parse_time(state.alerts.get("cookie_invalid"))
    if dry_run or discord is None or not cfg.state_channel_id:
        return
    if last and datetime.now(timezone.utc) - last < COOKIE_ALERT_INTERVAL:
        return
    try:
        discord.send(cfg.state_channel_id, {"content": f"⚠️ {msg}", "allowed_mentions": {"parse": []}})
        state.alerts["cookie_invalid"] = now_iso()
    except DiscordError as exc:
        log.warning("could not send cookie alert: %s", exc)


def run(cfg: Config, role: str = "primary", dry_run: bool = False) -> int:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    discord = DiscordClient(cfg.discord_token) if cfg.discord_token else None
    if not dry_run:
        cfg.require_discord()
    if dry_run and cfg.state_backend == "discord" and discord is None:
        raise ConfigError("dry-run with Discord state needs DISCORD_BOT_TOKEN (or pass --state-file)")
    store = make_store(cfg, discord)
    yt = YouTubeClient(cfg.youtube_channel_id, cfg.cookies_file)

    state = store.load()
    now = datetime.now(timezone.utc)
    if role == "fallback" and _primary_is_fresh(state, cfg, now):
        log.debug("primary runner is healthy; fallback skipped")
        return 0

    notifier = Notifier(cfg, yt, now)
    result = notifier.collect(state)
    for error in result.errors:
        log.warning("source error: %s", error)
    if yt.has_cookies and result.posts_logged_in is False:
        _cookie_alert(cfg, discord, state, dry_run)

    send_failures = 0
    for note in result.notifications:
        if dry_run:
            print(f"--- [dry-run] {note.kind} -> channel {cfg.channel_ids.get(note.group, '(unset)')}")
            print(json.dumps(note.payload, ensure_ascii=False, indent=2))
        else:
            try:
                discord.send(cfg.channel_ids[note.group], note.payload)
            except DiscordError as exc:
                send_failures += 1
                log.error("failed to send %s %s: %s", note.item_type, note.item_id, exc)
                continue
        target = state.videos if note.item_type == "video" else state.posts
        target[note.item_id] = now_iso()
        log.info("%s %s %s", "would notify" if dry_run else "notified", note.kind, note.item_id)

    runner = state.runners.setdefault(role, {})
    runner["last_run"] = now_iso()
    runner["host"] = "github-actions" if role == "primary" and _in_github() else platform.node()
    if not result.errors and not send_failures:
        runner["last_success"] = now_iso()

    if dry_run:
        # Local file state is still updated so repeated dry-runs behave like real runs;
        # the shared Discord state is never touched by a dry-run.
        if isinstance(store, FileStateStore):
            store.save(state)
        log.info("dry-run: %d notification(s) printed, nothing posted", len(result.notifications))
        return 0
    store.save(state)
    log.info("done: %d notified, %d source error(s), %d send failure(s)",
             len(result.notifications) - send_failures, len(result.errors), send_failures)
    return 1 if send_failures else 0


def _in_github() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def preview(cfg: Config, send: bool = False) -> int:
    """Render the latest item of each type regardless of state (to check formatting)."""
    yt = YouTubeClient(cfg.youtube_channel_id, cfg.cookies_file)
    notifier = Notifier(cfg, yt)
    result = CollectResult()
    refs = notifier.collect_video_refs(result)
    picked: dict[str, VideoRef] = {}
    for ref in refs:
        group = "short" if ref.is_short else ("live" if "streams" in ref.sources else "video")
        picked.setdefault(group, ref)
    notes: list[Notification] = []
    for ref in picked.values():
        info = yt.fetch_video_info(ref)
        notes.append(Notification(info.kind, "video", ref.video_id, notifier.now,
                                  render.video_payload(info, result.meta, None)))
    try:
        page = yt.fetch_posts()
        posts: list[Post] = page.posts
        result.meta = result.meta or page.meta
        log.info("posts page: %d posts, logged_in=%s", len(posts), page.logged_in)
        first_public = next((p for p in posts if not p.members_only), None)
        first_member = next((p for p in posts if p.members_only), None)
        for post in (first_public, first_member):
            if post:
                kind = "member_post" if post.members_only else "post"
                notes.append(Notification(kind, "post", post.post_id, notifier.now,
                                          render.post_payload(post, result.meta, None)))
    except YouTubeError as exc:
        result.errors.append(f"posts: {exc}")
    for error in result.errors:
        log.warning("source error: %s", error)

    if send:
        cfg.require_discord()
        discord = DiscordClient(cfg.discord_token)
        for note in notes:
            payload = dict(note.payload)
            payload["content"] = "【プレビュー】" + payload["content"]
            discord.send(cfg.channel_ids[note.group], payload)
            log.info("sent preview %s %s", note.kind, note.item_id)
    else:
        for note in notes:
            print(f"--- [preview] {note.kind} {note.item_id}")
            print(json.dumps(note.payload, ensure_ascii=False, indent=2))
    return 1 if result.errors and not notes else 0
