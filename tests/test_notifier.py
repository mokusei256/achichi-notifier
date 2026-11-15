import json
from datetime import datetime, timedelta, timezone

import pytest

from achichi_notifier import notifier
from achichi_notifier.config import Config
from achichi_notifier.youtube import ChannelMeta, Post, PostsPage, VideoRef, classify_video


class FakeYouTube:
    calls = 0

    def __init__(self, *_args, **_kwargs):
        FakeYouTube.calls += 1
        self.has_cookies = False

    videos: list[VideoRef] = []
    posts: list[Post] = []
    published = {}

    def fetch_rss(self):
        return [r for r in self.videos if "rss" in r.sources], "Mela Ch."

    def fetch_tab(self, tab):
        return [r for r in self.videos if tab in r.sources], ChannelMeta("Mela Ch.", None, None)

    def fetch_video_info(self, ref):
        player = {"videoDetails": {"title": ref.title, "isLiveContent": "streams" in ref.sources,
                                   "isUpcoming": ref.live_hint == "upcoming"},
                  "microformat": {"playerMicroformatRenderer": {
                      "publishDate": self.published.get(ref.video_id, datetime.now(timezone.utc)).isoformat()}}}
        return classify_video(player, ref)

    def fetch_posts(self):
        return PostsPage(list(self.posts), False, None)


def _cfg(tmp_path, **overrides):
    values = dict(
        discord_token=None, channel_ids={k: "1" for k in ("video", "short", "live", "post")},
        state_backend="file", state_channel_id=None, state_file=tmp_path / "state.json",
        youtube_channel_id="UC", youtube_handle="@x", cookies_file=None, mentions={},
        fallback_after_minutes=15, max_age_hours=72.0, max_new_per_run=10,
    )
    values.update(overrides)
    return Config(**values)


@pytest.fixture(autouse=True)
def fake_youtube(monkeypatch):
    FakeYouTube.calls = 0
    FakeYouTube.videos = [VideoRef("old00000001", {"rss", "videos"}, title="old")]
    FakeYouTube.posts = [Post("UgkxOLD", text="old", published_text="3 日前")]
    FakeYouTube.published = {}
    monkeypatch.setattr(notifier, "YouTubeClient", FakeYouTube)


def _run(cfg, capsys, role="primary"):
    rc = notifier.run(cfg, role=role, dry_run=True)
    out = capsys.readouterr().out
    return rc, [line for line in out.splitlines() if line.startswith("--- [dry-run]")]


def test_first_run_seeds_without_notifying_then_detects_new(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    rc, notes = _run(cfg, capsys)
    assert rc == 0 and notes == []

    FakeYouTube.videos = [
        VideoRef("slot0000001", {"streams", "rss"}, title="枠", live_hint="upcoming"),
        VideoRef("short000001", {"shorts", "rss"}, is_short=True, title="short"),
        *FakeYouTube.videos,
    ]
    FakeYouTube.posts = [Post("UgkxMEM", text="secret", members_only=True, published_text="2 分前"),
                         Post("UgkxNEW", text="hello", published_text="10 分前"), *FakeYouTube.posts]
    rc, notes = _run(cfg, capsys)
    kinds = sorted(n.split()[2] for n in notes)
    assert kinds == ["live_upcoming", "member_post", "post", "short"]

    rc, notes = _run(cfg, capsys)  # nothing new the third time
    assert notes == []


def test_old_items_are_marked_seen_silently(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _run(cfg, capsys)
    FakeYouTube.videos = [VideoRef("revived0001", {"rss"}, title="archive moved back into feed")]
    FakeYouTube.published = {"revived0001": datetime.now(timezone.utc) - timedelta(days=10)}
    FakeYouTube.posts = [Post("UgkxANCIENT", text="x", published_text="2 週間前")]
    _, notes = _run(cfg, capsys)
    assert notes == []
    state = json.loads((tmp_path / "state.json").read_text())
    assert "revived0001" in state["videos"] and "UgkxANCIENT" in state["posts"]


def test_fallback_skips_while_primary_is_healthy(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _run(cfg, capsys, role="primary")
    FakeYouTube.calls = 0
    _run(cfg, capsys, role="fallback")
    assert FakeYouTube.calls == 1  # client constructed, but no fetch happened
    state = json.loads((tmp_path / "state.json").read_text())
    assert "fallback" not in state["runners"]

    stale = (datetime.now(timezone.utc) - timedelta(minutes=30)).replace(microsecond=0).isoformat()
    state["runners"]["primary"]["last_success"] = stale
    (tmp_path / "state.json").write_text(json.dumps(state))
    _run(cfg, capsys, role="fallback")
    state = json.loads((tmp_path / "state.json").read_text())
    assert "last_success" in state["runners"]["fallback"]
    assert state["runners"]["primary"]["last_success"] == stale
