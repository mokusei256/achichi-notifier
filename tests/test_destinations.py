import json

import pytest

from achichi_notifier import destinations as dests
from achichi_notifier import dest_cli, notifier
from achichi_notifier.state import FileStateStore, State
from achichi_notifier.youtube import Post, VideoRef

from test_notifier import FakeDiscordClient, FakeYouTube, _cfg, _state, fake_youtube  # noqa: F401


def test_resolve_main_and_stored_destinations(tmp_path):
    cfg = _cfg(tmp_path, mentions={"live": "<@&1>"})
    state = State(destinations={
        "big": {"channels": {"default": "200", "live": "201"}, "mentions": {"post": "<@&9>"},
                "kinds": ["live", "post"], "enabled": True},
        "off": {"channels": {"default": "300"}, "enabled": False},
        "main": {"channels": {"default": "999"}},  # cannot shadow .env
    })
    resolved = {d.name: d for d in dests.resolve(cfg, state)}
    assert list(resolved) == ["main", "big", "off"]
    big, main = resolved["big"], resolved["main"]
    assert big.channel_for("live_upcoming") == "201" and big.channel_for("post") == "200"
    assert big.wants("live_now") and big.wants("post") and not big.wants("video") and not big.wants("member_post")
    assert big.mention_for("member_post") == "<@&9>"  # falls back to the post mention
    assert main.channel_for("video") == "1" and main.mention_for("live_upcoming") == "<@&1>"
    assert not resolved["off"].wants("video")


def test_destinations_merge_newest_list_wins():
    runner = State(destinations={"old": {"channels": {"default": "1"}}},
                   destinations_updated="2026-11-15T00:00:00+00:00")
    fresh = State(destinations={"new": {"channels": {"default": "2"}}},
                  destinations_updated="2026-11-16T00:00:00+00:00")
    runner.merge_from(fresh)
    assert list(runner.destinations) == ["new"]
    cli_edit = State(destinations={"edited": {"channels": {"default": "3"}}},
                     destinations_updated="2026-11-17T00:00:00+00:00")
    cli_edit.merge_from(fresh)  # an older stored copy must not undo a newer edit
    assert list(cli_edit.destinations) == ["edited"]


@pytest.fixture
def wired_dests(monkeypatch, tmp_path):
    FakeDiscordClient.instances = []
    monkeypatch.setattr(notifier, "DiscordClient", FakeDiscordClient)
    monkeypatch.setattr(notifier, "make_store", lambda cfg, discord: FileStateStore(tmp_path / "state.json"))
    return _cfg(tmp_path, discord_token="t", state_backend="discord", state_channel_id="999",
                mentions={"video": "<@&10>"})


def _add_dest(tmp_path, name, data):
    store = FileStateStore(tmp_path / "state.json")
    state = store.load()
    state.destinations[name] = data
    state.destinations_updated = "2099-01-01T00:00:00+00:00"
    store.save(state)


def test_each_destination_gets_its_own_mention_and_kinds(wired_dests, tmp_path):
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "big", {"channels": {"default": "500"}, "mentions": {"video": "@everyone"},
                                "kinds": ["video"], "enabled": True})
    FakeYouTube.videos = [VideoRef("newvideo001", {"rss", "videos"}, title="new"), *FakeYouTube.videos]
    FakeYouTube.public_posts = [Post("UgkxNEWPUB", text="hi", published_text="1 分前"), *FakeYouTube.public_posts]
    notifier.run(wired_dests, role="primary")
    sent = [(c, p["content"]) for d in FakeDiscordClient.instances for c, p in d.sent]
    assert ("1", "<@&10> 🎬 新しい動画が公開されました") in sent
    assert ("500", "@everyone 🎬 新しい動画が公開されました") in sent
    assert not any(c == "500" and "投稿" in content for c, content in sent)  # big only wants videos
    assert any(c == "1" and "投稿" in content for c, content in sent)


class FailingFor(FakeDiscordClient):
    bad: dict = {}  # channel -> HTTP status to fail with

    def send(self, channel_id, payload):
        if channel_id in self.bad:
            from achichi_notifier.discord_api import DiscordError
            status = self.bad[channel_id]
            raise DiscordError(f"POST /channels/{channel_id}/messages -> HTTP {status}", status=status)
        return super().send(channel_id, payload)

    def recent_urls(self, channel_id, limit=50):
        return {p["embeds"][0]["url"] for d in FakeDiscordClient.instances
                for c, p in d.sent if c == channel_id and p.get("embeds")}


