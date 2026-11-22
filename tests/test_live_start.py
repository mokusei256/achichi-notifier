"""A stream is announced twice: when its slot appears (📅) and when it goes on air (🔴)."""

from datetime import datetime, timedelta, timezone

import pytest

from achichi_notifier import control, notifier, render
from achichi_notifier.config import DEFAULT_EXCLUDE_LIVE_TITLES
from achichi_notifier.discord_api import DiscordError
from achichi_notifier.state import FileStateStore, State
from achichi_notifier.youtube import VideoRef, classify_video

from test_notifier import FakeDiscordClient, FakeYouTube, _cfg, _state, _write_state, fake_youtube  # noqa: F401

SLOT = render.HEADLINES["live_upcoming"]
START = render.HEADLINES["live_now"]
URL = "https://www.youtube.com/watch?v={}"


def _now():
    return datetime.now(timezone.utc)


class Watch(FakeYouTube):
    """Watch pages decide what a stream is now: video_id -> {status: upcoming | live | archive |
    blocked, scheduled / started / ended: datetime, title}. Listing badges come from the refs."""

    pages: dict = {}
    fetched: list = []

    def fetch_video_info(self, ref):
        Watch.fetched.append(ref.video_id)
        page = self.pages.get(ref.video_id)
        if page is None:
            return super().fetch_video_info(ref)
        if page["status"] == "blocked":
            return classify_video({"playabilityStatus": {"status": "LOGIN_REQUIRED"}}, ref)
        broadcast = {"isLiveNow": page["status"] == "live"}
        if page.get("scheduled") or page.get("started"):
            broadcast["startTimestamp"] = (page.get("scheduled") or page["started"]).isoformat()
        if page.get("ended"):
            broadcast["endTimestamp"] = page["ended"].isoformat()
        details = {"title": page.get("title") or ref.title, "isLiveContent": True,
                   "isUpcoming": page["status"] == "upcoming", "isLive": page["status"] == "live"}
        micro = {"publishDate": (_now() - timedelta(days=5)).isoformat(), "liveBroadcastDetails": broadcast}
        return classify_video({"videoDetails": details,
                               "microformat": {"playerMicroformatRenderer": micro}}, ref)


class Discord(FakeDiscordClient):
    """Remembers every message across runs; the duplicate guard reads them back with their text."""

    posted: list = []
    fail: dict = {}  # channel -> HTTP status

    def send(self, channel_id, payload):
        if channel_id in Discord.fail:
            status = Discord.fail[channel_id]
            raise DiscordError(f"POST /channels/{channel_id}/messages -> HTTP {status}", status=status)
        Discord.posted.append((channel_id, payload))
        return super().send(channel_id, payload)

    def recent_notices(self, channel_id, limit=50):
        notes = [(p["embeds"][0]["url"], p.get("content") or "")
                 for c, p in Discord.posted if c == channel_id and p.get("embeds")]
        return list(reversed(notes))[:limit]


@pytest.fixture(autouse=True)
def wired(fake_youtube, monkeypatch, tmp_path):  # noqa: F811 (fake_youtube runs first)
    Watch.pages, Watch.fetched = {}, []
    Discord.posted, Discord.fail = [], {}
    FakeDiscordClient.instances = []
    FakeDiscordClient.history = {}
    monkeypatch.setattr(notifier, "YouTubeClient", Watch)
    monkeypatch.setattr(notifier, "DiscordClient", Discord)
    monkeypatch.setattr(notifier, "make_store", lambda cfg, discord: FileStateStore(tmp_path / "state.json"))
    cfg = _cfg(tmp_path, discord_token="t", state_backend="discord", state_channel_id="999",
               exclude_live_titles=DEFAULT_EXCLUDE_LIVE_TITLES)
    notifier.run(cfg, role="primary")  # seed with the default (old) video
    Watch.fetched = []
    return cfg


def _run(cfg):
    Watch.fetched = []
    return notifier.run(cfg, role="primary")


