import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from achichi_notifier import notifier
from achichi_notifier.config import Config
from dataclasses import replace

from achichi_notifier.state import FileStateStore, State
from achichi_notifier.youtube import (
    ChannelMeta, Post, PostsPage, VideoRef, YouTubeError, classify_video,
)

SECRET_TEXT = "ひみつの日記の本文"


class FakeYouTube:
    """Stands in for YouTubeClient; configured through class attributes per test."""

    videos: list = []
    published: dict = {}
    public_posts: list = []
    member_posts: list | None = None  # None -> no cookies configured
    member_logged_in = True
    membership = "member"
    member_valid = True
    cookie_error: str | None = None
    public_fails = False
    public_valid = True
    public_logged_in = False
    member_fails = False
    rss_fails = False
    failing_tabs: set = set()
    calls: list = []

    def __init__(self):
        self.cookies_configured = FakeYouTube.member_posts is not None or FakeYouTube.cookie_error is not None
        self.cookie_error = FakeYouTube.cookie_error
        self.has_cookies = FakeYouTube.member_posts is not None and FakeYouTube.cookie_error is None

    @classmethod
    def from_config(cls, cfg):
        return cls()

    def fetch_rss(self):
        FakeYouTube.calls.append("rss")
        if self.rss_fails:
            raise YouTubeError("GET /feeds/videos.xml -> HTTP 404")
        return [r for r in self.videos if "rss" in r.sources], "Mela Ch."

    def fetch_tab(self, tab):
        FakeYouTube.calls.append(tab)
        if tab in self.failing_tabs:
            raise YouTubeError(f"ytInitialData not found on /channel/UC/{tab}")
        return [r for r in self.videos if tab in r.sources], ChannelMeta("Mela Ch.", None, None)

    def fetch_video_info(self, ref):
        published = self.published.get(ref.video_id, datetime.now(timezone.utc))
        player = {"videoDetails": {"title": ref.title, "isLiveContent": "streams" in ref.sources,
                                   "isUpcoming": ref.live_hint == "upcoming"},
                  "microformat": {"playerMicroformatRenderer": {"publishDate": published.isoformat()}}}
        return classify_video(player, ref)

    def fetch_posts_page(self, member=False):
        FakeYouTube.calls.append("posts-member" if member else "posts-public")
        if member:
            if self.member_fails:
                raise YouTubeError("GET /channel/UC/posts -> HTTP 429")
            return PostsPage(list(self.member_posts), self.member_logged_in, None,
                             valid=self.member_valid, membership=self.membership)
        if self.public_fails:
            raise YouTubeError("ytInitialData not found on /posts")
        return PostsPage(list(self.public_posts), self.public_logged_in, None, valid=self.public_valid)


def _cfg(tmp_path, **overrides):
    values = dict(
        discord_token=None, channel_ids={k: "1" for k in ("video", "short", "live", "post")},
        state_backend="file", state_channel_id=None, state_file=tmp_path / "state.json",
        youtube_channel_id="UC", youtube_handle="@x", cookies_file=None, cookies_secret=None,
        mentions={}, fallback_after_minutes=15, max_age_hours=72.0, max_new_per_run=10,
        member_check_minutes=10,
    )
    values.update(overrides)
    return Config(**values)


@pytest.fixture(autouse=True)
def fake_youtube(monkeypatch):
    FakeYouTube.videos = [VideoRef("old00000001", {"rss", "videos"}, title="old")]
    FakeYouTube.published = {}
    FakeYouTube.public_posts = [Post("UgkxOLD", text="old", published_text="3 日前")]
    FakeYouTube.member_posts = None
    FakeYouTube.member_logged_in = True
    FakeYouTube.membership = "member"
    FakeYouTube.member_valid = True
    FakeYouTube.cookie_error = None
    FakeYouTube.public_fails = False
    FakeYouTube.public_valid = True
    FakeYouTube.public_logged_in = False
    FakeYouTube.member_fails = False
    FakeYouTube.rss_fails = False
    FakeYouTube.failing_tabs = set()
    FakeYouTube.calls = []
    monkeypatch.setattr(notifier, "YouTubeClient", FakeYouTube)


def _run(cfg, capsys, role="primary"):
    rc = notifier.run(cfg, role=role, dry_run=True)
    out = capsys.readouterr().out
    return rc, out, [line.split()[2] for line in out.splitlines() if line.startswith("--- [dry-run]")]


