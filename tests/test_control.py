"""Emergency stop (stop marker / 🛑 reaction in #管理) and deleting the bot's own messages."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from achichi_notifier import control, control_cli, dest_cli, notifier
from achichi_notifier.config import ConfigError
from achichi_notifier.discord_api import DiscordError
from achichi_notifier.state import STATE_FILENAME, FileStateStore, State
from achichi_notifier.youtube import Post, VideoRef

from test_destinations import FailingFor, _add_dest, _age_undelivered, _embed_sends  # noqa: F401
from test_notifier import FakeDiscordClient, FakeYouTube, _cfg, _state, _write_state, fake_youtube  # noqa: F401

ADMIN = "999"
REAL_DELETE = control_cli.cmd_delete
REMOVED = "777777777777777777"
NOW = datetime.now(timezone.utc)


def _sf(minutes_ago):
    """A message id (snowflake) created `minutes_ago` minutes before the test module loaded."""
    ms = int((NOW - timedelta(minutes=minutes_ago)).timestamp() * 1000)
    return str((ms - control.DISCORD_EPOCH_MS) << 22)


def _marker(author="bot", minutes_ago=3):
    return {"id": _sf(minutes_ago), "author": {"id": author},
            "content": control.MARKER_PREFIX + "（テスト）\n再開: resume", "reactions": []}


def _stop_reaction(count=1, me=False, name="🛑", emoji_id=None):
    return {"id": _sf(2), "author": {"id": "human"}, "content": "",
            "reactions": [{"emoji": {"id": emoji_id, "name": name}, "count": count, "me": me}]}


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """run() with a fake Discord, #管理 = 999, main = 1 and an extra destination "big" = 500."""
    FakeDiscordClient.instances = []
    FakeDiscordClient.history = {}
    monkeypatch.setattr(notifier, "DiscordClient", FakeDiscordClient)
    monkeypatch.setattr(notifier, "make_store", lambda cfg, discord: FileStateStore(tmp_path / "state.json"))
    cfg = _cfg(tmp_path, discord_token="t", state_backend="discord", state_channel_id=ADMIN)
    notifier.run(cfg, role="primary")  # seed
    _add_dest(tmp_path, "big", {"channels": {"default": "500"}, "enabled": True})
    return cfg


def _all_sent():
    return [(c, p) for d in FakeDiscordClient.instances for c, p in d.sent]


def _new_items(tag):
    vid = f"{tag}vid0001"[:11]
    FakeYouTube.videos = [VideoRef(vid, {"rss", "videos"}, title=f"{tag} video"), *FakeYouTube.videos]
    FakeYouTube.public_posts = [Post(f"Ugkx{tag}POST", text=f"{tag} post", published_text="1 分前"),
                                *FakeYouTube.public_posts]
    return vid


def _stop(on=True):
    FakeDiscordClient.history = {ADMIN: [_marker()] if on else []}


# ---------------------------------------------------------------------- the runner side


def test_stop_marker_sends_nothing_and_never_sends_it_later(wired, tmp_path):
    _stop()
    vid = _new_items("held")
    FakeDiscordClient.instances = []
    assert notifier.run(wired, role="primary") == 0
    assert _all_sent() == []  # neither main, nor big, nor #管理
    state = _state(tmp_path)
    assert vid in state["videos"] and "UgkxheldPOST" in state["posts"]  # still checked and marked seen
    held = state["muted"]
    assert any(vid in url for url in held) and any("UgkxheldPOST" in url for url in held)
    assert all(e["why"] == "stop コマンド" for e in held.values())
    runner = state["runners"]["primary"]
    assert runner["last_success"]  # a stopped run is not a failed run
    assert "pause" not in state  # the switch lives in #管理, not in the state JSON

    _stop(on=False)
    notifier.run(wired, role="primary")
    assert _all_sent() == []  # what was held back is not sent after resume either
    new = _new_items("next")
    notifier.run(wired, role="primary")
    assert sorted(_embed_sends(new)) == ["1", "500"]