def _sent(video_id, channel="1"):
    return [p["content"] for c, p in Discord.posted
            if c == channel and p.get("embeds") and p["embeds"][0]["url"] == URL.format(video_id)]


def _stream(video_id, hint, title="歌枠なのじゃ"):
    return VideoRef(video_id, {"streams"}, title=title, live_hint=hint)


def _set_listing(*refs):
    FakeYouTube.videos = [*refs, VideoRef("old00000001", {"rss", "videos"}, title="old")]


def test_slot_and_then_its_start_are_announced_once_each(wired, tmp_path):
    _set_listing(_stream("slot0000001", "upcoming"))
    Watch.pages = {"slot0000001": {"status": "upcoming", "scheduled": _now() + timedelta(hours=2)}}
    assert _run(wired) == 0
    assert _sent("slot0000001") == [SLOT]
    assert _state(tmp_path)["live"]["slot0000001"]["done"] is None

    _run(wired)  # long before the start time: the watch page is not fetched again
    assert Watch.fetched == [] and _sent("slot0000001") == [SLOT]

    started = _now() - timedelta(minutes=2)
    _set_listing(_stream("slot0000001", "live"))
    Watch.pages["slot0000001"] = {"status": "live", "started": started}
    _run(wired)
    assert _sent("slot0000001") == [SLOT, START]
    start = [p for c, p in Discord.posted if p["content"] == START][0]
    assert start["embeds"][0]["timestamp"] == started.isoformat()  # when it began, not the slot date
    assert _state(tmp_path)["live"]["slot0000001"]["done"] == "started"

    _run(wired)  # still on air: nothing more, and no more watch-page fetches
    _set_listing(_stream("slot0000001", None))
    Watch.pages["slot0000001"] = {"status": "archive", "started": started, "ended": _now()}
    _run(wired)
    assert _sent("slot0000001") == [SLOT, START] and Watch.fetched == []


def test_start_is_found_from_the_start_time_even_without_a_live_badge(wired, tmp_path):
    _set_listing(_stream("nobadge0001", "upcoming"))
    Watch.pages = {"nobadge0001": {"status": "upcoming", "scheduled": _now() + timedelta(minutes=5)}}
    _run(wired)
    assert _sent("nobadge0001") == [SLOT]
    Watch.pages["nobadge0001"] = {"status": "upcoming", "scheduled": _now() + timedelta(minutes=5)}
    _run(wired)  # within LIVE_START_LEAD: looked at, not on air yet
    assert Watch.fetched == ["nobadge0001"] and _sent("nobadge0001") == [SLOT]
    Watch.pages["nobadge0001"] = {"status": "live", "started": _now()}  # the tab still says upcoming
    _run(wired)
    assert _sent("nobadge0001") == [SLOT, START]


def test_slot_seen_before_this_version_learns_its_start_time_once(wired, tmp_path):
    state = _state(tmp_path)
    state["videos"]["oldslot0001"] = "2026-11-18T05:47:36+00:00"  # announced by the old version
    _write_state(tmp_path, state)
    _set_listing(_stream("oldslot0001", "upcoming"))
    Watch.pages = {"oldslot0001": {"status": "upcoming", "scheduled": _now() + timedelta(hours=3)}}
    _run(wired)
    assert Watch.fetched == ["oldslot0001"] and Discord.posted == []
    _run(wired)
    assert Watch.fetched == []
    _set_listing(_stream("oldslot0001", "live"))
    Watch.pages["oldslot0001"] = {"status": "live", "started": _now()}
    _run(wired)
    assert _sent("oldslot0001") == [START]


def test_stream_already_over_when_checked_gets_no_start_notice(wired, tmp_path):
    # e.g. the PC was asleep during the whole stream (today's _NIc65kdfsI is like this)
    _set_listing(_stream("over0000001", "upcoming"))
    Watch.pages = {"over0000001": {"status": "upcoming", "scheduled": _now() + timedelta(minutes=1)}}
    _run(wired)
    _set_listing(_stream("over0000001", None))
    Watch.pages["over0000001"] = {"status": "archive", "started": _now() - timedelta(hours=2),
                                  "ended": _now() - timedelta(minutes=30)}
    _run(wired)
    assert _sent("over0000001") == [SLOT]
    assert _state(tmp_path)["live"]["over0000001"]["done"] == "ended"
    _run(wired)
    assert Watch.fetched == []


