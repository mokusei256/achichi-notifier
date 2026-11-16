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
    FakeDiscordClient.history = {}
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
    assert "partial0001" in state["videos"] and "destination:big:permanent" in state["alerts"]
    assert not state["undelivered"]  # a permanent error is not retried
    assert state["runners"]["primary"].get("last_success")
    alert = [p["content"] for d in FakeDiscordClient.instances for c, p in d.sent if c == "999"][-1]
    assert "権限" in alert and "partial0001" in alert and "届いていません" in alert

    FailingFor.bad = {"1": 403, "500": 403}
    FakeYouTube.videos = [VideoRef("allfail0001", {"rss", "videos"}, title="f"), *FakeYouTube.videos]
    assert notifier.run(wired_dests, role="primary") == 1
    assert "allfail0001" not in _state(tmp_path)["videos"]


class BlindFailing(FailingFor):
    """Recent-links guard cannot see the channel (no Read Message History / busy channel)."""

    def recent_urls(self, channel_id, limit=50):
        return set()


def _embed_sends(video_id):
    return sorted(c for d in FakeDiscordClient.instances for c, p in d.sent
                  if p.get("embeds") and p["embeds"][0]["url"].endswith(video_id))


def test_transient_failure_retries_only_the_missed_destination(monkeypatch, wired_dests, tmp_path):
    # Even when the duplicate guard is blind, the destination that already got the
    # notification must never get it again (R1), and the primary stays healthy (R2).
    monkeypatch.setattr(notifier, "DiscordClient", BlindFailing)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "big", {"channels": {"default": "500"}, "enabled": True})
    FailingFor.bad = {"500": 503}  # big has a temporary problem, main works
    FakeYouTube.videos = [VideoRef("retry000001", {"rss", "videos"}, title="r"), *FakeYouTube.videos]
    assert notifier.run(wired_dests, role="primary") == 0
    state = _state(tmp_path)
    assert "retry000001" in state["videos"]
    assert state["runners"]["primary"].get("last_success")
    assert "destination:big:temporary" in state["alerts"]
    assert list(state["undelivered"]) == ["big|video|retry000001"]
    for _ in range(3):  # still failing: nothing new goes to main
        notifier.run(wired_dests, role="primary")
    assert _embed_sends("retry000001") == ["1"]
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")
    notifier.run(wired_dests, role="primary")
    assert _embed_sends("retry000001") == ["1", "500"]  # each channel exactly once
    entry = _state(tmp_path)["undelivered"]["big|video|retry000001"]
    assert entry["done"] and entry["tries"] >= 4
    retried = [p for d in FakeDiscordClient.instances for c, p in d.sent
               if c == "500" and p.get("embeds")][0]
    assert retried["allowed_mentions"] == {"parse": []}  # big has no mention configured


def test_transient_failure_everywhere_keeps_the_item_unseen(monkeypatch, wired_dests, tmp_path):
    monkeypatch.setattr(notifier, "DiscordClient", FailingFor)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "big", {"channels": {"default": "500"}, "enabled": True})
    FailingFor.bad = {"1": 503, "500": 503}
    FakeYouTube.videos = [VideoRef("allwait0001", {"rss", "videos"}, title="w"), *FakeYouTube.videos]
    assert notifier.run(wired_dests, role="primary") == 1
    state = _state(tmp_path)
    assert "allwait0001" not in state["videos"] and not state["undelivered"]
    FailingFor.bad = {}
    assert notifier.run(wired_dests, role="primary") == 0
    assert _embed_sends("allwait0001") == ["1", "500"]


def _age_undelivered(tmp_path, key, hours):
    from datetime import datetime, timedelta, timezone
    store = FileStateStore(tmp_path / "state.json")
    state = store.load()
    state.undelivered[key]["first"] = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds")
    store.save(state)


def _admin_messages():
    return [p["content"] for d in FakeDiscordClient.instances for c, p in d.sent if c == "999"]


def test_retry_gives_up_after_the_window_and_alerts(monkeypatch, wired_dests, tmp_path):
    monkeypatch.setattr(notifier, "DiscordClient", FailingFor)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "big", {"channels": {"default": "500"}, "enabled": True})
    FailingFor.bad = {"500": 503}
    FakeYouTube.videos = [VideoRef("giveup00001", {"rss", "videos"}, title="g"), *FakeYouTube.videos]
    notifier.run(wired_dests, role="primary")
    _age_undelivered(tmp_path, "big|video|giveup00001", 3)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")
    assert _embed_sends("giveup00001") == ["1"]  # too late: not sent
    lost = [a for a in _admin_messages() if "打ち切りました" in a]
    assert len(lost) == 1 and "giveup00001" in lost[0] and "手動で投稿" in lost[0]
    notifier.run(wired_dests, role="primary")
    assert len([a for a in _admin_messages() if "打ち切りました" in a]) == 1  # reported once