def test_a_marker_written_by_someone_else_does_not_stop(wired, tmp_path):
    FakeDiscordClient.history = {ADMIN: [_marker(author="human")]}
    vid = _new_items("fake")
    notifier.run(wired, role="primary")
    assert sorted(_embed_sends(vid)) == ["1", "500"]


@pytest.mark.parametrize("reaction, stopped", [
    (_stop_reaction(), True),
    (_stop_reaction(name="🛑️"), True),
    (_stop_reaction(count=2, me=True), True),  # someone else besides the bot
    (_stop_reaction(count=1, me=True), False),  # only the bot itself
    (_stop_reaction(name="👍"), False),
    (_stop_reaction(emoji_id="42"), False),  # a custom emoji that happens to be named 🛑
])
def test_stop_reaction_in_the_admin_channel(wired, tmp_path, reaction, stopped):
    FakeDiscordClient.history = {ADMIN: [{"id": _sf(1), "author": {"id": "bot"}, "content": "x"}, reaction]}
    vid = _new_items("react")
    FakeDiscordClient.instances = []
    notifier.run(wired, role="primary")
    if stopped:
        assert _all_sent() == []
        assert "#管理" in next(iter(_state(tmp_path)["muted"].values()))["why"]
    else:
        assert sorted(_embed_sends(vid)) == ["1", "500"]


def test_removing_the_reaction_resumes(wired, tmp_path):
    FakeDiscordClient.history = {ADMIN: [_stop_reaction()]}
    _new_items("first")
    notifier.run(wired, role="primary")
    FakeDiscordClient.history = {ADMIN: []}
    vid = _new_items("again")
    FakeDiscordClient.instances = []
    notifier.run(wired, role="primary")
    assert sorted(_embed_sends(vid)) == ["1", "500"]


def test_the_stop_is_read_after_the_youtube_fetch(monkeypatch, wired, tmp_path):
    vid = _new_items("late")
    collect = notifier.Notifier.collect

    def slow_collect(self, state, mode):
        result = collect(self, state, mode)
        _stop()  # someone stops the bot while this run is still fetching YouTube
        return result

    monkeypatch.setattr(notifier.Notifier, "collect", slow_collect)
    FakeDiscordClient.instances = []
    notifier.run(wired, role="primary")
    assert _all_sent() == [] and any(vid in url for url in _state(tmp_path)["muted"])


@pytest.mark.parametrize("error", [DiscordError("GET messages -> HTTP 500", status=500), ValueError("bad json")])
def test_an_unreadable_admin_channel_does_not_stop_the_bot(monkeypatch, wired, tmp_path, caplog, error):
    class Unreadable(FakeDiscordClient):
        def messages(self, channel_id, limit=100, before=None):
            raise error

    monkeypatch.setattr(notifier, "DiscordClient", Unreadable)
    vid = _new_items("blind")
    notifier.run(wired, role="primary")
    assert sorted(_embed_sends(vid)) == ["1", "500"]
    assert "stop switches" in caplog.text


def test_a_stopped_fallback_takeover_sends_no_alert(wired, tmp_path):
    state = _state(tmp_path)
    old = "2000-01-01T00:00:00+00:00"
    state["runners"]["primary"].update({"last_success": old, "last_run": old})
    _write_state(tmp_path, state)
    _stop()
    vid = _new_items("fall")
    FakeDiscordClient.instances = []
    notifier.run(wired, role="fallback")
    assert _all_sent() == []
    assert "primary_down" not in _state(tmp_path)["alerts"]  # so it is sent once resumed
    _stop(on=False)
    notifier.run(wired, role="fallback")
    assert [p for c, p in _all_sent() if c == ADMIN and "主系の PC が止まっています" in p["content"]]
    assert _embed_sends(vid) == []  # found while stopped: still never sent