def test_archives_announced_before_this_version_are_left_alone(wired, tmp_path):
    state = _state(tmp_path)
    state["videos"]["_NIc65kdfsI"] = "2026-11-18T05:47:36+00:00"
    _write_state(tmp_path, state)
    _set_listing(_stream("_NIc65kdfsI", None))
    Watch.pages = {"_NIc65kdfsI": {"status": "archive", "started": _now() - timedelta(hours=4),
                                   "ended": _now() - timedelta(hours=2)}}
    _run(wired)
    assert Watch.fetched == [] and Discord.posted == []


def test_free_chat_slot_going_live_is_not_announced(wired, tmp_path):
    _set_listing(_stream("freechat001", "upcoming", title="【フリーチャット】お知らせ"))
    _run(wired)
    _set_listing(_stream("freechat001", "live", title="【フリーチャット】お知らせ"))
    Watch.pages = {"freechat001": {"status": "live", "started": _now()}}
    _run(wired)
    assert Discord.posted == [] and Watch.fetched == []
    assert _state(tmp_path)["live"]["freechat001"]["done"] == "excluded"


def test_the_slot_notice_does_not_count_as_the_start_notice_but_a_start_notice_does(wired, tmp_path):
    _set_listing(_stream("guard000001", "upcoming"), _stream("guard000002", "upcoming"))
    Watch.pages = {v: {"status": "upcoming", "scheduled": _now() + timedelta(hours=1)}
                   for v in ("guard000001", "guard000002")}
    _run(wired)
    # Another runner already posted the start of guard000002 (racing on the shared state).
    Discord.posted.append(("1", {"content": START, "embeds": [{"url": URL.format("guard000002")}]}))
    _set_listing(_stream("guard000001", "live"), _stream("guard000002", "live"))
    Watch.pages = {v: {"status": "live", "started": _now()} for v in ("guard000001", "guard000002")}
    _run(wired)
    assert _sent("guard000001") == [SLOT, START]
    assert _sent("guard000002") == [SLOT, START]  # not twice
    assert _state(tmp_path)["live"]["guard000002"]["done"] == "started"


def test_start_notice_that_reached_nobody_is_tried_again(wired, tmp_path):
    _set_listing(_stream("retry000001", "upcoming"))
    Watch.pages = {"retry000001": {"status": "upcoming", "scheduled": _now() + timedelta(hours=1)}}
    _run(wired)
    _set_listing(_stream("retry000001", "live"))
    Watch.pages["retry000001"] = {"status": "live", "started": _now()}
    Discord.fail = {"1": 503}
    assert _run(wired) == 1
    assert _state(tmp_path)["live"]["retry000001"]["done"] is None
    Discord.fail = {}
    _set_listing(_stream("retry000001", "upcoming"))  # even if the tab badge is gone meanwhile
    _run(wired)
    assert _sent("retry000001") == [SLOT, START]


def test_start_notice_missed_by_one_destination_is_resent_only_there(wired, tmp_path):
    store = FileStateStore(tmp_path / "state.json")
    state = store.load()
    state.destinations["friend"] = {"channels": {"default": "500"}, "enabled": True}
    state.destinations_updated = "2099-01-01T00:00:00+00:00"
    store.save(state)
    _set_listing(_stream("partial0001", "upcoming"))
    Watch.pages = {"partial0001": {"status": "upcoming", "scheduled": _now() + timedelta(hours=1)}}
    _run(wired)
    assert _sent("partial0001", "500") == [SLOT]
    _set_listing(_stream("partial0001", "live"))
    Watch.pages["partial0001"] = {"status": "live", "started": _now()}
    Discord.fail = {"500": 503}
    _run(wired)
    undelivered = _state(tmp_path)["undelivered"]
    assert list(undelivered) == ["friend|live_start|partial0001"]
    assert undelivered["friend|live_start|partial0001"]["item_type"] == "live_start"
    Discord.fail = {}
    _run(wired)  # the 📅 in channel 500 has the same link: it must not count as delivered
    assert _sent("partial0001", "500") == [SLOT, START]
    assert _sent("partial0001", "1") == [SLOT, START]


