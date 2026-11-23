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


def _complete_listing(tmp_path, *refs):
    """Every tab answers with videos (a listing the bot can trust to say what is not listed)."""
    fillers = [VideoRef("fillshort01", {"shorts"}, is_short=True, title="s"),
               VideoRef("fillstrm001", {"streams"}, title="a")]
    state = _state(tmp_path)
    for ref in fillers:
        state["videos"][ref.video_id] = "2026-11-01T00:00:00+00:00"
    _write_state(tmp_path, state)
    _set_listing(*refs, *fillers)


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
                                 ("ended", "started", "started")):
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


def _watch_cli(monkeypatch, tmp_path):
    from achichi_notifier import watch_cli
    store = FileStateStore(tmp_path / "state.json")
    monkeypatch.setattr(watch_cli, "_client", lambda cfg: None)
    monkeypatch.setattr(watch_cli, "_store", lambda cfg, discord: store)
    monkeypatch.setattr(watch_cli, "YouTubeClient", lambda channel_id: Watch())
    return watch_cli


@pytest.mark.parametrize("settled", ["removed", "stale", "ended", "not_live"])
def test_registering_again_after_it_was_settled_watches_it_again(wired, tmp_path, monkeypatch, settled):
    watch_cli = _watch_cli(monkeypatch, tmp_path)
    Watch.pages = {"again000001": {"status": "upcoming", "scheduled": _now() + timedelta(minutes=5)}}
    assert watch_cli.cmd_add(wired, "again000001") == 0
    state = _state(tmp_path)
    state["live"]["again000001"]["done"] = settled  # e.g. a mistaken --remove, or a postponed collab
    _write_state(tmp_path, state)
    assert watch_cli.cmd_add(wired, "again000001") == 0
    assert _state(tmp_path)["live"]["again000001"]["done"] is None
    Watch.pages["again000001"] = {"status": "live", "started": _now()}
    _run(wired)
    assert _sent("again000001") == [START]
    assert watch_cli.cmd_add(wired, "again000001") == 0  # already announced: left alone
    assert _state(tmp_path)["live"]["again000001"]["done"] == "started"


def test_watched_stream_that_starts_early_is_noticed_within_half_an_hour(wired, tmp_path):
    state = _state(tmp_path)
    state["live"]["early000001"] = {"scheduled": (_now() + timedelta(hours=2)).isoformat(), "done": None,
                                    "at": _now().isoformat(), "external": True, "title": "コラボ"}
    _write_state(tmp_path, state)
    Watch.pages = {"early000001": {"status": "live", "started": _now()}}  # went live 2 hours early
    _run(wired)
    assert Watch.fetched == []  # looked at less than WATCH_RECHECK ago
    state = _state(tmp_path)
    state["live"]["early000001"]["at"] = (_now() - notifier.WATCH_RECHECK - timedelta(minutes=1)).isoformat()
    _write_state(tmp_path, state)
    _run(wired)
    assert _sent("early000001") == [START]


def test_watched_stream_that_stays_unreadable_long_after_its_start_is_given_up(wired, tmp_path):
    state = _state(tmp_path)
    state["live"]["gone0000001"] = {"scheduled": (_now() - notifier.LIVE_WAIT - timedelta(minutes=5)).isoformat(),
                                    "done": None, "at": _now().isoformat(), "external": True}
    _write_state(tmp_path, state)
    Watch.pages = {"gone0000001": {"status": "blocked"}}  # deleted or made private
    _complete_listing(tmp_path)
    _run(wired)  # one failed read is not enough
    entry = _state(tmp_path)["live"]["gone0000001"]
    assert entry["done"] is None and entry["unreadable_since"]
    Watch.pages["gone0000001"] = {"status": "upcoming", "scheduled": _now() + timedelta(minutes=5)}
    _run(wired)  # readable again (e.g. moved later): forgotten
    assert "unreadable_since" not in _state(tmp_path)["live"]["gone0000001"]
    state = _state(tmp_path)
    state["live"]["gone0000001"].update(scheduled=(_now() - notifier.LIVE_WAIT - timedelta(minutes=5)).isoformat(),
                                        unreadable_since=(_now() - timedelta(hours=2)).isoformat())
    _write_state(tmp_path, state)
    Watch.pages["gone0000001"] = {"status": "blocked"}
    _run(wired)
    assert _state(tmp_path)["live"]["gone0000001"]["done"] == "unreadable"
    _run(wired)
    assert Watch.fetched == []


def test_unreadable_watched_streams_are_throttled_and_never_crowd_out_one_in_its_window(wired, tmp_path):
    state = _state(tmp_path)
    for n in range(notifier.LIVE_CHECKS_PER_RUN + 1):  # deleted collabs registered days ahead
        state["live"][f"aaagone{n:04d}"] = {"scheduled": (_now() + timedelta(days=3)).isoformat(),
                                            "done": None, "at": "2026-11-01T00:00:00+00:00", "external": True}
    state["live"]["zzzsoon0001"] = {"scheduled": (_now() + timedelta(minutes=5)).isoformat(),
                                    "done": None, "at": _now().isoformat(), "external": True}
    _write_state(tmp_path, state)
    Watch.pages = {f"aaagone{n:04d}": {"status": "blocked"} for n in range(notifier.LIVE_CHECKS_PER_RUN + 1)}
    Watch.pages["zzzsoon0001"] = {"status": "live", "started": _now()}
    _run(wired)
    assert Watch.fetched[0] == "zzzsoon0001" and _sent("zzzsoon0001") == [START]
    _run(wired)
    assert Watch.fetched == ["aaagone0004", "aaagone0005"]  # the ones left over (5 per run)
    _run(wired)
    assert Watch.fetched == []  # every unreadable one was looked at less than WATCH_RECHECK ago