def test_cookie_alert_waits_until_resume(wired, tmp_path):
    FakeYouTube.member_posts, FakeYouTube.member_logged_in = [], False
    _stop()
    FakeDiscordClient.instances = []
    notifier.run(wired, role="primary")
    assert _all_sent() == []
    _stop(on=False)
    notifier.run(wired, role="primary")
    assert len([p for c, p in _all_sent() if c == ADMIN and "失効" in p["content"]]) == 1


def test_retries_wait_while_stopped(monkeypatch, wired, tmp_path):
    monkeypatch.setattr(notifier, "DiscordClient", FailingFor)
    FailingFor.bad = {"500": 503}
    vid = _new_items("retry")
    notifier.run(wired, role="primary")  # main got it, big is queued for a retry
    assert _embed_sends(vid) == ["1"]
    FailingFor.bad = {}
    _stop()
    notifier.run(wired, role="primary")
    assert _embed_sends(vid) == ["1"]  # no retry while stopped
    assert not _state(tmp_path)["undelivered"][f"big|video|{vid}"].get("done")
    _stop(on=False)
    notifier.run(wired, role="primary")
    assert sorted(_embed_sends(vid)) == ["1", "500"]  # still within the retry window


def test_a_retry_given_up_during_a_stop_says_so(monkeypatch, wired, tmp_path):
    monkeypatch.setattr(notifier, "DiscordClient", FailingFor)
    FailingFor.bad = {"500": 503}
    vid = _new_items("lost")
    notifier.run(wired, role="primary")
    FailingFor.bad = {}
    _stop()
    notifier.run(wired, role="primary")
    _age_undelivered(tmp_path, f"big|video|{vid}", 3)
    _stop(on=False)
    FakeDiscordClient.instances = []
    notifier.run(wired, role="primary")
    lost = [p["content"] for c, p in _all_sent() if c == ADMIN and "打ち切りました" in p["content"]]
    assert len(lost) == 1 and "Bot を止めていた間の 1 回" in lost[0]


def test_a_long_stop_keeps_unhandled_retries_until_they_are_reported(monkeypatch, wired, tmp_path):
    monkeypatch.setattr(notifier, "DiscordClient", FailingFor)
    FailingFor.bad = {"500": 503}
    vid = _new_items("long")
    notifier.run(wired, role="primary")
    FailingFor.bad = {}
    _stop()
    _age_undelivered(tmp_path, f"big|video|{vid}", 30)  # stopped for more than a day
    notifier.run(wired, role="primary")
    assert f"big|video|{vid}" in _state(tmp_path)["undelivered"]  # not pruned while unhandled
    _stop(on=False)
    FakeDiscordClient.instances = []
    notifier.run(wired, role="primary")
    assert [p for c, p in _all_sent() if c == ADMIN and "打ち切りました" in p["content"]]


def test_dry_run_ignores_the_stop(wired, tmp_path, capsys):
    _stop()
    _new_items("dry")
    notifier.run(wired, role="primary", dry_run=True)
    assert "--- [dry-run]" in capsys.readouterr().out


def test_preview_is_not_sent_while_stopped(wired, tmp_path, capsys):
    _stop()
    FakeDiscordClient.instances = []
    assert notifier.preview(wired, send=True) == 2
    assert _all_sent() == [] and "止めている" in capsys.readouterr().out


# ---------------------------------------------------------------------- shared state


def test_held_back_lists_merge_and_round_trip():
    runner = State(muted={"u1": {"kind": "video", "at": "2026-11-16T10:00:00+00:00"}})
    runner.merge_from(State(muted={"u2": {"kind": "post", "at": "2026-11-16T11:00:00+00:00"}}))
    assert set(runner.muted) == {"u1", "u2"}
    again = State.from_dict(json.loads(runner.dumps()))
    assert again.muted == runner.muted and "muted" not in again.extra
    assert State.from_dict({"videos": {}}).muted == {}
    assert State.from_dict({"muted": {"a": "b"}}).muted == {}
    old_switch = State.from_dict({"pause": {"on": True}})  # from the first draft: just carried along
    assert old_switch.extra == {"pause": {"on": True}}