def test_every_given_up_notification_is_reported(monkeypatch, wired_dests, tmp_path):
    # Two give-ups in different runs within 12h: both must reach #管理 (not swallowed by a throttle).
    monkeypatch.setattr(notifier, "DiscordClient", FailingFor)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "big", {"channels": {"default": "500"}, "enabled": True})
    FailingFor.bad = {"500": 503}
    for vid in ("lostfirst01", "lostsecond1"):
        FakeYouTube.videos = [VideoRef(vid, {"rss", "videos"}, title=vid), *FakeYouTube.videos]
        notifier.run(wired_dests, role="primary")
    _age_undelivered(tmp_path, "big|video|lostfirst01", 3)
    notifier.run(wired_dests, role="primary")
    _age_undelivered(tmp_path, "big|video|lostsecond1", 3)
    notifier.run(wired_dests, role="primary")
    text = "\n".join(_admin_messages())
    assert "lostfirst01" in text and "lostsecond1" in text


def test_lost_alert_is_retried_when_it_cannot_be_sent(monkeypatch, wired_dests, tmp_path):
    monkeypatch.setattr(notifier, "DiscordClient", FailingFor)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "big", {"channels": {"default": "500"}, "enabled": True})
    FailingFor.bad = {"500": 503}
    FakeYouTube.videos = [VideoRef("lostlater01", {"rss", "videos"}, title="l"), *FakeYouTube.videos]
    notifier.run(wired_dests, role="primary")
    _age_undelivered(tmp_path, "big|video|lostlater01", 3)
    FailingFor.bad = {"500": 503, "999": 503}  # #管理 is also unreachable for a moment
    notifier.run(wired_dests, role="primary")
    assert not _state(tmp_path)["undelivered"]["big|video|lostlater01"].get("reported")
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")
    assert any("lostlater01" in a and "打ち切りました" in a for a in _admin_messages())
    assert _state(tmp_path)["undelivered"]["big|video|lostlater01"]["reported"]


@pytest.mark.parametrize("change", ["kinds", "moved", "disabled", "removed"])
def test_retry_follows_the_destination_settings_of_now(monkeypatch, wired_dests, tmp_path, change):
    monkeypatch.setattr(notifier, "DiscordClient", BlindFailing)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "big", {"channels": {"default": "500"}, "enabled": True})
    FailingFor.bad = {"500": 503}
    FakeYouTube.videos = [VideoRef("settings001", {"rss", "videos"}, title="s"), *FakeYouTube.videos]
    notifier.run(wired_dests, role="primary")
    new = {"kinds": {"channels": {"default": "500"}, "kinds": ["live", "post"], "enabled": True},
           "moved": {"channels": {"default": "500", "video": "600"}, "enabled": True},
           "disabled": {"channels": {"default": "500"}, "enabled": False},
           "removed": None}[change]
    store = FileStateStore(tmp_path / "state.json")
    state = store.load()
    if new is None:
        state.destinations.pop("big")
    else:
        state.destinations["big"] = new
    state.destinations_updated = "2099-01-02T00:00:00+00:00"
    store.save(state)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")
    assert _embed_sends("settings001") == ["1"]
    assert _state(tmp_path)["undelivered"]["big|video|settings001"]["done"]


def test_retry_uses_the_mention_configured_now(monkeypatch, wired_dests, tmp_path):
    monkeypatch.setattr(notifier, "DiscordClient", BlindFailing)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "big", {"channels": {"default": "500"}, "mentions": {"video": "@everyone"},
                                "enabled": True})
    FailingFor.bad = {"500": 503}
    FakeYouTube.videos = [VideoRef("mention0001", {"rss", "videos"}, title="m"), *FakeYouTube.videos]
    notifier.run(wired_dests, role="primary")
    assert "@everyone" not in json.dumps(_state(tmp_path)["undelivered"], ensure_ascii=False)
    _add_dest(tmp_path, "big", {"channels": {"default": "500"}, "mentions": {}, "enabled": True})
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")
    resent = [p for d in FakeDiscordClient.instances for c, p in d.sent if c == "500" and p.get("embeds")]
    assert len(resent) == 1 and not resent[0]["content"].startswith("@everyone")
    assert resent[0]["allowed_mentions"] == {"parse": []}