def test_an_announced_start_survives_a_watch_or_remove_saved_during_that_run(wired, tmp_path, monkeypatch):
    watch_cli = _watch_cli(monkeypatch, tmp_path)
    Watch.pages = {"race0000001": {"status": "upcoming", "scheduled": _now() + timedelta(minutes=5)}}
    assert watch_cli.cmd_add(wired, "race0000001") == 0
    Watch.pages["race0000001"] = {"status": "live", "started": _now()}

    class SavesMeanwhile(Discord):  # the CLI saves after the runner loaded and before it saves
        def send(self, channel_id, payload):
            result = super().send(channel_id, payload)
            if channel_id == "1":
                assert watch_cli.cmd_remove(wired, "race0000001") == 0
                assert watch_cli.cmd_add(wired, "race0000001") == 0
            return result

    class Blind(Discord):  # the duplicate guard cannot read Discord
        def recent_notices(self, channel_id, limit=50):
            raise DiscordError("GET /channels/1/messages -> HTTP 503", status=503)

    monkeypatch.setattr(notifier, "DiscordClient", SavesMeanwhile)
    _run(wired)
    assert _state(tmp_path)["live"]["race0000001"]["done"] == "started"
    monkeypatch.setattr(notifier, "DiscordClient", Blind)
    _run(wired)
    assert _sent("race0000001") == [START]


def test_a_newer_registration_wins_on_merge_and_otherwise_the_more_settled_one():
    old, new = "2026-11-22T08:00:00+00:00", "2026-11-22T09:00:00+00:00"
    runner = State(live={"x": {"done": "removed", "reg": old, "external": True}})
    runner.merge_from(State(live={"x": {"done": None, "reg": new, "external": True}}))
    assert runner.live["x"]["done"] is None and runner.live["x"]["reg"] == new
    cli = State(live={"x": {"done": None, "reg": new, "external": True}})
    cli.merge_from(State(live={"x": {"done": "removed", "reg": old, "external": True}}))
    assert cli.live["x"]["done"] is None
    same = State(live={"x": {"done": None, "reg": old, "external": True}})
    same.merge_from(State(live={"x": {"done": "started", "reg": old, "external": True}}))
    assert same.live["x"]["done"] == "started"


def test_registering_a_given_up_slot_again_restarts_the_wait_even_with_its_old_start_time(
        wired, tmp_path, monkeypatch):
    watch_cli = _watch_cli(monkeypatch, tmp_path)
    old_start = _now() - notifier.LIVE_WAIT - timedelta(hours=1)  # postponed without a new time
    Watch.pages = {"postpone001": {"status": "upcoming", "scheduled": old_start}}
    assert watch_cli.cmd_add(wired, "postpone001") == 0
    state = _state(tmp_path)
    state["live"]["postpone001"]["done"] = "stale"
    _write_state(tmp_path, state)
    assert watch_cli.cmd_add(wired, "postpone001") == 0
    _run(wired)
    assert _state(tmp_path)["live"]["postpone001"]["done"] is None  # not given up again at once
    Watch.pages["postpone001"] = {"status": "live", "started": _now()}
    _run(wired)
    assert _sent("postpone001") == [START]


def test_watched_start_is_not_announced_again_when_the_stream_shows_up_in_this_channel(wired, tmp_path, monkeypatch):
    state = _state(tmp_path)
    state["live"]["shared00001"] = {"scheduled": _now().isoformat(), "done": None,
                                    "at": _now().isoformat(), "external": True}
    _write_state(tmp_path, state)
    Watch.pages = {"shared00001": {"status": "live", "started": _now()}}
    _run(wired)
    assert _sent("shared00001") == [START]

    class Blind(Discord):
        def recent_notices(self, channel_id, limit=50):
            raise DiscordError("GET /channels/1/messages -> HTTP 503", status=503)

    monkeypatch.setattr(notifier, "DiscordClient", Blind)
    _set_listing(_stream("shared00001", "live"))  # e.g. YouTube lists the collab on both channels
    _run(wired)
    assert _sent("shared00001") == [START]
    assert "shared00001" in _state(tmp_path)["videos"]


def test_merge_keeps_the_newest_check_time_whichever_copy_wins():
    older = {"done": None, "reg": "2026-11-22T08:00:00+00:00", "external": True, "checked": "2026-11-22T08:00:00+00:00"}
    newer = {**older, "checked": "2026-11-22T09:30:00+00:00"}
    cli = State(live={"x": dict(older)})  # e.g. a CLI copy loaded before a runner looked at x
    cli.merge_from(State(live={"x": dict(newer)}))
    assert cli.live["x"]["checked"] == newer["checked"]


def test_streams_in_their_window_take_turns_when_there_are_more_than_the_cap(wired, tmp_path):
    ids = [f"window{n:05d}" for n in range(notifier.LIVE_CHECKS_PER_RUN + 1)]
    state = _state(tmp_path)
    for video_id in ids:  # all past their start, none begun yet
        state["live"][video_id] = {"scheduled": (_now() - timedelta(minutes=30)).isoformat(), "done": None,
                                   "at": _now().isoformat(), "external": True}
    _write_state(tmp_path, state)
    Watch.pages = {v: {"status": "upcoming", "scheduled": _now() - timedelta(minutes=30)} for v in ids}
    _run(wired)
    first = list(Watch.fetched)
    _run(wired)
    assert len(first) == notifier.LIVE_CHECKS_PER_RUN
    assert Watch.fetched[0] == next(v for v in ids if v not in first)  # the one left out goes first