def test_old_held_back_entries_are_pruned():
    state = State(muted={"old": {"at": (NOW - timedelta(days=8)).isoformat()},
                         "new": {"at": (NOW - timedelta(days=1)).isoformat()},
                         "bad": {"at": "not a time"}})
    state.prune()
    assert set(state.muted) == {"new", "bad"}


# ---------------------------------------------------------------------- stop / resume / status CLI


class FakeRest:
    """REST client for the CLI commands: per-channel history (newest first), sends, deletes."""
    history: dict = {}
    deleted: list = []
    fail_delete: dict = {}  # message id -> HTTP status
    fail_send: int | None = None
    sent: list = []
    counter = 0

    def __init__(self, token=None):
        pass

    def me(self):
        return {"id": "bot"}

    def messages(self, channel_id, limit=100, before=None):
        batch = list(FakeRest.history.get(channel_id, []))
        if before is not None:
            batch = [m for m in batch if int(m["id"]) < int(before)]
        return batch[:limit]

    def send(self, channel_id, payload):
        if FakeRest.fail_send:
            raise DiscordError(f"POST -> HTTP {FakeRest.fail_send}", status=FakeRest.fail_send)
        FakeRest.counter += 1
        message = {"id": _sf(-FakeRest.counter / 60), "author": {"id": "bot"},
                   "content": payload.get("content") or "", "reactions": []}
        FakeRest.history.setdefault(channel_id, []).insert(0, message)
        FakeRest.sent.append((channel_id, payload))
        return message

    def delete_message(self, channel_id, message_id):
        status = FakeRest.fail_delete.get(message_id)
        if status:
            raise DiscordError(f"DELETE -> HTTP {status}", status=status)
        FakeRest.deleted.append((channel_id, message_id))
        FakeRest.history[channel_id] = [m for m in FakeRest.history.get(channel_id, []) if m["id"] != message_id]

    def _request(self, method, path, **kw):
        if path.startswith("/channels/"):
            return {"name": "通知", "guild_id": "77"}
        return {"name": "サーバー"}