def test_destination_alert_keeps_the_link_when_the_error_is_long(monkeypatch, wired_dests, tmp_path):
    class LongError(FailingFor):
        def send(self, channel_id, payload):
            if channel_id == "500":
                from achichi_notifier.discord_api import DiscordError
                raise DiscordError("POST /channels/500/messages -> HTTP 400: " + "x" * 400, status=400)
            return super().send(channel_id, payload)

    monkeypatch.setattr(notifier, "DiscordClient", LongError)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "big", {"channels": {"default": "500"}, "enabled": True})
    FakeYouTube.videos = [VideoRef("longerr0001", {"rss", "videos"}, title="e"), *FakeYouTube.videos]
    notifier.run(wired_dests, role="primary")
    assert any("longerr0001" in a for a in _admin_messages())


def test_undelivered_merge_prefers_done():
    from achichi_notifier.state import State as S
    mine = S(undelivered={"big|video|x": {"first": "2099-01-01T00:00:00+00:00", "tries": 1, "done": None}})
    theirs = S(undelivered={"big|video|x": {"first": "2099-01-01T00:00:00+00:00", "tries": 3,
                                            "done": "2099-01-01T00:10:00+00:00"}})
    mine.merge_from(theirs)
    assert mine.undelivered["big|video|x"]["done"]
    pending = S(undelivered={"big|video|x": {"first": "2099-01-01T00:00:00+00:00", "tries": 5, "done": None}})
    theirs.merge_from(pending)  # an older copy without "done" must not revive it
    assert theirs.undelivered["big|video|x"]["done"]
    reported = S(undelivered={"big|video|y": {"lost": "2099-01-01T00:00:00+00:00",
                                              "reported": "2099-01-01T00:05:00+00:00"}})
    unreported = S(undelivered={"big|video|y": {"lost": "2099-01-01T00:00:00+00:00", "reported": None}})
    unreported.merge_from(reported)
    assert unreported.undelivered["big|video|y"]["reported"]  # never reported twice


def test_unreported_lost_notifications_survive_pruning():
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
    state = State(undelivered={
        "a|video|1": {"first": old, "done": old, "lost": old, "reported": None},
        "a|video|2": {"first": old, "done": old, "lost": old, "reported": old},
        "a|video|3": {"first": old, "done": old},
        "a|video|4": "garbage",
    })
    state.prune()
    assert list(state.undelivered) == ["a|video|1"]


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
        defaults.update(kinds=None, clear_mentions=False)
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


def test_re_adding_shows_what_was_saved_and_can_clear_mentions(cli_env, tmp_path, capsys):
    assert dest_cli.cmd_add(cli_env, Args(name="big", channel="123456789012345678",
                                          live_channel="323456789012345678", mention_live="<@&8>")) == 0
    assert dest_cli.cmd_enable(cli_env, "big", False) == 0
    capsys.readouterr()
    assert dest_cli.cmd_add(cli_env, Args(name="big", channel="123456789012345678")) == 0
    out = capsys.readouterr().out
    assert "停止中" in out and "dest enable big" in out  # a disabled destination is not silently "added"
    assert "live 用チャンネルは外しました" in out and "--clear-mentions" in out
    assert dest_cli.cmd_add(cli_env, Args(name="big", channel="123456789012345678",
                                          clear_mentions=True, mention_post="<@&9>")) == 0
    big = json.loads((tmp_path / "state.json").read_text())["destinations"]["big"]
    assert big["mentions"] == {"post": "<@&9>"}


def test_cli_warns_when_a_channel_is_shared(cli_env, tmp_path, capsys):
    # main (.env) posts to channel "1"; use a real-looking id for both destinations instead
    assert dest_cli.cmd_add(cli_env, Args(name="a", channel="123456789012345678")) == 0
    capsys.readouterr()
    assert dest_cli.cmd_add(cli_env, Args(name="b", channel="123456789012345678")) == 0
    assert "送り先「a」でも使われています" in capsys.readouterr().out