def test_watched_stream_without_a_start_time_is_given_up_when_it_stays_unreadable(wired, tmp_path):
    reg = (_now() - notifier.LIVE_WAIT - timedelta(hours=1)).isoformat()
    state = _state(tmp_path)
    state["live"]["notime00001"] = {"scheduled": None, "done": None, "at": reg, "reg": reg, "external": True,
                                    "unreadable_since": (_now() - timedelta(hours=2)).isoformat()}
    _write_state(tmp_path, state)
    Watch.pages = {"notime00001": {"status": "blocked"}}
    _complete_listing(tmp_path)
    _run(wired)
    assert _state(tmp_path)["live"]["notime00001"]["done"] == "unreadable"


@pytest.mark.parametrize("watched", [False, True])
def test_stream_first_seen_on_air_whose_notice_reached_nobody_is_sent_next_run(wired, tmp_path, watched):
    if watched:  # registered with `watch`, then first listed in this channel's tabs while on air
        state = _state(tmp_path)
        state["live"]["firstair001"] = {"scheduled": (_now() + timedelta(hours=1)).isoformat(), "done": None,
                                        "at": _now().isoformat(), "external": True}
        _write_state(tmp_path, state)
    _set_listing(_stream("firstair001", "live"))
    Watch.pages = {"firstair001": {"status": "live", "started": _now()}}
    Discord.fail = {"1": 503}
    assert _run(wired) == 1
    assert _sent("firstair001") == []  # (only the #管理 alert went out)
    Discord.fail = {}
    _run(wired)
    assert _sent("firstair001") == [START]
    assert _state(tmp_path)["live"]["firstair001"]["done"] == "started"
    _run(wired)
    assert _sent("firstair001") == [START]


@pytest.mark.parametrize("listed_as", ["upcoming_unreadable", "archive"])
def test_watched_start_is_not_followed_by_another_notice_when_listed_here_later(
        wired, tmp_path, monkeypatch, listed_as):
    state = _state(tmp_path)
    state["live"]["listed00001"] = {"scheduled": _now().isoformat(), "done": None,
                                    "at": _now().isoformat(), "external": True}
    _write_state(tmp_path, state)
    Watch.pages = {"listed00001": {"status": "live", "started": _now()}}
    _run(wired)
    assert _sent("listed00001") == [START]

    class Blind(Discord):
        def recent_notices(self, channel_id, limit=50):
            raise DiscordError("GET /channels/1/messages -> HTTP 503", status=503)

    monkeypatch.setattr(notifier, "DiscordClient", Blind)
    state = _state(tmp_path)
    state["live"]["listed00001"]["checked"] = (_now() - timedelta(minutes=20)).isoformat()
    _write_state(tmp_path, state)
    if listed_as == "archive":
        _set_listing(_stream("listed00001", None))
        Watch.pages["listed00001"] = {"status": "archive", "started": _now() - timedelta(hours=1), "ended": _now()}
    else:  # the tab still says upcoming and the watch page cannot be read this once
        _set_listing(_stream("listed00001", "upcoming"))
        Watch.pages["listed00001"] = {"status": "blocked"}
    _run(wired)
    Watch.pages["listed00001"] = {"status": "live", "started": _now()}
    _run(wired)
    assert _sent("listed00001") == [START]
    assert _state(tmp_path)["live"]["listed00001"]["done"] == "started"


def test_a_recheck_a_few_seconds_short_of_half_an_hour_still_counts(wired, tmp_path):
    state = _state(tmp_path)
    state["live"]["slack000001"] = {"scheduled": (_now() + timedelta(hours=3)).isoformat(), "done": None,
                                    "at": _now().isoformat(), "external": True,
                                    "checked": (_now() - notifier.WATCH_RECHECK + timedelta(seconds=20)).isoformat()}
    _write_state(tmp_path, state)
    Watch.pages = {"slack000001": {"status": "live", "started": _now()}}  # went live early
    _run(wired)
    assert _sent("slack000001") == [START]


def test_start_left_undelivered_by_an_older_version_is_still_sent(wired, tmp_path):
    # e2cc4f9 / 52e4262 recorded "started" when queueing; then every destination failed.
    state = _state(tmp_path)
    state["live"]["oldver00001"] = {"scheduled": None, "done": "started", "at": _now().isoformat()}
    _write_state(tmp_path, state)
    _set_listing(_stream("oldver00001", "live"))
    Watch.pages = {"oldver00001": {"status": "live", "started": _now()}}
    _run(wired)
    assert _sent("oldver00001") == [START]


@pytest.mark.parametrize("settled", ["removed", "stale"])
def test_watched_stream_of_this_channel_still_gets_its_start_from_the_live_badge(wired, tmp_path, settled):
    _set_listing(_stream("both0000001", "upcoming"))
    Watch.pages = {"both0000001": {"status": "upcoming", "scheduled": _now() + timedelta(hours=1)}}
    _run(wired)
    state = _state(tmp_path)
    state["live"]["both0000001"].update(external=True, done=settled)  # watched, then removed / given up
    _write_state(tmp_path, state)
    _set_listing(_stream("both0000001", "live"))
    Watch.pages["both0000001"] = {"status": "live", "started": _now()}
    _run(wired)
    _run(wired)
    assert _sent("both0000001") == [SLOT, START]