def test_permanent_failure_marks_seen_and_alerts_total_failure_retries(monkeypatch, wired_dests, tmp_path):
    monkeypatch.setattr(notifier, "DiscordClient", FailingFor)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "big", {"channels": {"default": "500"}, "enabled": True})
    FailingFor.bad = {"500": 403}
    FakeYouTube.videos = [VideoRef("partial0001", {"rss", "videos"}, title="p"), *FakeYouTube.videos]
    assert notifier.run(wired_dests, role="primary") == 0
    state = _state(tmp_path)
    assert "partial0001" in state["videos"] and "destination:big" in state["alerts"]
    alert = [p["content"] for d in FakeDiscordClient.instances for c, p in d.sent if c == "999"][-1]
    assert "権限" in alert and "partial0001" in alert

    FailingFor.bad = {"1": 403, "500": 403}
    FakeYouTube.videos = [VideoRef("allfail0001", {"rss", "videos"}, title="f"), *FakeYouTube.videos]
    assert notifier.run(wired_dests, role="primary") == 1
    assert "allfail0001" not in _state(tmp_path)["videos"]


def test_transient_failure_is_retried_without_duplicates(monkeypatch, wired_dests, tmp_path):
    monkeypatch.setattr(notifier, "DiscordClient", FailingFor)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "big", {"channels": {"default": "500"}, "enabled": True})
    FailingFor.bad = {"1": 503}  # main has a temporary problem, big works
    FakeYouTube.videos = [VideoRef("retry000001", {"rss", "videos"}, title="r"), *FakeYouTube.videos]
    assert notifier.run(wired_dests, role="primary") == 1
    assert "retry000001" not in _state(tmp_path)["videos"]
    FailingFor.bad = {}
    assert notifier.run(wired_dests, role="primary") == 0
    sent = [c for d in FakeDiscordClient.instances for c, p in d.sent
            if p.get("embeds") and p["embeds"][0]["url"].endswith("retry000001")]
    assert sorted(sent) == ["1", "500"]  # main got it on the retry, big only once
    assert "retry000001" in _state(tmp_path)["videos"]


def test_two_destinations_on_one_channel_post_once(wired_dests, tmp_path):
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "same", {"channels": {"default": "1"}, "enabled": True})
    FakeYouTube.videos = [VideoRef("samechan001", {"rss", "videos"}, title="s"), *FakeYouTube.videos]
    notifier.run(wired_dests, role="primary")
    sent = [c for d in FakeDiscordClient.instances for c, p in d.sent
            if p.get("embeds") and p["embeds"][0]["url"].endswith("samechan001")]
    assert sent == ["1"]


class FakeRestClient:
    def __init__(self, token=None):
        self.sent = []

    def _request(self, method, path, **kw):
        if path == "/channels/404404404404404404":
            from achichi_notifier.discord_api import DiscordError
            raise DiscordError("GET /channels/404404404404404404 -> HTTP 403: Missing Access")
        if path.startswith("/channels/"):
            return {"name": "通知", "guild_id": "77"}
        return {"name": "大きいサーバー"}

    def messages(self, channel_id, limit=100, before=None):
        return []

    def send(self, channel_id, payload):
        self.sent.append(channel_id)
        return {"id": "1"}


class Args:
    def __init__(self, **kw):
        defaults = {f"{g}_channel": None for g in dests.CHANNEL_GROUPS}
        defaults.update({f"mention_{k}": None for k in dests.KIND_CHOICES})
        defaults.update(kinds=None)
        defaults.update(kw)
        self.__dict__.update(defaults)