def test_stream_found_already_on_air_is_announced_once(wired, tmp_path):
    _set_listing(_stream("instant0001", "live"))
    Watch.pages = {"instant0001": {"status": "live", "started": _now()}}
    _run(wired)
    _run(wired)
    assert _sent("instant0001") == [START]
    assert Watch.fetched == []


def test_unreadable_watch_page_keeps_the_slot_waiting(wired, tmp_path):
    scheduled = _now() - timedelta(minutes=1)
    _set_listing(_stream("blocked0001", "upcoming"))
    Watch.pages = {"blocked0001": {"status": "upcoming", "scheduled": scheduled}}
    _run(wired)
    Watch.pages["blocked0001"] = {"status": "blocked"}
    _set_listing(_stream("blocked0001", None))
    _run(wired)
    entry = _state(tmp_path)["live"]["blocked0001"]
    assert entry["done"] is None and entry["scheduled"] == scheduled.isoformat()
    Watch.pages["blocked0001"] = {"status": "live", "started": _now()}
    _run(wired)
    assert _sent("blocked0001") == [SLOT, START]


def test_slot_that_never_begins_stops_being_checked_until_the_tab_says_live(wired, tmp_path):
    late = _now() - notifier.LIVE_WAIT - timedelta(minutes=5)
    _set_listing(_stream("never000001", "upcoming"))
    Watch.pages = {"never000001": {"status": "upcoming", "scheduled": _now() + timedelta(minutes=1)}}
    _run(wired)
    Watch.pages["never000001"] = {"status": "upcoming", "scheduled": late}
    _run(wired)
    assert _state(tmp_path)["live"]["never000001"]["done"] == "stale"
    _run(wired)
    assert Watch.fetched == []
    _set_listing(_stream("never000001", "live"))
    Watch.pages["never000001"] = {"status": "live", "started": _now()}
    _run(wired)
    assert _sent("never000001") == [SLOT, START]


def test_start_while_stopped_is_listed_and_never_sent_later(wired, tmp_path):
    _set_listing(_stream("held0000001", "upcoming"))
    Watch.pages = {"held0000001": {"status": "upcoming", "scheduled": _now() + timedelta(hours=1)}}
    _run(wired)
    ms = int(_now().timestamp() * 1000)
    FakeDiscordClient.history = {"999": [{"id": str((ms - control.DISCORD_EPOCH_MS) << 22),
                                          "author": {"id": "bot"}, "reactions": [],
                                          "content": control.MARKER_PREFIX + "（テスト）"}]}
    _set_listing(_stream("held0000001", "live"))
    Watch.pages["held0000001"] = {"status": "live", "started": _now()}
    _run(wired)
    state = _state(tmp_path)
    assert URL.format("held0000001") + "#start" in state["muted"]
    assert state["live"]["held0000001"]["done"] == "started"
    FakeDiscordClient.history = {}
    _run(wired)
    assert _sent("held0000001") == [SLOT]


def test_seeding_never_announces_a_stream_already_on_air(monkeypatch, tmp_path):
    monkeypatch.setattr(notifier, "make_store", lambda cfg, discord: FileStateStore(tmp_path / "fresh.json"))
    cfg = _cfg(tmp_path, discord_token="t", state_backend="discord", state_channel_id="999")
    _set_listing(_stream("onair000001", "live"))
    Watch.pages = {"onair000001": {"status": "live", "started": _now()}}
    Discord.posted = []
    notifier.run(cfg, role="primary")
    notifier.run(cfg, role="primary")
    assert Discord.posted == []