def _state(tmp_path):
    return json.loads((tmp_path / "state.json").read_text())


def _write_state(tmp_path, state):
    (tmp_path / "state.json").write_text(json.dumps(state))


def test_first_run_seeds_without_notifying_then_detects_new(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    rc, _, kinds = _run(cfg, capsys)
    assert rc == 0 and kinds == []

    FakeYouTube.videos = [
        VideoRef("slot0000001", {"streams", "rss"}, title="枠", live_hint="upcoming"),
        VideoRef("short000001", {"shorts", "rss"}, is_short=True, title="short"),
        *FakeYouTube.videos,
    ]
    FakeYouTube.public_posts = [Post("UgkxNEW", text="hello", published_text="10 分前"),
                                *FakeYouTube.public_posts]
    _, _, kinds = _run(cfg, capsys)
    assert sorted(kinds) == ["live_upcoming", "post", "short"]
    _, _, kinds = _run(cfg, capsys)
    assert kinds == []


def test_old_items_are_marked_seen_silently(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _run(cfg, capsys)
    FakeYouTube.videos = [VideoRef("revived0001", {"rss"}, title="archive moved back into feed")]
    FakeYouTube.published = {"revived0001": datetime.now(timezone.utc) - timedelta(days=10)}
    FakeYouTube.public_posts = [Post("UgkxANCIENT", text="x", published_text="2 週間前")]
    _, _, kinds = _run(cfg, capsys)
    assert kinds == []
    state = _state(tmp_path)
    assert "revived0001" in state["videos"] and "UgkxANCIENT" in state["posts"]


def test_fallback_skips_while_primary_is_healthy_without_cookies(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _run(cfg, capsys, role="primary")
    FakeYouTube.calls = []
    _run(cfg, capsys, role="fallback")
    assert FakeYouTube.calls == []
    assert "fallback" not in _state(tmp_path)["runners"]

    state = _state(tmp_path)
    stale = (datetime.now(timezone.utc) - timedelta(minutes=30)).replace(microsecond=0).isoformat()
    state["runners"]["primary"]["last_success"] = stale
    _write_state(tmp_path, state)
    _run(cfg, capsys, role="fallback")
    state = _state(tmp_path)
    assert "last_success" in state["runners"]["fallback"]
    assert state["runners"]["primary"]["last_success"] == stale


def _reset_member_schedule(tmp_path):
    state = _state(tmp_path)
    state["runners"].setdefault("fallback", {})["last_member_check"] = "2000-01-01T00:00:00+00:00"
    _write_state(tmp_path, state)


def _seed_with_primary(cfg, capsys):
    """Primary (no cookies) runs first, as in production."""
    member_posts = FakeYouTube.member_posts
    FakeYouTube.member_posts = None
    _run(cfg, capsys, role="primary")
    FakeYouTube.member_posts = member_posts


def test_fallback_members_mode_announces_only_members_posts(tmp_path, capsys, caplog):
    cfg = _cfg(tmp_path)
    public_new = Post("UgkxPUB", text="公開の告知", published_text="5 分前")
    FakeYouTube.member_posts = [Post("UgkxMEMOLD", text="old secret", members_only=True,
                                     published_text="2 日前"), *FakeYouTube.public_posts]
    _seed_with_primary(cfg, capsys)

    # First members-only check seeds silently.
    _, _, kinds = _run(cfg, capsys, role="fallback")
    assert kinds == []
    state = _state(tmp_path)
    assert "UgkxMEMOLD" in state["posts"] and "last_success" not in state["runners"].get("fallback", {})

    # New members-only post (badge missing -> detected by logged-out comparison) + new public post.
    FakeYouTube.public_posts = [public_new, *FakeYouTube.public_posts]
    FakeYouTube.member_posts = [Post("UgkxDIARY", text=SECRET_TEXT, image_url="https://img/secret",
                                     published_text="3 分前"), public_new, *FakeYouTube.member_posts]
    state = _state(tmp_path)
    state["runners"]["fallback"]["last_member_check"] = "2000-01-01T00:00:00+00:00"
    _write_state(tmp_path, state)
    FakeYouTube.calls = []
    with caplog.at_level(logging.DEBUG):
        _, out, kinds = _run(cfg, capsys, role="fallback")
    assert kinds == []  # no badge: held until a second check confirms it
    state = _state(tmp_path)
    assert "UgkxDIARY" in state["pending"] and "UgkxDIARY" not in state["posts"]
    assert FakeYouTube.calls.index("posts-member") < FakeYouTube.calls.index("posts-public")

    _reset_member_schedule(tmp_path)
    with caplog.at_level(logging.DEBUG):
        _, out, kinds = _run(cfg, capsys, role="fallback")
    assert kinds == ["member_post"]  # the public post is left to the primary runner
    assert SECRET_TEXT not in out and "https://img/secret" not in out
    assert SECRET_TEXT not in caplog.text
    state = _state(tmp_path)
    assert "UgkxDIARY" in state["posts"] and "UgkxDIARY" not in state["pending"]
    assert "UgkxPUB" not in state["posts"]

    # The primary then announces the public post with its content.
    _, out, kinds = _run(cfg, capsys, role="primary")
    assert kinds == ["post"] and "公開の告知" in out


def test_member_check_respects_interval(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    FakeYouTube.member_posts = list(FakeYouTube.public_posts)
    _seed_with_primary(cfg, capsys)
    _run(cfg, capsys, role="fallback")
    FakeYouTube.calls = []
    _run(cfg, capsys, role="fallback")  # within MEMBER_CHECK_MINUTES
    assert FakeYouTube.calls == []


def test_fallback_only_self_checks_daily_when_primary_has_working_cookies(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    FakeYouTube.member_posts = list(FakeYouTube.public_posts)
    _run(cfg, capsys, role="primary")  # primary with cookies
    assert "last_member_success" in _state(tmp_path)["runners"]["primary"]
    FakeYouTube.calls = []
    _run(cfg, capsys, role="fallback")  # first time: daily self-check of the spare cookies
    assert "posts-member" in FakeYouTube.calls
    FakeYouTube.calls = []
    _run(cfg, capsys, role="fallback")  # within 24h: nothing to do
    assert FakeYouTube.calls == []
    state = _state(tmp_path)
    state["runners"]["fallback"]["last_member_check"] = "2000-01-01T00:00:00+00:00"
    _write_state(tmp_path, state)
    _run(cfg, capsys, role="fallback")  # a day later: checks again
    assert "posts-member" in FakeYouTube.calls


def test_public_page_failure_holds_unbadged_member_posts(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    FakeYouTube.member_posts = list(FakeYouTube.public_posts)
    _seed_with_primary(cfg, capsys)
    _run(cfg, capsys, role="fallback")  # seeds posts_member

    FakeYouTube.member_posts = [
        Post("UgkxBADGE", text=SECRET_TEXT, members_only=True, published_text="1 分前"),
        Post("UgkxNOBADGE", text=SECRET_TEXT, published_text="2 分前"),
        *FakeYouTube.public_posts,
    ]
    FakeYouTube.public_fails = True
    state = _state(tmp_path)
    state["runners"]["fallback"]["last_member_check"] = "2000-01-01T00:00:00+00:00"
    _write_state(tmp_path, state)
    _, out, kinds = _run(cfg, capsys, role="fallback")
    assert kinds == ["member_post"] and SECRET_TEXT not in out
    state = _state(tmp_path)
    assert "UgkxBADGE" in state["posts"] and "UgkxNOBADGE" not in state["posts"]

    FakeYouTube.public_fails = False
    _reset_member_schedule(tmp_path)
    _, out, kinds = _run(cfg, capsys, role="fallback")
    assert kinds == [] and "UgkxNOBADGE" in _state(tmp_path)["pending"]
    _reset_member_schedule(tmp_path)
    _, out, kinds = _run(cfg, capsys, role="fallback")
    assert kinds == ["member_post"] and SECRET_TEXT not in out
    assert "UgkxNOBADGE" in _state(tmp_path)["posts"]


def test_first_member_check_does_not_swallow_unannounced_public_post(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    FakeYouTube.member_posts = list(FakeYouTube.public_posts)
    _seed_with_primary(cfg, capsys)
    late = Post("UgkxLATE", text="公開", published_text="1 分前")
    FakeYouTube.public_posts = [late, *FakeYouTube.public_posts]
    FakeYouTube.member_posts = [late, *FakeYouTube.member_posts]
    _run(cfg, capsys, role="fallback")  # first members-only check (seed)
    assert "UgkxLATE" not in _state(tmp_path)["posts"]
    _, _, kinds = _run(cfg, capsys, role="primary")
    assert kinds == ["post"]


@pytest.mark.parametrize("setup, status", [
    ({"cookie_error": "cookie data is not UTF-8 text"}, "unreadable"),
    ({"member_posts": [], "member_logged_in": False}, "logged_out"),
    ({"member_posts": [], "membership": "not_member"}, "not_member"),
])
def test_cookie_problems_never_stop_other_notifications(tmp_path, capsys, setup, status):
    cfg = _cfg(tmp_path)
    _run(cfg, capsys)  # seed
    for key, value in setup.items():
        setattr(FakeYouTube, key, value)
    FakeYouTube.videos = [VideoRef("new00000001", {"rss", "videos"}, title="new"), *FakeYouTube.videos]
    rc, _, kinds = _run(cfg, capsys, role="primary")
    assert rc == 0 and kinds == ["video"]
    runner = _state(tmp_path)["runners"]["primary"]
    assert "last_success" in runner and "last_member_success" not in runner
    assert runner["last_member_check"]


class FakeDiscord:
    def __init__(self):
        self.sent = []

    def send(self, channel_id, payload):
        self.sent.append((channel_id, payload))
        return {"id": "1"}


def test_cookie_alert_is_throttled_per_runner_and_hides_details(tmp_path):
    cfg = _cfg(tmp_path, state_backend="discord", state_channel_id="999")
    discord, state = FakeDiscord(), State()
    notifier._cookie_alert(cfg, discord, state, "fallback", "logged_out", None, dry_run=False)
    notifier._cookie_alert(cfg, discord, state, "fallback", "logged_out", None, dry_run=False)
    assert len(discord.sent) == 1
    channel, payload = discord.sent[0]
    assert channel == "999" and "install.sh" in payload["content"]
    notifier._cookie_alert(cfg, discord, state, "primary", "logged_out", None, dry_run=False)
    assert len(discord.sent) == 2
    notifier._cookie_alert(cfg, discord, state, "fallback", "ok", None, dry_run=False)
    notifier._cookie_alert(cfg, discord, state, "fallback", "fetch_failed", "x", dry_run=False)
    assert len(discord.sent) == 2
    notifier._cookie_alert(cfg, FakeDiscord(), State(), "fallback", "not_member", None, dry_run=True)


def _seeded_members(tmp_path, capsys, cfg):
    FakeYouTube.member_posts = list(FakeYouTube.public_posts)
    _seed_with_primary(cfg, capsys)
    _run(cfg, capsys, role="fallback")
    assert "posts_member" in _state(tmp_path)["seeded"]
    _reset_member_schedule(tmp_path)


def test_empty_logged_out_list_is_not_used_for_comparison(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _seeded_members(tmp_path, capsys, cfg)
    FakeYouTube.member_posts = [Post("UgkxPUBNEW", text="公開", published_text="1 分前"),
                                *FakeYouTube.member_posts]
    FakeYouTube.public_posts = []  # continuation-only / lagging logged-out page
    _, _, kinds = _run(cfg, capsys, role="fallback")
    state = _state(tmp_path)
    assert kinds == [] and "UgkxPUBNEW" not in state["pending"] and "UgkxPUBNEW" not in state["posts"]


def test_badge_post_is_announced_immediately_and_old_ones_silently(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _seeded_members(tmp_path, capsys, cfg)
    FakeYouTube.member_posts = [
        Post("UgkxNEWBADGE", text=SECRET_TEXT, members_only=True, published_text="2 分前"),
        Post("UgkxOLDBADGE", text=SECRET_TEXT, members_only=True, published_text="4 日前"),
        *FakeYouTube.member_posts,
    ]
    _, out, kinds = _run(cfg, capsys, role="fallback")
    assert kinds == ["member_post"] and SECRET_TEXT not in out
    assert {"UgkxNEWBADGE", "UgkxOLDBADGE"} <= set(_state(tmp_path)["posts"])


@pytest.mark.parametrize("setup, detail", [
    ({"member_valid": False}, "no post list"),
    ({"member_posts": [Post("UgkxOTHER", text="x", members_only=True)]}, "missing posts"),
])
def test_untrustworthy_member_view_is_not_success_and_not_seeded(tmp_path, capsys, caplog, setup, detail):
    cfg = _cfg(tmp_path)
    FakeYouTube.member_posts = list(FakeYouTube.public_posts)
    _seed_with_primary(cfg, capsys)
    for key, value in setup.items():
        setattr(FakeYouTube, key, value)
    with caplog.at_level(logging.WARNING):
        _run(cfg, capsys, role="fallback")
    state = _state(tmp_path)
    assert "posts_member" not in state["seeded"]
    assert "last_member_success" not in state["runners"]["fallback"]
    assert state["runners"]["fallback"]["member_fail_streak"] == 1
    assert detail in caplog.text


def test_not_member_view_is_never_seeded(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    FakeYouTube.member_posts = list(FakeYouTube.public_posts)
    FakeYouTube.membership = "not_member"
    _seed_with_primary(cfg, capsys)
    _run(cfg, capsys, role="fallback")
    assert "posts_member" not in _state(tmp_path)["seeded"]


def test_full_mode_with_cookies_never_double_announces(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    FakeYouTube.member_posts = list(FakeYouTube.public_posts)
    _run(cfg, capsys)  # primary with cookies seeds both scopes
    both = Post("UgkxBOTH", text=SECRET_TEXT, members_only=True, published_text="1 分前")
    FakeYouTube.public_posts = [replace(both, members_only=False), *FakeYouTube.public_posts]
    FakeYouTube.member_posts = [both, *FakeYouTube.member_posts]
    _, out, kinds = _run(cfg, capsys)
    assert kinds == ["member_post"] and SECRET_TEXT not in out


class FakeDiscordClient:
    instances: list = []

    def __init__(self, token=None):
        self.sent = []
        self.recent: set = set()
        FakeDiscordClient.instances.append(self)

    def me(self):
        return {"id": "bot"}

    def send(self, channel_id, payload):
        self.sent.append((channel_id, payload))
        return {"id": str(len(self.sent))}

    def recent_urls(self, channel_id, limit=50):
        return set(self.recent)


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """run() with a fake Discord client, Discord-style config and a local state file."""
    FakeDiscordClient.instances = []
    monkeypatch.setattr(notifier, "DiscordClient", FakeDiscordClient)
    monkeypatch.setattr(notifier, "make_store", lambda cfg, discord: FileStateStore(tmp_path / "state.json"))
    return _cfg(tmp_path, discord_token="t", state_backend="discord", state_channel_id="999")


def test_run_sends_cookie_alert_once_per_day(wired, tmp_path, capsys):
    FakeYouTube.member_posts, FakeYouTube.member_logged_in = [], False
    for _ in range(2):
        notifier.run(wired, role="primary")
    alerts = [p for c, p in FakeDiscordClient.instances[-1].sent + FakeDiscordClient.instances[0].sent
              if c == "999"]
    assert len(alerts) == 1 and "失効" in alerts[0]["content"]


def test_fetch_failures_alert_only_after_a_streak(wired, tmp_path, capsys):
    FakeYouTube.member_posts = list(FakeYouTube.public_posts)
    FakeYouTube.member_valid = False
    sent = []
    for _ in range(notifier.FETCH_FAIL_ALERT_STREAK):
        notifier.run(wired, role="primary")
        sent.append(sum(1 for d in FakeDiscordClient.instances for c, _ in d.sent if c == "999"))
    assert sent[:-1] == [0] * (notifier.FETCH_FAIL_ALERT_STREAK - 1) and sent[-1] == 1


def test_dry_run_never_sends_alerts(wired, tmp_path, capsys):
    FakeYouTube.member_posts, FakeYouTube.member_logged_in = [], False
    notifier.run(wired, role="primary", dry_run=True)
    assert all(not d.sent for d in FakeDiscordClient.instances)


def test_link_already_in_channel_is_not_sent_again(wired, tmp_path, capsys):
    notifier.run(wired, role="primary")  # seed
    FakeYouTube.videos = [VideoRef("dup00000001", {"rss", "videos"}, title="dup"), *FakeYouTube.videos]
    original_init = FakeDiscordClient.__init__

    def init_with_recent(self, token=None):
        original_init(self, token)
        self.recent = {"https://www.youtube.com/watch?v=dup00000001"}

    FakeDiscordClient.__init__ = init_with_recent
    try:
        notifier.run(wired, role="primary")
    finally:
        FakeDiscordClient.__init__ = original_init
    assert not FakeDiscordClient.instances[-1].sent
    assert "dup00000001" in _state(tmp_path)["videos"]


def test_public_logs_do_not_reveal_members_only_details(tmp_path, capsys, caplog, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    cfg = _cfg(tmp_path)
    FakeYouTube.member_posts = list(FakeYouTube.public_posts)
    _run(cfg, capsys)
    FakeYouTube.member_posts = [Post("UgkxSECRETID", text=SECRET_TEXT, members_only=True,
                                     published_text="1 分前"), *FakeYouTube.member_posts]
    with caplog.at_level(logging.INFO):
        _run(cfg, capsys)
    FakeYouTube.member_logged_in = False
    with caplog.at_level(logging.INFO):
        _run(cfg, capsys)
    assert "UgkxSECRETID" not in caplog.text and SECRET_TEXT not in caplog.text
    assert "logged_out" not in caplog.text


def test_backlog_without_badge_at_first_comparison_is_settled_silently(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    FakeYouTube.member_posts = [Post("UgkxBACKLOG", text=SECRET_TEXT, published_text="1 日前"),
                                *FakeYouTube.public_posts]
    _seed_with_primary(cfg, capsys)
    for _ in range(3):
        _, out, kinds = _run(cfg, capsys, role="fallback")
        assert kinds == [] and SECRET_TEXT not in out
        _reset_member_schedule(tmp_path)
    state = _state(tmp_path)
    assert "UgkxBACKLOG" in state["posts"] and "UgkxBACKLOG" not in state["pending"]


def test_badge_baseline_does_not_wait_for_logged_out_page(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    FakeYouTube.member_posts = list(FakeYouTube.public_posts)
    _seed_with_primary(cfg, capsys)
    FakeYouTube.public_fails = True
    _run(cfg, capsys, role="fallback")
    state = _state(tmp_path)
    assert "posts_member" in state["seeded"] and "posts_member_diff" not in state["seeded"]
    _reset_member_schedule(tmp_path)
    FakeYouTube.member_posts = [Post("UgkxNEWB", text=SECRET_TEXT, members_only=True,
                                     published_text="1 分前"), *FakeYouTube.member_posts]
    _, out, kinds = _run(cfg, capsys, role="fallback")
    assert kinds == ["member_post"] and SECRET_TEXT not in out


def test_public_seed_accepts_unknown_layout_with_posts_but_not_an_empty_page(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    FakeYouTube.public_valid = False
    FakeYouTube.public_posts = []
    _run(cfg, capsys)
    assert "posts" not in _state(tmp_path)["seeded"]
    FakeYouTube.public_posts = [Post("UgkxLAYOUT", text="x", published_text="1 日前")]
    _run(cfg, capsys)
    assert "UgkxLAYOUT" in _state(tmp_path)["posts"]


def test_invalid_logged_out_page_is_not_used_for_comparison(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _seeded_members(tmp_path, capsys, cfg)
    FakeYouTube.public_valid = False  # posts still parsed (unknown layout)
    FakeYouTube.member_posts = [Post("UgkxPUB2", text="公開", published_text="1 分前"),
                                *FakeYouTube.member_posts]
    for _ in range(2):
        _, _, kinds = _run(cfg, capsys, role="fallback")
        assert kinds == []
        _reset_member_schedule(tmp_path)
    assert "UgkxPUB2" not in _state(tmp_path)["posts"]


def test_logged_in_public_page_is_rejected(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _run(cfg, capsys)
    FakeYouTube.public_logged_in = True
    FakeYouTube.public_posts = [Post("UgkxHIDDEN", text=SECRET_TEXT, published_text="1 分前"),
                                *FakeYouTube.public_posts]
    _, out, kinds = _run(cfg, capsys)
    assert kinds == [] and SECRET_TEXT not in out
    assert "last_success" in _state(tmp_path)["runners"]["primary"]  # from the first run only
    first = _state(tmp_path)["runners"]["primary"]["last_success"]
    assert first <= _state(tmp_path)["runners"]["primary"]["last_run"]


@pytest.mark.parametrize("setup, text", [
    ({"cookie_error": "cookie data is not UTF-8 text"}, "読み込めません"),
    ({"member_posts": [Post("UgkxOLD", text="old")], "membership": "not_member"}, "メンバーではありません"),
])
def test_run_sends_each_cookie_alert_kind(wired, tmp_path, capsys, setup, text):
    for key, value in setup.items():
        setattr(FakeYouTube, key, value)
    notifier.run(wired, role="primary")
    alerts = [p for d in FakeDiscordClient.instances for c, p in d.sent if c == "999"]
    assert len(alerts) == 1 and text in alerts[0]["content"]


def test_member_fetch_error_is_not_a_source_error(wired, tmp_path, capsys):
    FakeYouTube.member_posts = list(FakeYouTube.public_posts)
    FakeYouTube.member_fails = True
    assert notifier.run(wired, role="primary") == 0
    runner = _state(tmp_path)["runners"]["primary"]
    assert "last_success" in runner and runner["member_fail_streak"] == 1


def test_streak_resets_on_non_failure_status(wired, tmp_path, capsys):
    FakeYouTube.member_posts = list(FakeYouTube.public_posts)
    FakeYouTube.member_fails = True
    notifier.run(wired, role="primary")
    FakeYouTube.member_fails, FakeYouTube.member_logged_in = False, False
    notifier.run(wired, role="primary")
    assert _state(tmp_path)["runners"]["primary"]["member_fail_streak"] == 0


def test_fallback_takeover_alerts_the_state_channel_once(wired, tmp_path, capsys):
    notifier.run(wired, role="primary")  # primary ran once
    state = _state(tmp_path)
    old = "2000-01-01T00:00:00+00:00"
    state["runners"]["primary"].update({"last_success": old, "last_run": old})
    _write_state(tmp_path, state)
    for _ in range(2):
        notifier.run(wired, role="fallback")
    alerts = [p for d in FakeDiscordClient.instances for c, p in d.sent if c == "999"]
    assert len(alerts) == 1 and "主系の PC が止まっています" in alerts[0]["content"]


def test_no_takeover_alert_when_no_primary_ever_ran(wired, tmp_path, capsys):
    notifier.run(wired, role="fallback")
    assert not [p for d in FakeDiscordClient.instances for c, p in d.sent if c == "999"]


def test_unexpected_errors_reach_the_log_file_without_a_console(tmp_path, monkeypatch):
    import sys
    from achichi_notifier import __main__ as cli

    def boom(*args, **kwargs):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(notifier, "run", boom)
    monkeypatch.setattr(sys, "stderr", None)
    log_path = tmp_path / "notifier.log"
    env = tmp_path / ".env"
    env.write_text("DISCORD_CHANNEL_ID=1\nSTATE_BACKEND=file\n", encoding="utf-8")
    rc = cli.main(["--env-file", str(env), "--log-file", str(log_path), "run"])
    monkeypatch.undo()
    cli._setup_logging(False, None)
    assert rc == 1
    text = log_path.read_text(encoding="utf-8")
    assert "unexpected error" in text and "Traceback" in text and "parser exploded" in text


def _backdate_primary(tmp_path) -> str:
    """Stamps are whole seconds: move the first run's into the past so a new one is visible."""
    old = "2000-01-01T00:00:00+00:00"
    state = _state(tmp_path)
    state["runners"]["primary"].update({"last_success": old, "last_run": old})
    _write_state(tmp_path, state)
    return old


def _every_tab_lists_something():
    FakeYouTube.videos = [VideoRef("old00000001", {"rss", "videos"}, title="old"),
                          VideoRef("oldshort001", {"rss", "shorts"}, title="old short"),
                          VideoRef("oldlive0001", {"streams"}, title="old stream")]


def test_rss_404_alone_still_counts_as_success(tmp_path, capsys, caplog):
    """The feed 404s now and then; the channel tabs cover it, so the run is still a success."""
    cfg = _cfg(tmp_path)
    _every_tab_lists_something()
    _run(cfg, capsys)  # seeds
    old = _backdate_primary(tmp_path)
    FakeYouTube.rss_fails = True
    FakeYouTube.videos = [VideoRef("new00000001", {"videos"}, title="new"), *FakeYouTube.videos]
    with caplog.at_level(logging.INFO, logger="achichi_notifier.notifier"):
        rc, _, kinds = _run(cfg, capsys)
    runner = _state(tmp_path)["runners"]["primary"]
    assert rc == 0 and kinds == ["video"]
    assert runner["last_success"] == runner["last_run"] != old
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "rss" in r.getMessage()]
    assert any("ignored" in r.getMessage() and "HTTP 404" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("rss_fails, failing_tabs", [(True, {"shorts"}), (False, {"streams"})])
def test_tab_failure_is_still_an_error(tmp_path, capsys, caplog, rss_fails, failing_tabs):
    cfg = _cfg(tmp_path)
    _every_tab_lists_something()
    _run(cfg, capsys)
    old = _backdate_primary(tmp_path)
    FakeYouTube.rss_fails, FakeYouTube.failing_tabs = rss_fails, failing_tabs
    with caplog.at_level(logging.WARNING, logger="achichi_notifier.notifier"):
        _run(cfg, capsys)
    runner = _state(tmp_path)["runners"]["primary"]
    assert runner["last_success"] == old and runner["last_run"] != old
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert next(iter(failing_tabs)) in warned
    assert ("rss" in warned) == rss_fails  # with a tab down, the feed's failure counts too


def test_rss_404_with_an_empty_tab_is_an_error(tmp_path, capsys, caplog):
    """A tab that parses to nothing (layout change) proves nothing, so the feed is still needed."""
    cfg = _cfg(tmp_path)
    _every_tab_lists_something()
    _run(cfg, capsys)
    old = _backdate_primary(tmp_path)
    FakeYouTube.rss_fails = True
    FakeYouTube.videos = [r for r in FakeYouTube.videos if "streams" not in r.sources]
    with caplog.at_level(logging.WARNING, logger="achichi_notifier.notifier"):
        _run(cfg, capsys)
    assert _state(tmp_path)["runners"]["primary"]["last_success"] == old
    assert any("rss" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


def test_rss_404_keeps_the_fallback_idle(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _every_tab_lists_something()
    _run(cfg, capsys)
    _backdate_primary(tmp_path)
    FakeYouTube.rss_fails = True
    _run(cfg, capsys)  # primary: RSS 404 only
    calls_before = len(FakeYouTube.calls)
    _run(cfg, capsys, role="fallback")
    assert "videos" not in FakeYouTube.calls[calls_before:]  # primary is fresh: no full run


class _CheckYouTube:
    def __init__(self, rss_fails=False, tabs=None, posts_fail=False):
        self.rss_fails, self.posts_fail, self.calls = rss_fails, posts_fail, []
        self.tabs = tabs if tabs is not None else {"videos": 1, "shorts": 1, "streams": 1}

    def fetch_tab(self, tab):
        self.calls.append(tab)
        count = self.tabs.get(tab)
        if count is None:
            raise YouTubeError(f"ytInitialData not found on /channel/UC/{tab}")
        return [VideoRef(f"{tab}{i}", {tab}) for i in range(count)], None

    def fetch_rss(self):
        self.calls.append("rss")
        if self.rss_fails:
            raise YouTubeError("GET /feeds/videos.xml -> HTTP 404")
        return [], "Mela Ch."

    def fetch_posts_page(self, member=False):
        self.calls.append("posts")
        if self.posts_fail:
            raise YouTubeError("ytInitialData not found on /posts")
        return PostsPage([Post("UgkxA", text="x")], False, None)


@pytest.mark.parametrize("yt, ok", [
    (_CheckYouTube(), True),
    (_CheckYouTube(rss_fails=True), True),  # covered by the tabs: info line, not NG
    (_CheckYouTube(rss_fails=True, tabs={"videos": 3, "shorts": 0, "streams": 2}), False),
    (_CheckYouTube(rss_fails=True, tabs={"videos": 3, "streams": 2}), False),
    (_CheckYouTube(tabs={"videos": 3, "streams": 2}), False),
    (_CheckYouTube(posts_fail=True), False),
])
def test_check_judges_sources_like_a_run(yt, ok, capsys):
    from achichi_notifier import __main__ as cli
    problems: list[str] = []
    page = cli._check_sources(yt, problems)
    out = capsys.readouterr().out
    assert (not problems) == ok, problems
    assert "posts" in yt.calls  # never skipped because the feed failed
    assert (page is None) == yt.posts_fail
    if yt.rss_fails:  # NG exactly when a tab is down too; the harmless-info line only when covered
        assert any(p.startswith("YouTube RSS:") for p in problems) == (not ok)
        assert ("通知には影響しません" in out) == ok