def test_dest_enable_disable_and_check_through_the_command_line(cli_env, tmp_path, monkeypatch, capsys):
    from achichi_notifier import __main__ as cli
    from achichi_notifier.config import Config
    monkeypatch.setattr(Config, "from_env", classmethod(lambda c: cli_env))
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    assert cli.main(["--env-file", str(env), "dest", "add", "big", "--channel", "123456789012345678"]) == 0
    assert cli.main(["--env-file", str(env), "dest", "disable", "big"]) == 0
    assert json.loads((tmp_path / "state.json").read_text())["destinations"]["big"]["enabled"] is False
    assert cli.main(["--env-file", str(env), "dest", "enable", "big"]) == 0
    assert json.loads((tmp_path / "state.json").read_text())["destinations"]["big"]["enabled"] is True
    sent = []
    monkeypatch.setattr(FakeRestClient, "send", lambda self, c, p: sent.append(c) or {"id": "1"})
    assert cli.main(["--env-file", str(env), "dest", "check", "big", "--send-test"]) == 0
    assert sent == ["123456789012345678"]
    assert cli.main(["--env-file", str(env), "dest", "check", "nosuch"]) == 1
    assert cli.main(["--env-file", str(env), "dest", "add", "hidden", "--channel", "404404404404404404"]) == 1
    store = FileStateStore(tmp_path / "state.json")
    state = store.load()
    state.destinations["hidden"] = {"channels": {"default": "404404404404404404"}, "enabled": True}
    store.save(state)
    assert cli.main(["--env-file", str(env), "dest", "check", "hidden"]) == 1


def test_shared_channel_gets_one_message_with_both_mentions(wired_dests, tmp_path):
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "same", {"channels": {"default": "1"}, "mentions": {"video": "<@&20>"},
                                 "enabled": True})
    FakeYouTube.videos = [VideoRef("bothment001", {"rss", "videos"}, title="m"), *FakeYouTube.videos]
    notifier.run(wired_dests, role="primary")
    sent = [p for d in FakeDiscordClient.instances for c, p in d.sent
            if c == "1" and p.get("embeds") and p["embeds"][0]["url"].endswith("bothment001")]
    assert len(sent) == 1 and sent[0]["content"].startswith("<@&10> <@&20> ")


def test_retry_is_dropped_when_the_destination_is_removed(monkeypatch, wired_dests, tmp_path):
    monkeypatch.setattr(notifier, "DiscordClient", FailingFor)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "big", {"channels": {"default": "500"}, "enabled": True})
    FailingFor.bad = {"500": 503}
    FakeYouTube.videos = [VideoRef("dropped0001", {"rss", "videos"}, title="d"), *FakeYouTube.videos]
    notifier.run(wired_dests, role="primary")
    store = FileStateStore(tmp_path / "state.json")
    state = store.load()
    state.destinations["big"]["enabled"] = False
    state.destinations_updated = "2099-01-02T00:00:00+00:00"
    store.save(state)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")
    assert _embed_sends("dropped0001") == ["1"]
    assert _state(tmp_path)["undelivered"]["big|video|dropped0001"]["done"]


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


def test_cli_add_reports_failure_when_a_runner_overwrites_it(cli_env, tmp_path, monkeypatch, capsys):
    real_save = FileStateStore.save

    def save_then_lose(self, state):
        real_save(self, state)
        lost = FileStateStore.load(self)
        lost.destinations.pop("big", None)  # a runner's save lands right after ours
        real_save(self, lost)
    monkeypatch.setattr(FileStateStore, "save", save_then_lose)
    assert dest_cli.cmd_add(cli_env, Args(name="big", channel="123456789012345678")) == 1
    out = capsys.readouterr().out
    assert "[NG]" in out and "追加しました" not in out



def test_expired_retry_that_actually_arrived_is_not_reported_lost(monkeypatch, wired_dests, tmp_path):
    class Ambiguous(FailingFor):
        """Discord stores the message but the reply times out (status None = temporary)."""
        ambiguous = False

        def send(self, channel_id, payload):
            if channel_id == "500" and Ambiguous.ambiguous:
                FakeDiscordClient.send(self, channel_id, payload)
                from achichi_notifier.discord_api import DiscordError
                raise DiscordError("POST /channels/500/messages: read timeout")
            return super().send(channel_id, payload)

    monkeypatch.setattr(notifier, "DiscordClient", Ambiguous)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "big", {"channels": {"default": "500"}, "enabled": True})
    FailingFor.bad = {"500": 503}
    FakeYouTube.videos = [VideoRef("ambiguous01", {"rss", "videos"}, title="a"), *FakeYouTube.videos]
    notifier.run(wired_dests, role="primary")
    _age_undelivered(tmp_path, "big|video|ambiguous01", 1.9)
    FailingFor.bad, Ambiguous.ambiguous = {}, True
    notifier.run(wired_dests, role="primary")  # arrives, but looks like a failure
    Ambiguous.ambiguous = False
    _age_undelivered(tmp_path, "big|video|ambiguous01", 2.1)
    notifier.run(wired_dests, role="primary")
    assert _embed_sends("ambiguous01") == ["1", "500"]
    assert not any("打ち切りました" in a for a in _admin_messages())
    assert not _state(tmp_path)["undelivered"]["big|video|ambiguous01"].get("lost")