def test_at_most_a_few_streams_are_checked_per_run(wired, tmp_path):
    ids = [f"many{n:07d}" for n in range(notifier.LIVE_CHECKS_PER_RUN + 2)]
    state = _state(tmp_path)
    for video_id in ids:
        state["videos"][video_id] = "2026-11-18T00:00:00+00:00"
    _write_state(tmp_path, state)
    _set_listing(*[_stream(v, "live") for v in ids])
    Watch.pages = {v: {"status": "live", "started": _now()} for v in ids}
    _run(wired)
    assert len(Watch.fetched) == notifier.LIVE_CHECKS_PER_RUN
    _run(wired)
    assert sorted(v for v in ids if _sent(v) == [START]) == ids


def test_live_tracking_merges_settled_first_and_is_pruned():
    waiting = {"scheduled": "2026-11-19T10:00:00+00:00", "done": None, "at": datetime.now(timezone.utc).isoformat()}
    mine = State(live={"a": dict(waiting), "b": {**waiting, "done": "started"}})
    theirs = State(live={"a": {**waiting, "done": "started"}, "b": dict(waiting), "c": dict(waiting)})
    mine.merge_from(theirs)
    assert {k: v["done"] for k, v in mine.live.items()} == {"a": "started", "b": "started", "c": None}
    old = State(live={"x": {**waiting, "at": "2020-01-01T00:00:00+00:00"}, "y": dict(waiting)})
    old.prune()
    assert list(old.live) == ["y"]
    assert State.from_dict(mine.to_dict()).live == mine.live


def _age_live_entry(tmp_path, video_id, minutes):
    state = _state(tmp_path)
    state["live"][video_id]["at"] = (_now() - timedelta(minutes=minutes)).isoformat()
    _write_state(tmp_path, state)


def test_slot_first_seen_with_an_unreadable_watch_page_learns_its_start_time_later(wired, tmp_path):
    _set_listing(_stream("nosched0001", "upcoming"))
    Watch.pages = {"nosched0001": {"status": "blocked"}}
    _run(wired)
    assert _sent("nosched0001") == [SLOT]
    assert _state(tmp_path)["live"]["nosched0001"]["scheduled"] is None
    Watch.pages["nosched0001"] = {"status": "upcoming", "scheduled": _now() + timedelta(minutes=3)}
    _run(wired)  # just tried: not again within LIVE_START_LEAD
    assert Watch.fetched == []
    _age_live_entry(tmp_path, "nosched0001", minutes=11)
    _run(wired)
    assert Watch.fetched == ["nosched0001"] and _state(tmp_path)["live"]["nosched0001"]["scheduled"]
    Watch.pages["nosched0001"] = {"status": "live", "started": _now()}  # the tab still says upcoming
    _run(wired)
    assert _sent("nosched0001") == [SLOT, START]


def test_slot_notice_that_arrived_although_the_send_looked_failed_does_not_hide_the_start(wired, tmp_path):
    class TimesOut(Discord):
        def send(self, channel_id, payload):
            Discord.posted.append((channel_id, payload))  # it did reach Discord...
            raise DiscordError("POST /channels/1/messages failed: ReadTimeout")  # ...but looked failed

    _set_listing(_stream("timeout0001", "upcoming"))
    Watch.pages = {"timeout0001": {"status": "upcoming", "scheduled": _now() + timedelta(minutes=30)}}
    notifier.DiscordClient = TimesOut
    try:
        assert _run(wired) == 1
    finally:
        notifier.DiscordClient = Discord
    assert "timeout0001" not in _state(tmp_path)["videos"]
    _set_listing(_stream("timeout0001", "live"))
    Watch.pages["timeout0001"] = {"status": "live", "started": _now()}
    _run(wired)  # first seen as new again, now on air: the 📅 with the same link is not the 🔴
    assert _sent("timeout0001") == [SLOT, START]