@pytest.fixture
def cli(monkeypatch, tmp_path):
    FakeRest.history, FakeRest.deleted, FakeRest.fail_delete, FakeRest.sent = {}, [], {}, []
    FakeRest.fail_send = None
    monkeypatch.setattr(control_cli, "DiscordClient", FakeRest)
    monkeypatch.setattr(dest_cli, "DiscordClient", FakeRest)
    monkeypatch.setattr(dest_cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(notifier, "make_store", lambda cfg, discord: FileStateStore(tmp_path / "state.json"))
    return _cfg(tmp_path, discord_token="t", state_backend="discord", state_channel_id=ADMIN)


def _markers():
    return [m for m in FakeRest.history.get(ADMIN, []) if control.is_marker(m, "bot")]


def test_stop_then_resume(cli, tmp_path, capsys):
    assert control_cli.cmd_stop(cli, "二重通知") == 0
    assert len(_markers()) == 1 and "理由: 二重通知" in _markers()[0]["content"]
    assert FakeRest.sent[0][1]["allowed_mentions"] == {"parse": []}
    assert "止めました" in capsys.readouterr().out
    assert control.pause_reason(cli, FakeRest()) == "stop コマンド"
    assert control_cli.cmd_stop(cli) == 0 and len(_markers()) == 1  # no second marker
    assert "すでに止めています" in capsys.readouterr().out

    state = _state(tmp_path) if (tmp_path / "state.json").exists() else {}
    state["muted"] = {"https://www.youtube.com/watch?v=heldheld001": {
        "kind": "live_upcoming", "title": "held stream", "at": (NOW + timedelta(hours=1)).isoformat()}}
    _write_state(tmp_path, state)
    assert control_cli.cmd_status(cli) == 0
    out = capsys.readouterr().out
    assert "止まっています" in out and "held stream" in out

    assert control_cli.cmd_resume(cli) == 0
    out = capsys.readouterr().out
    assert "解除しました" in out and "held stream" in out and "heldheld001" in out
    assert _markers() == [] and control.pause_reason(cli, FakeRest()) is None
    assert control_cli.cmd_status(cli) == 0
    out = capsys.readouterr().out
    assert "動いています" in out and "held stream" in out  # still listed after resume (7 days)


def test_resume_with_a_reaction_left_says_it_is_still_stopped(cli, capsys):
    assert control_cli.cmd_stop(cli) == 0
    FakeRest.history[ADMIN].append(_stop_reaction())
    capsys.readouterr()
    assert control_cli.cmd_resume(cli) == 1
    out = capsys.readouterr().out
    assert "解除しました" in out and "まだ止まっています" in out and "次の確認から通知を送ります" not in out
    assert control_cli.cmd_status(cli) == 0 and "止まっています" in capsys.readouterr().out


def test_resume_reports_a_marker_it_could_not_delete(cli, capsys):
    assert control_cli.cmd_stop(cli) == 0
    FakeRest.fail_delete = {_markers()[0]["id"]: 403}
    assert control_cli.cmd_resume(cli) == 1
    assert "残っています" in capsys.readouterr().out


def test_stop_that_cannot_write_points_to_the_reaction(cli, capsys):
    FakeRest.fail_send = 403
    assert control_cli.cmd_stop(cli) == 1
    assert "🛑" in capsys.readouterr().out


def test_stop_warns_about_runners_that_cannot_stop_even_if_idle(cli, tmp_path, capsys):
    store = FileStateStore(tmp_path / "state.json")
    state = store.load()
    state.runners = {"primary": {"last_run": NOW.isoformat(), "features": list(notifier.FEATURES)},
                     "fallback": {"last_run": "2026-01-01T00:00:00+00:00", "features": ["destinations"]}}
    store.save(state)
    assert control_cli.cmd_stop(cli) == 0
    out = capsys.readouterr().out
    assert "fallback の PC は古い版" in out and "primary" not in out.split("古い版")[0]


def test_stop_needs_the_admin_channel(cli):
    import dataclasses
    with pytest.raises(ConfigError):
        control_cli.cmd_stop(dataclasses.replace(cli, state_channel_id=None))


def test_send_test_is_skipped_while_stopped(cli, capsys):
    assert dest_cli.cmd_add(cli, _Args(name="big", channel="123456789012345678")) == 0
    assert control_cli.cmd_stop(cli) == 0
    FakeRest.sent = []
    capsys.readouterr()
    assert dest_cli.cmd_check(cli, "big", send_test=True) == 1
    assert FakeRest.sent == [] and "テスト送信はしません" in capsys.readouterr().out


class _Args:
    def __init__(self, **kw):
        from achichi_notifier import destinations as dests
        self.__dict__.update({f"{g}_channel": None for g in dests.CHANNEL_GROUPS})
        self.__dict__.update({f"mention_{k}": None for k in dests.KIND_CHOICES})
        self.__dict__.update(kinds=None, clear_mentions=False, **kw)


# ---------------------------------------------------------------------- delete


def _msg(minutes_ago, author="bot", content="🎬 新しい動画が公開されました", url=None, attachments=()):
    embeds = [{"url": url, "title": "t"}] if url else []
    return {"id": _sf(minutes_ago), "author": {"id": author}, "content": content, "embeds": embeds,
            "attachments": [{"filename": f} for f in attachments]}


@pytest.fixture
def channels(cli, tmp_path):
    _add_dest(tmp_path, "big", {"channels": {"default": "500"}, "enabled": True})
    _add_dest(tmp_path, "off", {"channels": {"default": "600"}, "enabled": False})
    FakeRest.history = {
        "1": [_msg(5, url="https://www.youtube.com/watch?v=abcdefghijk"),
              _msg(10, author="human", content="https://www.youtube.com/watch?v=abcdefghijk"),
              _msg(20, url="https://www.youtube.com/watch?v=abcdefghijkX"),  # a different id
              _msg(300, url="https://www.youtube.com/watch?v=oldoldoldol")],
        "500": [_msg(6, url="https://www.youtube.com/watch?v=abcdefghijk"),
                _msg(30, content="📝 新しい投稿がありました", url="https://www.youtube.com/post/UgkxPOST1234")],
        "600": [_msg(7, url="https://www.youtube.com/shorts/shortshort1")],
        REMOVED: [_msg(8, url="https://www.youtube.com/watch?v=removedremo")],  # a removed destination
        ADMIN: [_msg(1, content="🗂 state", attachments=(STATE_FILENAME,)), _msg(2, content="⚠️ alert")],
    }
    return cli


def _deleted_ids():
    return sorted(m for _, m in FakeRest.deleted)


def _printed_command(out):
    import shlex
    line = next(line.strip() for line in out.splitlines() if "--ids=" in line)
    words = shlex.split(line)
    assert words[:6] == ["uv", "run", "python", "-m", "achichi_notifier", "delete"] and words[-1] == "--yes"
    return words[6:]


def _run_printed(cfg, words, monkeypatch):
    """Run the printed command through the real argument parser."""
    from achichi_notifier import __main__ as cli
    captured = {}
    monkeypatch.setattr(control_cli, "cmd_delete", lambda cfg_, **kw: captured.update(kw) or 0)
    monkeypatch.setattr(cli, "Config", type("C", (), {"from_env": staticmethod(lambda: cfg)}))
    monkeypatch.setattr(cli, "load_dotenv", lambda path: None)
    assert cli.main(["delete", *words]) == 0
    return captured


def test_the_printed_command_deletes_exactly_the_list(channels, capsys, monkeypatch):
    assert control_cli.cmd_delete(channels, last=3) == 0
    out = capsys.readouterr().out
    assert "3 件が見つかりました" in out and "まだ消していません" in out and FakeRest.deleted == []
    kwargs = _run_printed(channels, _printed_command(out), monkeypatch)
    assert kwargs["last"] == 3 and kwargs["yes"] is True
    assert sorted(kwargs["ids"].split(",")) == sorted([_sf(5), _sf(6), _sf(7)])
    # Meanwhile: a new notification arrives, and a moderator deletes one listed message by hand.
    FakeRest.history["1"].insert(0, _msg(0, url="https://www.youtube.com/watch?v=brandnew001"))
    FakeRest.history["600"] = []
    assert REAL_DELETE(channels, last=3, ids=kwargs["ids"], yes=True) == 0
    assert _deleted_ids() == sorted([_sf(5), _sf(6)])  # nothing outside the list, no stand-in
    assert "見つかりませんでした" in capsys.readouterr().out


def test_the_printed_command_survives_odd_ids_and_urls(channels, capsys, monkeypatch):
    FakeRest.history["1"].insert(0, _msg(1, url="https://www.youtube.com/watch?v=-bcdefghijk"))
    assert control_cli.cmd_delete(channels, url="-bcdefghijk", since="1h") == 0
    kwargs = _run_printed(channels, _printed_command(capsys.readouterr().out), monkeypatch)
    assert kwargs["url"] == "-bcdefghijk" and kwargs["since"] == "1h" and kwargs["ids"] == _sf(1)


def test_ids_hold_even_if_discord_ignored_before(monkeypatch, channels):
    def messages(self, channel_id, limit=100, before=None):  # e.g. a proxy dropping the parameter
        return list(FakeRest.history.get(channel_id, []))[:limit]

    monkeypatch.setattr(FakeRest, "messages", messages)
    FakeRest.history["1"].insert(0, _msg(0, url="https://www.youtube.com/watch?v=brandnew001"))
    assert control_cli.cmd_delete(channels, since="1h", ids=_sf(5), yes=True) == 0
    assert _deleted_ids() == [_sf(5)]


def test_last_with_yes_refuses_when_a_channel_could_not_be_read(monkeypatch, channels, capsys):
    real = FakeRest.messages

    def messages(self, channel_id, limit=100, before=None):
        if channel_id == "1":
            raise DiscordError("GET -> HTTP 503", status=503)
        return real(self, channel_id, limit, before)

    monkeypatch.setattr(FakeRest, "messages", messages)
    assert control_cli.cmd_delete(channels, last=1, yes=True) == 1
    assert FakeRest.deleted == [] and "消しません" in capsys.readouterr().out


def test_delete_by_url_matches_only_that_item(channels):
    assert control_cli.cmd_delete(channels, url="https://youtu.be/abcdefghijk", yes=True) == 0
    assert _deleted_ids() == sorted([_sf(5), _sf(6)])
    FakeRest.deleted = []
    assert control_cli.cmd_delete(channels, url="UgkxPOST1234", yes=True) == 0
    assert _deleted_ids() == [_sf(30)]


@pytest.mark.parametrize("bad", ["watch", "https://www.youtube.com/", "abc", "https://youtu.be/abcdefghijkX"])
def test_delete_rejects_vague_urls(channels, bad):
    with pytest.raises(ConfigError):
        control_cli.cmd_delete(channels, url=bad)


def test_delete_last_counts_across_channels(channels):
    assert control_cli.cmd_delete(channels, last=2, yes=True) == 0
    assert _deleted_ids() == sorted([_sf(5), _sf(6)])  # the two newest overall, not two per channel


def test_delete_dest_and_channel_filters(channels, capsys):
    assert control_cli.cmd_delete(channels, last=1, dest_names=["MAIN"], yes=True) == 0
    assert FakeRest.deleted == [("1", _sf(5))]
    FakeRest.deleted = []
    assert control_cli.cmd_delete(channels, since="1h", channel_ids=[REMOVED], yes=True) == 0
    assert FakeRest.deleted == [(REMOVED, _sf(8))]


def test_delete_never_touches_the_admin_channel(channels, tmp_path, capsys):
    import dataclasses
    admin = "888888888888888888"
    cfg = dataclasses.replace(channels, state_channel_id=admin)
    FakeRest.history[admin] = [_msg(1, url="https://www.youtube.com/watch?v=abcdefghijk"),
                               _msg(2, content="🗂 state", attachments=(STATE_FILENAME,))]
    _add_dest(tmp_path, "oops", {"channels": {"default": admin}, "enabled": True})
    assert control_cli.cmd_delete(cfg, since="1h", dest_names=["oops"], yes=True) == 0
    assert control_cli.cmd_delete(cfg, since="1h", channel_ids=[admin], yes=True) == 0
    assert FakeRest.deleted == [] and "対象外" in capsys.readouterr().out
    FakeRest.history["1"].insert(0, _msg(0, content="🗂 copy", attachments=(STATE_FILENAME,)))
    assert control_cli.cmd_delete(cfg, last=1, dest_names=["main"], yes=True) == 0
    assert FakeRest.deleted == [("1", _sf(5))]  # a state-file message is skipped anywhere


def test_delete_reports_failures_and_accepts_already_gone(channels, capsys):
    FakeRest.fail_delete = {_sf(5): 404, _sf(6): 403}
    assert control_cli.cmd_delete(channels, since="1h", yes=True) == 1
    out = capsys.readouterr().out
    assert "削除できません" in out and "4 件削除しました" in out


def test_delete_says_when_a_channel_could_not_be_read(monkeypatch, channels, capsys):
    real = FakeRest.messages

    def messages(self, channel_id, limit=100, before=None):
        if channel_id == "500":
            raise DiscordError("GET -> HTTP 403", status=403)
        return real(self, channel_id, limit, before)

    monkeypatch.setattr(FakeRest, "messages", messages)
    assert control_cli.cmd_delete(channels, url="UgkxPOST1234") == 1
    assert "読めません" in capsys.readouterr().out


def test_deleting_a_notification_cancels_its_pending_retry(channels, tmp_path):
    store = FileStateStore(tmp_path / "state.json")
    state = store.load()
    url = "https://www.youtube.com/watch?v=abcdefghijk"
    for key, channel in (("big|video|abcdefghijk", "500"), ("off|video|abcdefghijk", "600")):
        state.undelivered[key] = {"channel": channel, "url": url, "kind": "video",
                                  "first": NOW.isoformat(), "tries": 1, "done": None}
    store.save(state)
    FakeRest.history["600"].insert(0, {**_msg(4, url=url), "content": "🎬 新しい動画が公開されました\u200b"})
    assert control_cli.cmd_delete(channels, url="abcdefghijk", yes=True) == 0
    undelivered = _state(tmp_path)["undelivered"]
    assert undelivered["big|video|abcdefghijk"]["done"]  # its channel lost the notification: cancelled
    assert not undelivered["off|video|abcdefghijk"]["done"]  # only a preview was deleted there


def test_delete_needs_a_range_and_known_destinations(channels):
    for kwargs in ({}, {"since": "1h", "dest_names": ["nope"]}, {"last": 0},
                   {"since": "1h", "channel_ids": ["abc"]}, {"since": "1h", "ids": "x"}, {"since": "1h", "ids": ""},
                   {"since": "100000d"}):
        with pytest.raises(ConfigError):
            control_cli.cmd_delete(channels, **kwargs)
    assert FakeRest.deleted == []


@pytest.mark.parametrize("text, minutes", [("30m", 30), ("2h", 120), ("1.5h", 90), ("1d", 1440), (" 10 min ", 10)])
def test_parse_duration(text, minutes):
    assert control_cli.parse_duration(text) == timedelta(minutes=minutes)


@pytest.mark.parametrize("bad", ["", "1w", "-1h", "0m", "h", "1e9d", "9999999999999999d"])
def test_parse_duration_rejects(bad):
    with pytest.raises(ConfigError):
        control_cli.parse_duration(bad)


@pytest.mark.parametrize("text, expected", [
    ("https://www.youtube.com/watch?v=abcdefghijk&t=3", "abcdefghijk"),
    ("https://youtu.be/abcdefghijk", "abcdefghijk"),
    ("https://www.youtube.com/shorts/abcdefghijk", "abcdefghijk"),
    ("https://www.youtube.com/live/abcdefghijk?si=x", "abcdefghijk"),
    ("https://www.youtube.com/post/UgkxABC1234567", "UgkxABC1234567"),
    ("abcdefghijk", "abcdefghijk"),
])
def test_item_id(text, expected):
    assert control_cli.item_id(text) == expected


def test_stop_feature_is_advertised():
    assert "pause" in notifier.FEATURES


def test_stop_still_writes_the_marker_when_the_state_cannot_be_read(monkeypatch, cli, capsys):
    def broken(cfg, discord):
        class Store:
            def load(self):
                raise DiscordError("attachment download -> HTTP 503", status=503)
        return Store()

    monkeypatch.setattr(control_cli, "_store", broken)
    assert control_cli.cmd_stop(cli) == 0 and len(_markers()) == 1


def test_resume_says_when_a_stop_reaction_on_the_marker_goes_too(cli, capsys):
    assert control_cli.cmd_stop(cli) == 0
    _markers()[0]["reactions"] = _stop_reaction()["reactions"]
    capsys.readouterr()
    assert control_cli.cmd_resume(cli) == 0
    assert "一緒に消えました" in capsys.readouterr().out
