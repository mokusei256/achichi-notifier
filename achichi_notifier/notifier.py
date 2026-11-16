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

from . import destinations as dests
from . import render
from .config import Config, ConfigError
from .discord_api import SUPPRESS_NOTIFICATIONS, DiscordClient, DiscordError
from .state import BACKLOG_PREFIX, DiscordStateStore, FileStateStore, State, now_iso, parse_time
from .youtube import (
    VIDEO_TABS, ChannelMeta, Post, PostsPage, VideoInfo, VideoRef, YouTubeClient, YouTubeError,
    approx_age_hours,
)

log = logging.getLogger(__name__)

ROLES = ("primary", "fallback")
FEATURES = ("destinations",)
COOKIE_ALERT_INTERVAL = timedelta(hours=24)
# Consecutive failed members-only checks before the state channel is told (~30 min on the
# Windows primary, ~1h on the Mac). Covers temporary page problems and Firefox-profile reads
# that fail while someone is logging in again.
FETCH_FAIL_ALERT_STREAK = 6
STREAK_STATUSES = ("fetch_failed", "read_failed")
BROWSER_REFRESH_ALERT_FAILURES = 3


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
        if rss_error:
            # The feed answers HTTP 404 now and then. The /videos, /shorts and /streams tabs
            # list everything it does, so it only counts as an error when they did not all
            # come back with videos.
            (result.tolerated if covered else result.errors).append(rss_error)
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
            if excluded_stream(self.cfg, info.kind, info.title):
                log.info("stream %s is excluded by EXCLUDE_LIVE_TITLES (%s); marking seen without notifying",
                         ref.video_id, info.title)
                state.videos[ref.video_id] = now_iso()
                continue
            if info.published and self.now - info.published > self.max_age:
                log.info("video %s is old (%s); marking seen without notifying",
                         ref.video_id, info.published.date())
                state.videos[ref.video_id] = now_iso()
                continue
            log.info("new video %s kind=%s members_only=%s", ref.video_id, info.kind, info.members_only)
            result.queue(Notification(
                kind=info.kind,
                item_type="video",
                item_id=ref.video_id,
                sort_key=info.published or self.now,
                payload=render.video_payload(info, result.meta, None),  # mentions are per destination
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
        if recent is not None and recent.contains(channel, url):
            entry["done"] = now_iso()  # an earlier try did reach Discord after all
            continue
        first = parse_time(entry.get("first")) or now
        if first.tzinfo is None:
            first = first.replace(tzinfo=timezone.utc)
        if now - first > timedelta(hours=UNDELIVERED_RETRY_HOURS):
            tries = int(entry.get("tries") or 1)
            entry["done"] = entry["lost"] = now_iso()  # reported by _lost_alert
            entry["why"] = (f"最初の失敗から {UNDELIVERED_RETRY_HOURS} 時間を過ぎました。"
                            f"送信を試したのは {tries} 回"
                            + ("（PC が止まっていた可能性があります）" if tries <= 2 else ""))
            continue
        try:
            discord.send(channel, render.apply_mention(payload, mention))
            entry["done"] = now_iso()
            sent += 1
            if recent is not None:
                recent.add(channel, url)
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
                     f"もう一度送ります（届くまで毎回。公開から {cfg.max_age_hours:g} 時間を過ぎたものは送りません）。")
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
    skip a notification whose link already appears in the channel's latest messages."""

    def __init__(self, discord: DiscordClient):
        self.discord = discord
        self.cache: dict[str, set[str]] = {}

    def contains(self, channel_id: str, url: str | None) -> bool:
        if not url:
            return False
        if channel_id not in self.cache:
            try:
                self.cache[channel_id] = self.discord.recent_urls(channel_id)
            except DiscordError as exc:
                log.warning("could not read recent messages for the duplicate check: %s", exc)
                self.cache[channel_id] = set()
        return url in self.cache[channel_id]

    def add(self, channel_id: str, url: str | None) -> None:
        if url and channel_id in self.cache:
            self.cache[channel_id].add(url)


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
    result = notifier.collect(state, mode)
    for error in result.errors:
        log.warning("source error: %s", error)
    for problem in result.tolerated:
        log.info("source error ignored (the channel tabs covered it): %s", problem)

    state.owner_role = role
    if role == "fallback" and mode == "full":
        _primary_down_alert(cfg, discord, state, now, dry_run)
    runner = state.runners.setdefault(role, {})
    if result.member_status is not None:
        runner["last_member_check"] = now_iso()
        if result.member_status == "ok":
            runner["last_member_success"] = now_iso()
        if result.member_status in STREAK_STATUSES:
            runner["member_fail_streak"] = int(runner.get("member_fail_streak") or 0) + 1
        else:
            runner["member_fail_streak"] = 0
        _cookie_alert(cfg, discord, state, role, result.member_status, result.member_detail,
                      dry_run, streak=int(runner.get("member_fail_streak") or 0),
                      source=result.cookie_source)
    if cfg.firefox_profile and not dry_run:
        _browser_refresh_alert(cfg, discord, state, role)

    targets_all = dests.resolve(cfg, state)
    recent = _RecentlyPosted(discord) if discord is not None and not dry_run else None
    send_failures = skipped = delivered_total = 0
    failed_destinations: dict[tuple[str, str], list[tuple[str, str | None]]] = {}

    def record_failure(name: str, category: str, detail: str, url: str | None) -> None:
        """category: "permanent", "temporary" (retried for this channel only) or
        "everywhere" (reached no destination: the whole item is tried again next run)."""
        failed_destinations.setdefault((name, category), []).append((detail, url))

    if not dry_run and discord is not None:
        delivered_total += _retry_undelivered(state, discord, recent, targets_all)
    for note in result.notifications:
        targets = _targets_by_channel(targets_all, note.kind)
        sent_or_present = 0
        temporary: list[tuple[str, str, dict, DiscordError]] = []
        for channel, (dest, mention) in targets.items():
            payload = render.apply_mention(note.payload, mention)
            if dry_run:
                print(f"--- [dry-run] {note.kind} -> {dest.name} channel {channel}")
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                sent_or_present += 1
            elif recent is not None and recent.contains(channel, note.url):
                skipped += 1
                sent_or_present += 1
                log.info("this bot already posted that %s notification to %s", note.kind, dest.name)
            else:
                try:
                    discord.send(channel, payload)
                    sent_or_present += 1
                    delivered_total += 1
                    if recent is not None:
                        recent.add(channel, note.url)
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
                "channel": channel, "payload": note.payload, "url": note.url, "kind": note.kind,
                "first": now_iso(), "tries": 1, "done": None, "lost": None, "reported": None,
            }
            record_failure(name, "temporary", f"{channel}: {exc}", note.url)
        target = state.videos if note.item_type == "video" else state.posts
        target[note.item_id] = now_iso()
        state.pending.pop(note.item_id, None)
        if note.kind != "member_post":
            log.info("%s %s %s -> %d destination(s)", "would notify" if dry_run else "notified",
                     note.kind, note.item_id, sent_or_present)
    if failed_destinations and not dry_run:
        _destination_alert(cfg, discord, state, failed_destinations)
    if not dry_run:
        _lost_alert(cfg, discord, state)

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
    log.info("done (%s): %d message(s) sent, %d already posted, %d source error(s), %d undelivered", mode,
             delivered_total, skipped, len(result.errors), send_failures)
    return 1 if send_failures else 0


def preview(cfg: Config, send: bool = False, dest_name: str = dests.MAIN) -> int:
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
        targets = [d for d in dests.resolve(cfg, make_store(cfg, discord).load()) if d.name == dest_name.lower()]
        if not targets:
            raise ConfigError(f"unknown destination: {dest_name}")
        dest = targets[0]
        for note in notes:
            channel = dest.channel_for(note.kind)
            if not channel or not dest.wants(note.kind):
                continue
            payload = render.apply_mention(note.payload, None)  # previews never ping anyone
            # Looks exactly like the real notification, but silent; the flag also keeps the
            # "already posted?" check from mistaking a preview for the real thing.
            payload["flags"] = SUPPRESS_NOTIFICATIONS
            discord.send(channel, payload)
            log.info("sent preview %s to %s", note.kind, dest.name)
    else:
        for note in notes:
            print(f"--- [preview] {note.kind}")
            print(json.dumps(note.payload, ensure_ascii=False, indent=2))
    return 1 if result.errors and not notes else 0