def test_a_stale_copy_never_undoes_an_announced_start():
    at = datetime.now(timezone.utc).isoformat()
    for mine, theirs, merged in (("stale", "started", "started"), ("started", "stale", "started"),
                                 (None, "stale", "stale"), ("stale", None, "stale"),
                                 ("ended", "started", "ended")):
        state = State(live={"x": {"scheduled": None, "done": mine, "at": at}})
        state.merge_from(State(live={"x": {"scheduled": None, "done": theirs, "at": at}}))
        assert state.live["x"]["done"] == merged, (mine, theirs)


def test_stream_on_another_channel_registered_with_watch_is_announced_when_it_starts(wired, tmp_path):
    state = _state(tmp_path)
    state["live"]["collab00001"] = {"scheduled": (_now() + timedelta(hours=1)).isoformat(), "done": None,
                                    "at": _now().isoformat(), "external": True, "title": "【オフコラボ】"}
    _write_state(tmp_path, state)
    Watch.pages = {"collab00001": {"status": "upcoming", "scheduled": _now() + timedelta(hours=1)}}
    _run(wired)  # an hour before: not looked at; never in this channel's tabs, never in the seen set
    assert Watch.fetched == [] and Discord.posted == []
    state = _state(tmp_path)
    state["live"]["collab00001"]["scheduled"] = (_now() + timedelta(minutes=5)).isoformat()
    _write_state(tmp_path, state)
    Watch.pages["collab00001"] = {"status": "live", "started": _now(), "title": "【オフコラボ】雑談"}
    _run(wired)
    assert _sent("collab00001") == [START]
    assert _state(tmp_path)["live"]["collab00001"]["done"] == "started"
    assert _state(tmp_path)["live"]["collab00001"]["external"] is True
    _run(wired)
    assert _sent("collab00001") == [START] and Watch.fetched == []


def test_watch_command_registers_lists_and_removes(wired, tmp_path, monkeypatch, capsys):
    from achichi_notifier import watch_cli

    assert watch_cli.video_id_of("https://www.youtube.com/watch?v=7sXGlZUV8rA&t=1") == "7sXGlZUV8rA"
    assert watch_cli.video_id_of("https://youtu.be/7sXGlZUV8rA") == "7sXGlZUV8rA"
    assert watch_cli.video_id_of("https://www.youtube.com/live/7sXGlZUV8rA?si=x") == "7sXGlZUV8rA"
    assert watch_cli.video_id_of("7sXGlZUV8rA") == "7sXGlZUV8rA"
    with pytest.raises(Exception):
        watch_cli.video_id_of("https://www.youtube.com/@AchichiMela")

    store = FileStateStore(tmp_path / "state.json")
    monkeypatch.setattr(watch_cli, "_client", lambda cfg: None)
    monkeypatch.setattr(watch_cli, "_store", lambda cfg, discord: store)
    monkeypatch.setattr(watch_cli, "YouTubeClient", lambda channel_id: Watch())
    start = _now() + timedelta(hours=2)
    Watch.pages = {"7sXGlZUV8rA": {"status": "upcoming", "scheduled": start, "title": "【オフコラボ】"},
                   "notastream1": {"status": "blocked"}}
    assert watch_cli.cmd_add(wired, "https://www.youtube.com/watch?v=7sXGlZUV8rA") == 0
    entry = _state(tmp_path)["live"]["7sXGlZUV8rA"]
    assert entry["external"] is True and entry["done"] is None and entry["scheduled"] == start.isoformat()
    assert watch_cli.cmd_add(wired, "notastream1") == 1  # watch page unreadable: not registered
    assert "notastream1" not in _state(tmp_path)["live"]
    assert watch_cli.cmd_list(wired) == 0 and "【オフコラボ】" in capsys.readouterr().out
    assert watch_cli.cmd_remove(wired, "7sXGlZUV8rA") == 0
    assert _state(tmp_path)["live"]["7sXGlZUV8rA"]["done"] == "removed"
    Watch.pages["7sXGlZUV8rA"] = {"status": "live", "started": _now()}
    state = _state(tmp_path)
    state["live"]["7sXGlZUV8rA"]["scheduled"] = _now().isoformat()
    _write_state(tmp_path, state)
    _run(wired)
    assert Discord.posted == []  # removed: never announced