def test_watched_streams_are_checked_even_when_this_channels_listing_fails(wired, tmp_path):
    state = _state(tmp_path)
    state["live"]["nolist00001"] = {"scheduled": _now().isoformat(), "done": None,
                                    "at": _now().isoformat(), "external": True}
    _write_state(tmp_path, state)
    FakeYouTube.rss_fails = True
    FakeYouTube.failing_tabs = {"videos", "shorts", "streams"}
    Watch.pages = {"nolist00001": {"status": "live", "started": _now()}}
    _run(wired)
    assert _sent("nolist00001") == [START]


def test_watched_start_first_queued_from_this_channels_tabs_is_retried_when_the_tabs_drop_it(wired, tmp_path):
    state = _state(tmp_path)
    state["live"]["dropped0001"] = {"scheduled": (_now() + timedelta(hours=2)).isoformat(), "done": None,
                                    "at": _now().isoformat(), "external": True, "checked": _now().isoformat()}
    _write_state(tmp_path, state)
    _set_listing(_stream("dropped0001", "live"))  # went live early, first seen in this channel's tabs
    Watch.pages = {"dropped0001": {"status": "live", "started": _now()}}
    Discord.fail = {"1": 503}
    _run(wired)
    Discord.fail = {}
    _set_listing()  # the tabs do not list it this time
    _run(wired)
    assert _sent("dropped0001") == [START]


def test_own_stream_that_was_watched_and_removed_is_found_by_its_start_time_again(wired, tmp_path):
    _set_listing(_stream("ownrm000001", "upcoming"))
    Watch.pages = {"ownrm000001": {"status": "upcoming", "scheduled": _now() + timedelta(minutes=5)}}
    _run(wired)
    state = _state(tmp_path)
    state["live"]["ownrm000001"].update(external=True, done="removed", reg=_now().isoformat())
    _write_state(tmp_path, state)
    Watch.pages["ownrm000001"] = {"status": "live", "started": _now()}  # the tab still says upcoming
    _run(wired)
    assert _sent("ownrm000001") == [SLOT, START]


def test_watched_stream_listed_here_with_a_live_badge_is_announced_without_waiting_for_its_window(wired, tmp_path):
    _set_listing(_stream("badge000001", "upcoming"))
    Watch.pages = {"badge000001": {"status": "upcoming", "scheduled": _now() + timedelta(hours=2)}}
    _run(wired)
    state = _state(tmp_path)
    state["live"]["badge000001"].update(external=True, reg=_now().isoformat(), checked=_now().isoformat())
    _write_state(tmp_path, state)
    _set_listing(_stream("badge000001", "live"))  # went live 2 hours early
    Watch.pages["badge000001"] = {"status": "live", "started": _now()}
    _run(wired)
    assert _sent("badge000001") == [SLOT, START] and Watch.fetched == ["badge000001"]


def test_an_unreadable_look_before_the_window_is_retried_after_ten_minutes(wired, tmp_path):
    state = _state(tmp_path)
    state["live"]["retry10m001"] = {"scheduled": (_now() + timedelta(hours=3)).isoformat(), "done": None,
                                    "at": "2026-11-01T00:00:00+00:00", "external": True}
    _write_state(tmp_path, state)
    Watch.pages = {"retry10m001": {"status": "blocked"}}
    _run(wired)
    checked = datetime.fromisoformat(_state(tmp_path)["live"]["retry10m001"]["checked"])
    next_look = checked + notifier.WATCH_RECHECK
    assert timedelta(minutes=9) < next_look - _now() <= notifier.LIVE_START_LEAD


def test_streams_waiting_in_their_window_never_hold_back_an_early_start_elsewhere(wired, tmp_path):
    state = _state(tmp_path)
    for n in range(notifier.LIVE_CHECKS_PER_RUN):  # late streams, still upcoming
        state["live"][f"late{n:07d}"] = {"scheduled": (_now() - timedelta(hours=1)).isoformat(), "done": None,
                                         "at": _now().isoformat(), "external": True}
    state["live"]["zearly00001"] = {"scheduled": (_now() + timedelta(hours=3)).isoformat(), "done": None,
                                    "at": "2026-11-01T00:00:00+00:00", "external": True}
    _write_state(tmp_path, state)
    Watch.pages = {f"late{n:07d}": {"status": "upcoming", "scheduled": _now() - timedelta(hours=1)}
                   for n in range(notifier.LIVE_CHECKS_PER_RUN)}
    Watch.pages["zearly00001"] = {"status": "live", "started": _now()}  # went live 3 hours early
    _run(wired)
    assert _sent("zearly00001") == [START]


def test_a_watched_stream_first_listed_here_is_not_looked_at_again_next_run(wired, tmp_path):
    state = _state(tmp_path)
    state["live"]["seenhere001"] = {"scheduled": (_now() + timedelta(hours=3)).isoformat(), "done": None,
                                    "at": "2026-11-01T00:00:00+00:00", "external": True,
                                    "checked": (_now() - timedelta(minutes=40)).isoformat()}
    _write_state(tmp_path, state)
    _set_listing(_stream("seenhere001", "upcoming"))
    Watch.pages = {"seenhere001": {"status": "upcoming", "scheduled": _now() + timedelta(hours=3)}}
    _run(wired)
    assert Watch.fetched == ["seenhere001"]  # check_videos looked at it (and sent the slot notice)
    _run(wired)
    assert Watch.fetched == []