def test_shared_channel_retry_survives_removing_the_destination_it_was_filed_under(
        monkeypatch, wired_dests, tmp_path):
    monkeypatch.setattr(notifier, "DiscordClient", BlindFailing)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "a", {"channels": {"default": "500"}, "kinds": ["video"], "enabled": True})
    _add_dest(tmp_path, "b", {"channels": {"default": "500"}, "mentions": {"video": "<@&30>"}, "enabled": True})
    FailingFor.bad = {"500": 503}
    FakeYouTube.videos = [VideoRef("sharedret01", {"rss", "videos"}, title="s"), *FakeYouTube.videos]
    notifier.run(wired_dests, role="primary")
    assert list(_state(tmp_path)["undelivered"]) == ["a|video|sharedret01"]
    store = FileStateStore(tmp_path / "state.json")
    state = store.load()
    state.destinations.pop("a")
    state.destinations_updated = "2099-01-02T00:00:00+00:00"
    store.save(state)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")
    resent = [p for d in FakeDiscordClient.instances for c, p in d.sent if c == "500" and p.get("embeds")]
    assert len(resent) == 1 and resent[0]["content"].startswith("<@&30> ")  # b's mention, now


def test_lost_report_names_the_destination_that_owns_the_channel_now(monkeypatch, wired_dests, tmp_path):
    monkeypatch.setattr(notifier, "DiscordClient", BlindFailing)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "a", {"channels": {"default": "500"}, "enabled": True})
    _add_dest(tmp_path, "b", {"channels": {"default": "500"}, "enabled": True})
    FailingFor.bad = {"500": 503}
    FakeYouTube.videos = [VideoRef("ownernow001", {"rss", "videos"}, title="o"), *FakeYouTube.videos]
    notifier.run(wired_dests, role="primary")
    store = FileStateStore(tmp_path / "state.json")
    state = store.load()
    state.destinations.pop("a")
    state.destinations_updated = "2099-01-02T00:00:00+00:00"
    store.save(state)
    FailingFor.bad = {"500": 403}
    notifier.run(wired_dests, role="primary")
    lost = [a for a in _admin_messages() if "打ち切りました" in a]
    assert lost and "送り先「b」" in lost[0] and "dest check b" in lost[0] and "チャンネル 500" in lost[0]


def test_alert_text_matches_what_happens_when_nothing_was_delivered(monkeypatch, wired_dests, tmp_path):
    monkeypatch.setattr(notifier, "DiscordClient", FailingFor)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "big", {"channels": {"default": "500"}, "enabled": True})
    FailingFor.bad = {"1": 503, "500": 403}
    FakeYouTube.videos = [VideoRef("nowhere0001", {"rss", "videos"}, title="n"), *FakeYouTube.videos]
    notifier.run(wired_dests, role="primary")
    main_alert = [a for a in _admin_messages() if "送り先「main」" in a]
    assert main_alert and "すべての送り先" in main_alert[0] and "最大 2 時間" not in main_alert[0]
    assert "72 時間" in main_alert[0]


def test_give_up_reason_says_how_many_tries_were_made(monkeypatch, wired_dests, tmp_path):
    monkeypatch.setattr(notifier, "DiscordClient", FailingFor)
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")  # seed
    _add_dest(tmp_path, "big", {"channels": {"default": "500"}, "enabled": True})
    FailingFor.bad = {"500": 503}
    FakeYouTube.videos = [VideoRef("slept000001", {"rss", "videos"}, title="z"), *FakeYouTube.videos]
    notifier.run(wired_dests, role="primary")
    _age_undelivered(tmp_path, "big|video|slept000001", 7)  # the PC slept all night
    FailingFor.bad = {}
    notifier.run(wired_dests, role="primary")
    lost = [a for a in _admin_messages() if "打ち切りました" in a][0]
    assert "1 回" in lost and "止まっていた" in lost