def test_cli_add_list_disable_remove(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(dest_cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(dest_cli, "DiscordClient", FakeRestClient)
    monkeypatch.setattr(notifier, "make_store", lambda cfg, discord: FileStateStore(tmp_path / "state.json"))
    cfg = _cfg(tmp_path, discord_token="t")
    assert dest_cli.cmd_add(cfg, Args(name="Big", channel="123456789012345678", kinds="live,post",
                                      mention_live="<@&5>")) == 0
    stored = json.loads((tmp_path / "state.json").read_text())["destinations"]["big"]
    assert stored["channels"] == {"default": "123456789012345678"}
    assert stored["kinds"] == ["live", "post"] and stored["mentions"] == {"live": "<@&5>"}
    assert dest_cli.cmd_list(cfg) == 0 and "big" in capsys.readouterr().out
    assert dest_cli.cmd_enable(cfg, "big", False) == 0
    assert json.loads((tmp_path / "state.json").read_text())["destinations"]["big"]["enabled"] is False
    assert dest_cli.cmd_remove(cfg, "big") == 0
    assert json.loads((tmp_path / "state.json").read_text())["destinations"] == {}


def test_cli_add_rejects_bad_input_and_invisible_channels(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(dest_cli.time, "sleep", lambda s: None)
    from achichi_notifier.config import ConfigError
    monkeypatch.setattr(dest_cli, "DiscordClient", FakeRestClient)
    monkeypatch.setattr(notifier, "make_store", lambda cfg, discord: FileStateStore(tmp_path / "state.json"))
    cfg = _cfg(tmp_path, discord_token="t")
    with pytest.raises(ConfigError):
        dest_cli.cmd_add(cfg, Args(name="main", channel="123456789012345678"))
    with pytest.raises(ConfigError):
        dest_cli.cmd_add(cfg, Args(name="ok", channel="not-a-number"))
    with pytest.raises(ConfigError):
        dest_cli.cmd_add(cfg, Args(name="ok", channel="123456789012345678", kinds="video,bogus"))
    assert dest_cli.cmd_add(cfg, Args(name="hidden", channel="404404404404404404")) == 1
    assert not (tmp_path / "state.json").exists() or "hidden" not in json.loads(
        (tmp_path / "state.json").read_text()).get("destinations", {})


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    monkeypatch.setattr(dest_cli, "DiscordClient", FakeRestClient)
    monkeypatch.setattr(dest_cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(notifier, "make_store", lambda cfg, discord: FileStateStore(tmp_path / "state.json"))
    return _cfg(tmp_path, discord_token="t")


def test_re_adding_keeps_options_you_did_not_pass(cli_env, tmp_path):
    assert dest_cli.cmd_add(cli_env, Args(name="big", channel="123456789012345678", kinds="video,post",
                                          mention_post="<@&7>")) == 0
    assert dest_cli.cmd_enable(cli_env, "BIG", False) == 0  # names are case-insensitive
    assert dest_cli.cmd_add(cli_env, Args(name="big", channel="223456789012345678", mention_live="<@&8>")) == 0
    stored = json.loads((tmp_path / "state.json").read_text())
    big = stored["destinations"]["big"]
    assert big["channels"] == {"default": "223456789012345678"}
    assert big["kinds"] == ["post", "video"] and big["enabled"] is False
    assert big["mentions"] == {"post": "<@&7>", "live": "<@&8>"}
    assert stored["destinations_updated"]


def test_cli_warns_about_runners_on_an_old_version(cli_env, tmp_path, capsys):
    from achichi_notifier.state import now_iso
    store = FileStateStore(tmp_path / "state.json")
    state = State(runners={"fallback": {"last_run": now_iso()},
                           "primary": {"last_run": now_iso(), "features": ["destinations"]}})
    store.save(state)
    dest_cli.cmd_list(cli_env)
    out = capsys.readouterr().out
    assert "fallback の PC は古い版" in out and "primary の PC は古い版" not in out


def test_dest_commands_through_the_command_line(cli_env, tmp_path, monkeypatch, capsys):
    from achichi_notifier import __main__ as cli
    from achichi_notifier.config import Config
    monkeypatch.setattr(Config, "from_env", classmethod(lambda c: cli_env))
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    rc = cli.main(["--env-file", str(env), "dest", "add", "Big", "--channel", "123456789012345678",
                   "--live-channel", "323456789012345678", "--kinds", "live", "--mention-live", "@everyone"])
    assert rc == 0
    big = json.loads((tmp_path / "state.json").read_text())["destinations"]["big"]
    assert big["channels"] == {"default": "123456789012345678", "live": "323456789012345678"}
    assert big["kinds"] == ["live"] and big["mentions"] == {"live": "@everyone"}
    assert cli.main(["--env-file", str(env), "dest", "list"]) == 0
    assert "live=323456789012345678" in capsys.readouterr().out
    assert cli.main(["--env-file", str(env), "dest", "check", "big"]) == 0
    assert cli.main(["--env-file", str(env), "dest", "remove", "big"]) == 0


def test_mentions_actually_ping_only_where_configured(wired_dests, tmp_path):
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "quiet", {"channels": {"default": "600"}, "kinds": ["video"], "enabled": True})
    FakeYouTube.videos = [VideoRef("pingtest001", {"rss", "videos"}, title="p"), *FakeYouTube.videos]
    notifier.run(wired_dests, role="primary")
    payloads = {c: p for d in FakeDiscordClient.instances for c, p in d.sent if p.get("embeds")}
    assert payloads["1"]["allowed_mentions"] == {"parse": ["roles", "users", "everyone"]}
    assert payloads["600"]["allowed_mentions"] == {"parse": []}
    assert payloads["600"]["content"].startswith("🎬")


def test_recent_link_guard_is_per_channel(monkeypatch, wired_dests, tmp_path):
    class Recent(FakeDiscordClient):
        def recent_urls(self, channel_id, limit=50):
            return {"https://www.youtube.com/watch?v=perchannel1"} if channel_id == "1" else set()

    monkeypatch.setattr(notifier, "DiscordClient", Recent)
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "big", {"channels": {"default": "500"}, "enabled": True})
    FakeYouTube.videos = [VideoRef("perchannel1", {"rss", "videos"}, title="x"), *FakeYouTube.videos]
    notifier.run(wired_dests, role="primary")
    sent = [c for d in FakeDiscordClient.instances for c, p in d.sent if p.get("embeds")]
    assert sent == ["500"]


def test_destination_alert_is_throttled(monkeypatch, wired_dests, tmp_path):
    monkeypatch.setattr(notifier, "DiscordClient", FailingFor)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "big", {"channels": {"default": "500"}, "enabled": True})
    FailingFor.bad = {"500": 403}
    for i in range(2):
        FakeYouTube.videos = [VideoRef(f"throttle{i:03d}", {"rss", "videos"}, title="t"), *FakeYouTube.videos]
        notifier.run(wired_dests, role="primary")
    alerts = [p for d in FakeDiscordClient.instances for c, p in d.sent if c == "999"]
    assert len(alerts) == 1