@pytest.mark.parametrize("given_up", ["stale", "unreadable"])
def test_given_up_watch_listed_here_later_with_a_new_start_time_is_announced(wired, tmp_path, given_up):
    reg = (_now() - timedelta(hours=14)).isoformat()
    state = _state(tmp_path)
    state["live"]["revive00001"] = {"scheduled": (_now() - timedelta(hours=13)).isoformat(), "done": given_up,
                                    "at": reg, "reg": reg, "external": True}
    _write_state(tmp_path, state)
    new_start = _now() + timedelta(minutes=5)
    _set_listing(_stream("revive00001", "upcoming"))
    Watch.pages = {"revive00001": {"status": "upcoming", "scheduled": new_start}}
    _run(wired)
    entry = _state(tmp_path)["live"]["revive00001"]
    assert entry["done"] is None and entry["scheduled"] == new_start.isoformat()  # survived the merge
    Watch.pages["revive00001"] = {"status": "live", "started": _now()}  # no live badge on the tab
    _run(wired)
    assert _sent("revive00001") == [SLOT, START]


def test_a_watched_stream_listed_here_is_not_given_up_for_an_unreadable_page(wired, tmp_path):
    _set_listing(_stream("ownread0001", "upcoming"))
    Watch.pages = {"ownread0001": {"status": "upcoming", "scheduled": _now() - timedelta(minutes=1)}}
    _run(wired)
    long_ago = (_now() - notifier.LIVE_WAIT - timedelta(hours=1)).isoformat()
    state = _state(tmp_path)
    state["live"]["ownread0001"].update(external=True, reg=long_ago, unreadable_since=long_ago, scheduled=long_ago)
    _write_state(tmp_path, state)
    Watch.pages["ownread0001"] = {"status": "blocked"}
    _run(wired)
    assert _state(tmp_path)["live"]["ownread0001"]["done"] is None


def test_an_overdue_recheck_gets_the_last_slot_left_by_this_channels_streams(wired, tmp_path):
    own = [f"own{n:08d}" for n in range(notifier.LIVE_CHECKS_PER_RUN - 1)]
    state = _state(tmp_path)
    for video_id in own:  # this channel's slots, late, all due every run
        state["videos"][video_id] = "2026-11-22T00:00:00+00:00"
        state["live"][video_id] = {"scheduled": (_now() - timedelta(hours=1)).isoformat(), "done": None,
                                   "at": _now().isoformat()}
    state["live"]["alate000001"] = {"scheduled": (_now() - timedelta(hours=1)).isoformat(), "done": None,
                                    "at": _now().isoformat(), "external": True}
    state["live"]["zearly00002"] = {"scheduled": (_now() + timedelta(hours=3)).isoformat(), "done": None,
                                    "at": "2026-11-01T00:00:00+00:00", "external": True}
    _write_state(tmp_path, state)
    _set_listing(*[_stream(v, "upcoming") for v in own])
    Watch.pages = {v: {"status": "upcoming", "scheduled": _now() - timedelta(hours=1)} for v in own + ["alate000001"]}
    Watch.pages["zearly00002"] = {"status": "live", "started": _now()}  # went live 3 hours early
    _run(wired)
    assert _sent("zearly00002") == [START]


def test_unreadable_look_at_a_watched_stream_once_listed_here_is_retried_after_ten_minutes(wired, tmp_path):
    state = _state(tmp_path)
    state["videos"]["oncelist001"] = "2026-11-20T00:00:00+00:00"  # it was listed here once
    state["live"]["oncelist001"] = {"scheduled": (_now() + timedelta(hours=3)).isoformat(), "done": None,
                                    "at": "2026-11-01T00:00:00+00:00", "external": True}
    _write_state(tmp_path, state)
    Watch.pages = {"oncelist001": {"status": "blocked"}}
    _run(wired)
    checked = datetime.fromisoformat(_state(tmp_path)["live"]["oncelist001"]["checked"])
    assert checked + notifier.WATCH_RECHECK - _now() <= notifier.LIVE_START_LEAD


def test_watched_stream_no_longer_listed_here_is_given_up_when_it_stays_unreadable(wired, tmp_path):
    long_ago = (_now() - notifier.LIVE_WAIT - timedelta(hours=2)).isoformat()
    state = _state(tmp_path)
    state["videos"]["deleted0001"] = "2026-11-20T00:00:00+00:00"  # listed once, then deleted
    state["live"]["deleted0001"] = {"scheduled": long_ago, "done": None, "at": long_ago, "reg": long_ago,
                                    "external": True, "unreadable_since": long_ago}
    _write_state(tmp_path, state)
    Watch.pages = {"deleted0001": {"status": "blocked"}}
    _complete_listing(tmp_path)
    _run(wired)
    assert _state(tmp_path)["live"]["deleted0001"]["done"] == "unreadable"


def test_with_one_look_left_a_stream_in_its_window_and_the_rechecks_take_turns(wired, tmp_path):
    own = [f"own{n:08d}" for n in range(notifier.LIVE_CHECKS_PER_RUN - 1)]
    state = _state(tmp_path)
    for video_id in own:  # this channel's slots, late: they take 4 of the 5 looks every run
        state["videos"][video_id] = "2026-11-22T00:00:00+00:00"
        state["live"][video_id] = {"scheduled": (_now() - timedelta(hours=1)).isoformat(), "done": None,
                                   "at": _now().isoformat()}
    state["live"]["inwindow001"] = {"scheduled": (_now() - timedelta(minutes=5)).isoformat(), "done": None,
                                    "at": _now().isoformat(), "external": True,
                                    "checked": (_now() - timedelta(minutes=5)).isoformat()}
    for n in range(3):  # streams with an unknown start time: a recheck is due every run
        state["live"][f"nostart{n:04d}"] = {"scheduled": None, "done": None, "external": True,
                                            "at": "2026-11-01T00:00:00+00:00",
                                            "checked": (_now() - timedelta(minutes=11)).isoformat()}
    _write_state(tmp_path, state)
    _set_listing(*[_stream(v, "upcoming") for v in own])
    Watch.pages = {v: {"status": "upcoming", "scheduled": _now() - timedelta(hours=1)} for v in own}
    Watch.pages.update({f"nostart{n:04d}": {"status": "blocked"} for n in range(3)})
    Watch.pages["inwindow001"] = {"status": "live", "started": _now()}
    for _ in range(3):
        _run(wired)
    assert _sent("inwindow001") == [START]


def test_a_run_whose_listing_failed_never_gives_up_a_watched_stream_listed_here(wired, tmp_path):
    long_ago = (_now() - notifier.LIVE_WAIT - timedelta(hours=3)).isoformat()
    state = _state(tmp_path)
    state["videos"]["ownlong0001"] = "2026-11-20T00:00:00+00:00"
    state["live"]["ownlong0001"] = {"scheduled": long_ago, "done": None, "at": long_ago, "reg": long_ago,
                                    "external": True, "unreadable_since": long_ago}
    _write_state(tmp_path, state)
    FakeYouTube.rss_fails = True
    FakeYouTube.failing_tabs = {"videos", "shorts", "streams"}  # nothing listed this run
    Watch.pages = {"ownlong0001": {"status": "blocked"}}
    _run(wired)
    assert _state(tmp_path)["live"]["ownlong0001"]["done"] is None


def test_a_given_up_watched_stream_listed_here_is_looked_at_like_any_slot_of_this_channel(wired, tmp_path):
    state = _state(tmp_path)
    state["videos"]["ownunrd0001"] = "2026-11-20T00:00:00+00:00"
    state["live"]["ownunrd0001"] = {"scheduled": (_now() - timedelta(minutes=1)).isoformat(), "done": "unreadable",
                                    "at": _now().isoformat(), "reg": _now().isoformat(), "external": True}
    _write_state(tmp_path, state)
    _set_listing(_stream("ownunrd0001", "upcoming"))  # no live badge
    Watch.pages = {"ownunrd0001": {"status": "live", "started": _now()}}
    _run(wired)
    assert _sent("ownunrd0001") == [START]


def test_a_watched_slot_of_this_channel_in_its_window_is_looked_at_with_the_other_slots(wired, tmp_path):
    own = [f"own{n:08d}" for n in range(notifier.LIVE_CHECKS_PER_RUN - 1)]
    state = _state(tmp_path)
    for video_id in own + ["vwatched001"]:
        state["videos"][video_id] = "2026-11-22T00:00:00+00:00"
        state["live"][video_id] = {"scheduled": (_now() - timedelta(minutes=30)).isoformat(), "done": None,
                                   "at": _now().isoformat()}
    state["live"]["vwatched001"].update(external=True, reg=_now().isoformat(), checked=_now().isoformat())
    for n in range(3):  # watched streams elsewhere with an unknown start: rechecks keep coming due
        state["live"][f"nostart{n:04d}"] = {"scheduled": None, "done": None, "external": True,
                                            "at": "2026-11-01T00:00:00+00:00",
                                            "checked": (_now() - timedelta(minutes=30)).isoformat()}
    _write_state(tmp_path, state)
    _set_listing(_stream("vwatched001", "upcoming"), *[_stream(v, "upcoming") for v in own])
    Watch.pages = {v: {"status": "upcoming", "scheduled": _now() - timedelta(minutes=30)} for v in own}
    Watch.pages["vwatched001"] = {"status": "live", "started": _now()}  # no live badge on the tab
    _run(wired)
    assert _sent("vwatched001") == [START]


@pytest.mark.parametrize("collab_start", [timedelta(minutes=-5), timedelta(hours=2)])  # in its window / early
@pytest.mark.parametrize("extra_slots, watched_here", [(0, False), (0, True), (1, False)])
def test_this_channels_slots_using_up_their_looks_never_keep_a_collab_from_being_looked_at(
        wired, tmp_path, extra_slots, watched_here, collab_start):
    n_own = notifier.LIVE_CHECKS_PER_RUN + extra_slots - watched_here
    own = [f"own{n:08d}" for n in range(n_own)] + (["vwatched002"] if watched_here else [])
    state = _state(tmp_path)
    for video_id in own:  # this channel's slots, late: together they take every look of the tab loop
        state["videos"][video_id] = "2026-11-22T00:00:00+00:00"
        state["live"][video_id] = {"scheduled": (_now() - timedelta(hours=1)).isoformat(), "done": None,
                                   "at": _now().isoformat()}
    if watched_here:  # a collab hosted here, also registered with `watch`
        state["live"]["vwatched002"].update(external=True, reg=_now().isoformat(), checked=_now().isoformat())
    state["live"]["collab00001"] = {"scheduled": (_now() + collab_start).isoformat(), "done": None,
                                    "at": _now().isoformat(), "external": True,
                                    "checked": (_now() - notifier.WATCH_RECHECK).isoformat()}
    _write_state(tmp_path, state)
    _set_listing(*[_stream(v, "upcoming") for v in own])
    Watch.pages = {v: {"status": "upcoming", "scheduled": _now() - timedelta(hours=1)} for v in own}
    Watch.pages["collab00001"] = {"status": "live", "started": _now()}  # on another channel
    _run(wired)
    # this channel's slots still get every look of their own (the one past the cap waits)
    assert len(set(own) & set(Watch.fetched)) == notifier.LIVE_CHECKS_PER_RUN
    assert _sent("collab00001") == [START]


@pytest.mark.parametrize("given_up", ["removed", "unreadable"])
def test_a_watch_given_up_or_removed_and_listed_here_keeps_what_it_learns(wired, tmp_path, given_up):
    old_start = _now() - timedelta(hours=1)
    state = _state(tmp_path)
    state["videos"]["postpone001"] = "2026-11-20T00:00:00+00:00"
    state["live"]["postpone001"] = {"scheduled": old_start.isoformat(), "done": given_up, "external": True,
                                    "at": _now().isoformat(), "reg": _now().isoformat(),
                                    "checked": _now().isoformat()}
    _write_state(tmp_path, state)
    tomorrow = _now() + timedelta(days=1)
    _set_listing(_stream("postpone001", "upcoming"))
    Watch.pages = {"postpone001": {"status": "upcoming", "scheduled": tomorrow}}  # moved to tomorrow
    _run(wired)
    assert Watch.fetched == ["postpone001"]
    entry = _state(tmp_path)["live"]["postpone001"]
    assert entry["scheduled"] == tomorrow.isoformat() and entry["done"] is None  # survived the merge
    # removed by the user: a plain slot of this channel; given up by the bot: watched again
    assert bool(entry.get("external")) is (given_up == "unreadable")
    _run(wired)
    assert Watch.fetched == []  # waits for its new start time


@pytest.mark.parametrize("given_up", ["removed", "unreadable"])
def test_a_watch_given_up_without_a_start_time_and_listed_here_is_looked_at_right_away(wired, tmp_path, given_up):
    state = _state(tmp_path)
    state["videos"]["nostrt00001"] = "2026-11-20T00:00:00+00:00"
    state["live"]["nostrt00001"] = {"scheduled": None, "done": given_up, "external": True,
                                    "at": "2026-11-20T00:00:00+00:00", "reg": "2026-11-20T00:00:00+00:00",
                                    "checked": (_now() - timedelta(minutes=1)).isoformat()}
    _write_state(tmp_path, state)
    _set_listing(_stream("nostrt00001", "upcoming"))  # the tab has no live badge yet
    Watch.pages = {"nostrt00001": {"status": "live", "started": _now()}}
    _run(wired)
    assert _sent("nostrt00001") == [START]


def test_registering_again_counts_rechecks_from_the_registration(wired, tmp_path, monkeypatch):
    watch_cli = _watch_cli(monkeypatch, tmp_path)
    state = _state(tmp_path)
    state["live"]["reagain0001"] = {"scheduled": (_now() + timedelta(hours=3)).isoformat(), "done": "removed",
                                    "at": _now().isoformat(), "external": True, "reg": _now().isoformat(),
                                    "checked": (_now() - timedelta(hours=2)).isoformat()}  # an old look
    _write_state(tmp_path, state)
    Watch.pages = {"reagain0001": {"status": "upcoming", "scheduled": _now() + timedelta(hours=3)}}
    assert watch_cli.cmd_add(wired, "reagain0001") == 0  # reads the page
    _run(wired)
    assert Watch.fetched == []  # next look WATCH_RECHECK after the registration


def test_a_given_up_watch_listed_here_once_is_still_watched_after_it_leaves_the_tabs(wired, tmp_path):
    state = _state(tmp_path)
    state["videos"]["leaves00001"] = "2026-11-20T00:00:00+00:00"
    state["live"]["leaves00001"] = {"scheduled": (_now() - timedelta(hours=14)).isoformat(), "done": "unreadable",
                                    "external": True, "at": _now().isoformat(),
                                    "reg": (_now() - timedelta(hours=15)).isoformat()}
    _write_state(tmp_path, state)
    new_start = _now() + timedelta(minutes=5)
    _set_listing(_stream("leaves00001", "upcoming"))  # listed once, readable again with a new time
    Watch.pages = {"leaves00001": {"status": "upcoming", "scheduled": new_start}}
    _run(wired)
    _set_listing()  # no longer listed here (e.g. the host's channel lists it)
    Watch.pages["leaves00001"] = {"status": "live", "started": _now()}
    _run(wired)
    assert _sent("leaves00001") == [START]


def test_a_removed_watch_first_listed_here_keeps_the_start_time_learned_there(wired, tmp_path):
    state = _state(tmp_path)
    state["live"]["earlier0001"] = {"scheduled": (_now() + timedelta(hours=3)).isoformat(), "done": "removed",
                                    "external": True, "at": _now().isoformat(), "reg": _now().isoformat()}
    _write_state(tmp_path, state)
    new_start = _now() + timedelta(minutes=5)  # moved earlier after the removal
    _set_listing(_stream("earlier0001", "upcoming"))
    Watch.pages = {"earlier0001": {"status": "upcoming", "scheduled": new_start}}
    _run(wired)  # new here: the slot notice, and its start time is learned
    assert _state(tmp_path)["live"]["earlier0001"]["scheduled"] == new_start.isoformat()
    Watch.pages["earlier0001"] = {"status": "live", "started": _now()}  # no live badge on the tab
    _run(wired)
    assert _sent("earlier0001") == [SLOT, START]


@pytest.mark.parametrize("stored_reg", [timedelta(0), timedelta(hours=1)])  # the latter: a clock ahead
def test_a_watch_saved_while_a_run_adopts_the_entry_wins(wired, tmp_path, monkeypatch, stored_reg):
    watch_cli = _watch_cli(monkeypatch, tmp_path)
    reg = (_now() + stored_reg).isoformat()
    state = _state(tmp_path)
    state["videos"]["racing00001"] = "2026-11-20T00:00:00+00:00"
    state["live"]["racing00001"] = {"scheduled": (_now() + timedelta(hours=3)).isoformat(), "done": "removed",
                                    "external": True, "at": reg, "reg": reg}
    _write_state(tmp_path, state)
    Watch.pages = {"racing00001": {"status": "upcoming", "scheduled": _now() + timedelta(hours=3)}}
    _set_listing(_stream("racing00001", "upcoming"))
    real_fetch_tab = FakeYouTube.fetch_tab

    def fetch_tab_then_register(self, tab):  # the user registers it again while this run reads the tabs
        if tab == "streams":
            assert watch_cli.cmd_add(wired, "racing00001") == 0
        return real_fetch_tab(self, tab)

    monkeypatch.setattr(FakeYouTube, "fetch_tab", fetch_tab_then_register)
    _run(wired)
    entry = _state(tmp_path)["live"]["racing00001"]
    assert entry.get("external") and entry["done"] is None  # the registration survived


def test_a_given_up_watch_first_listed_here_waits_again_from_now(wired, tmp_path):
    long_ago = (_now() - notifier.LIVE_WAIT - timedelta(hours=2)).isoformat()
    state = _state(tmp_path)
    state["live"]["firstnew001"] = {"scheduled": None, "done": "unreadable", "external": True,
                                    "at": long_ago, "reg": long_ago}
    _write_state(tmp_path, state)
    _set_listing(_stream("firstnew001", "upcoming"))  # new here; its page is readable again, no start time
    Watch.pages = {"firstnew001": {"status": "upcoming"}}
    _run(wired)
    assert _sent("firstnew001") == [SLOT]
    state = _state(tmp_path)
    state["live"]["firstnew001"]["checked"] = (_now() - notifier.LIVE_START_LEAD - timedelta(minutes=1)).isoformat()
    _write_state(tmp_path, state)
    _run(wired)  # looked at again, still upcoming: the LIVE_WAIT counts from its revival, not the old one
    assert Watch.fetched == ["firstnew001"] and _state(tmp_path)["live"]["firstnew001"]["done"] is None


def test_a_given_up_watch_without_a_start_time_listed_here_is_not_given_up_again_for_its_old_registration(
        wired, tmp_path):
    long_ago = (_now() - notifier.LIVE_WAIT - timedelta(hours=2)).isoformat()
    state = _state(tmp_path)
    state["videos"]["seenlong001"] = "2026-11-20T00:00:00+00:00"
    state["live"]["seenlong001"] = {"scheduled": None, "done": "unreadable", "external": True,
                                    "at": long_ago, "reg": long_ago}
    _write_state(tmp_path, state)
    _set_listing(_stream("seenlong001", "upcoming"))
    Watch.pages = {"seenlong001": {"status": "upcoming"}}  # readable again, still no start time
    _run(wired)
    entry = _state(tmp_path)["live"]["seenlong001"]
    assert Watch.fetched == ["seenlong001"] and entry["done"] is None and entry.get("external")


@pytest.mark.parametrize("miss", ["streams_tab_fails", "everything_fails"])
def test_a_listed_watch_without_a_start_time_survives_a_run_whose_listing_misses_it(wired, tmp_path, miss):
    reg = (_now() - notifier.LIVE_WAIT - timedelta(hours=1)).isoformat()  # registered 13 hours ago
    state = _state(tmp_path)
    state["videos"]["colisted001"] = "2026-11-22T00:00:00+00:00"  # listed here all along
    state["live"]["colisted001"] = {"scheduled": None, "done": None, "external": True, "at": reg, "reg": reg,
                                    "checked": (_now() - timedelta(minutes=11)).isoformat()}
    _write_state(tmp_path, state)
    Watch.pages = {"colisted001": {"status": "upcoming"}}  # still no start time
    if miss == "streams_tab_fails":
        FakeYouTube.failing_tabs = {"streams"}
    else:
        FakeYouTube.failing_tabs, FakeYouTube.rss_fails = {"videos", "shorts", "streams"}, True
    _set_listing(_stream("colisted001", "upcoming"))
    _run(wired)  # its look is due, but this run's listing does not have it
    FakeYouTube.failing_tabs, FakeYouTube.rss_fails = set(), False
    Watch.pages["colisted001"] = {"status": "live", "started": _now()}
    state = _state(tmp_path)
    state["live"]["colisted001"]["at"] = (_now() - notifier.LIVE_START_LEAD - timedelta(minutes=1)).isoformat()
    _write_state(tmp_path, state)
    _run(wired)  # listed again, the tab still says "upcoming"
    assert _sent("colisted001") == [START]
