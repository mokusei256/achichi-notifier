"""A changed thumbnail of this channel's stream, video or short is announced once, old and new."""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from achichi_notifier import destinations as dests
from achichi_notifier import notifier, render, thumbnails
from achichi_notifier.discord_api import DiscordClient, DiscordError
from achichi_notifier.state import FileStateStore, State
from achichi_notifier.youtube import ThumbUnreachable, VideoRef, YouTubeError

from test_notifier import THUMB, FakeDiscordClient, FakeYouTube, _cfg, _state, _write_state, fake_youtube  # noqa: F401

HEADLINE = render.HEADLINES["thumb_live"]


class Discord(FakeDiscordClient):
    posted: list = []  # (channel, payload, files)
    fail: dict = {}  # channel -> HTTP status
    no_files: set = set()  # channels where the bot may not attach files

    def send(self, channel_id, payload, files=None):
        if files and channel_id in Discord.no_files:
            raise DiscordError(f"POST /channels/{channel_id}/messages -> HTTP 403: Missing Permissions",
                               status=403)
        if channel_id in Discord.fail:
            status = Discord.fail[channel_id]
            raise DiscordError(f"POST /channels/{channel_id}/messages -> HTTP {status}", status=status)
        Discord.posted.append((channel_id, payload, list(files or [])))
        return super().send(channel_id, payload)

    def recent_notices(self, channel_id, limit=50):
        notes = [(e["url"], p.get("content") or "") for c, p, _ in Discord.posted if c == channel_id
                 for e in p.get("embeds") or [] if e.get("url")]
        return list(reversed(notes))[:limit]


@pytest.fixture(autouse=True)
def wired(fake_youtube, monkeypatch, tmp_path):  # noqa: F811
    Discord.posted, Discord.fail, Discord.no_files = [], {}, set()
    FakeDiscordClient.instances, FakeDiscordClient.history = [], {}
    monkeypatch.setattr(notifier, "DiscordClient", Discord)
    monkeypatch.setattr(notifier, "make_store", lambda cfg, discord: FileStateStore(tmp_path / "state.json"))
    FakeYouTube.videos = [_ref("arch0000001", "streams"), _ref("vid00000001", "videos"),
                          _ref("short000001", "shorts")]
    cfg = _cfg(tmp_path, discord_token="t", state_backend="discord", state_channel_id="999",
               thumb_dir=tmp_path / "thumbs")
    notifier.run(cfg, role="primary")  # learns the channel and starts tracking its thumbnails
    return cfg


def _ref(video_id, tab, hint=None, title=None):
    return VideoRef(video_id, {tab}, is_short=tab == "shorts", title=title or f"title {video_id}",
                    live_hint=hint)


def _run(cfg):
    return notifier.run(cfg, role="primary")


def _notices(channel="1"):
    return [(p, f) for c, p, f in Discord.posted if c == channel and p["content"].endswith(HEADLINE)]


def _admin():
    return [p["content"] for c, p, _ in Discord.posted if c == "999"]


def _later(tmp_path, minutes):
    """Move every thumbnail time stamp back, as if `minutes` had passed."""
    state = _state(tmp_path)

    def back(value):
        return (datetime.fromisoformat(value) - timedelta(minutes=minutes)).isoformat() if value else value
    for entry in state["thumbs"].values():
        for key in ("since", "listed", "checked", "found"):
            entry[key] = back(entry.get(key))
    state["thumbs_listed"] = back(state.get("thumbs_listed"))
    state["thumbs_listed_ok"] = back(state.get("thumbs_listed_ok"))
    _write_state(tmp_path, state)


def _change(video_id, seconds=5):
    version = int(time.time()) + seconds
    FakeYouTube.thumbs[video_id] = version
    return version


def _add_dest(tmp_path, name, channel, kinds):
    store = FileStateStore(tmp_path / "state.json")
    state = store.load()
    state.destinations[name] = {"channels": {"default": channel}, "kinds": kinds, "enabled": True}
    state.destinations_updated = "2099-01-01T00:00:00+00:00"
    store.save(state)


def test_tracking_starts_quietly_with_the_current_image_kept(wired, tmp_path):
    thumbs = _state(tmp_path)["thumbs"]
    assert set(thumbs) == {"arch0000001", "vid00000001", "short000001"}
    assert all(e["v"] == e["seen"] == THUMB for e in thumbs.values())
    assert {e["tab"] for e in thumbs.values()} == {"streams", "videos", "shorts"}
    assert sorted(p.name for p in (tmp_path / "thumbs").iterdir()) == [
        f"{vid}_{THUMB:x}.jpg" for vid in ("arch0000001", "short000001", "vid00000001")]
    assert Discord.posted == []


def test_a_changed_thumbnail_is_announced_once_with_the_old_and_the_new_image(wired, tmp_path):
    _later(tmp_path, 31)
    version = _change("arch0000001")
    _run(wired)
    [(payload, files)] = _notices()
    assert payload["content"] == HEADLINE
    url = f"https://www.youtube.com/watch?v=arch0000001#thumbnail-{version:x}"
    first, second = payload["embeds"]
    assert first["url"] == second["url"] == url  # one embed with both images in Discord
    assert first["title"] == "title arch0000001" and first["footer"]["text"].endswith("・配信")
    assert first["image"]["url"] == "attachment://before.jpg"
    assert second["image"]["url"] == "attachment://after.jpg"
    assert files == [("before.jpg", f"jpeg arch0000001 {THUMB}".encode()),
                     ("after.jpg", f"jpeg arch0000001 {version}".encode())]
    entry = _state(tmp_path)["thumbs"]["arch0000001"]
    assert entry["v"] == entry["seen"] == version
    _later(tmp_path, 31)
    _run(wired)
    assert len(_notices()) == 1
    assert sorted(p.name for p in (tmp_path / "thumbs").iterdir() if p.name.startswith("arch")) == [
        f"arch0000001_{version:x}.jpg"]  # the new one is the "before" of the next change
    again = _change("arch0000001", seconds=60)
    _later(tmp_path, 31)
    _run(wired)  # a second change of the same thumbnail: its own link, so not taken as posted
    assert len(_notices()) == 2
    assert _notices()[1][1][0] == ("before.jpg", f"jpeg arch0000001 {version}".encode())
    assert _state(tmp_path)["thumbs"]["arch0000001"]["v"] == again


def test_shorts_and_videos_link_to_their_page_and_say_what_they_are(wired, tmp_path):
    _later(tmp_path, 31)
    _change("short000001")
    _change("vid00000001")
    _run(wired)
    by_link = {p["embeds"][0]["url"].split("#")[0]: p for p, _ in _notices()}
    assert by_link["https://www.youtube.com/shorts/short000001"]["embeds"][0]["footer"]["text"].endswith("・ショート")
    assert by_link["https://www.youtube.com/watch?v=vid00000001"]["embeds"][0]["footer"]["text"].endswith("・動画")


def test_thumbnail_notices_go_only_to_destinations_taking_them(wired, tmp_path):
    _add_dest(tmp_path, "friend", "500", list(dests.DEFAULT_KINDS))
    _add_dest(tmp_path, "fans", "700", ["thumbnail"])
    _later(tmp_path, 31)
    _change("arch0000001")
    _run(wired)
    assert len(_notices("1")) == 1 and len(_notices("700")) == 1 and _notices("500") == []


def test_checks_follow_how_likely_a_change_is(wired, tmp_path):
    FakeYouTube.videos = [_ref("slot0000001", "streams", "upcoming"), _ref("live0000001", "streams", "live"),
                          *FakeYouTube.videos]
    _run(wired)  # the new ones start being tracked
    _change("slot0000001")
    _change("live0000001")
    _change("arch0000001")
    _later(tmp_path, 5)
    _run(wired)  # next run: only the open slot is looked at again so soon
    assert [p["embeds"][0]["url"][32:43] for p, _ in _notices()] == ["slot0000001"]
    _later(tmp_path, 31)
    _run(wired)  # listed: every half hour; on air: not until it is over
    assert sorted(p["embeds"][0]["url"][32:43] for p, _ in _notices()) == ["arch0000001", "slot0000001"]
    FakeYouTube.videos[1] = _ref("live0000001", "streams")
    _run(wired)
    assert len(_notices()) == 3


def test_older_videos_from_the_full_listing_are_tracked_and_checked_every_two_hours(wired, tmp_path, monkeypatch):
    older = [_ref(f"old{i:08d}", "streams") for i in range(14)]
    page = list(FakeYouTube.videos)
    monkeypatch.setattr(FakeYouTube, "fetch_tab_all", lambda self, tab: [r for r in page + older if tab in r.sources])
    _later(tmp_path, 24 * 60)
    _run(wired)  # the daily full listing finds them: 10 start being tracked now, the rest next run
    tracked = {k for k, e in _state(tmp_path)["thumbs"].items() if e.get("v")}
    assert len(tracked & {r.video_id for r in older}) == thumbnails.BASELINES_PER_RUN
    _run(wired)
    assert all(_state(tmp_path)["thumbs"][r.video_id].get("v") == THUMB for r in older)
    _change("old00000003")
    _later(tmp_path, 60)
    _run(wired)
    assert _notices() == []
    _later(tmp_path, 61)
    _run(wired)
    assert [p["embeds"][0]["url"][32:43] for p, _ in _notices()] == ["old00000003"]


def test_a_failed_full_listing_is_tried_again_in_an_hour(wired, tmp_path, monkeypatch):
    calls = []

    def broken(self, tab):
        calls.append(tab)
        raise YouTubeError("page 2 failed")
    monkeypatch.setattr(FakeYouTube, "fetch_tab_all", broken)
    _later(tmp_path, 24 * 60)
    _run(wired)
    _run(wired)
    assert calls == ["streams"]
    _later(tmp_path, 60)
    _run(wired)
    assert calls == ["streams", "streams"]


def test_a_video_no_listing_shows_any_more_is_no_longer_checked_and_then_forgotten(wired, tmp_path):
    FakeYouTube.videos = FakeYouTube.videos[1:]  # the archive was deleted or hidden
    _later(tmp_path, 3 * 24 * 60)
    _change("arch0000001")
    _run(wired)
    assert _notices() == [] and "arch0000001" in _state(tmp_path)["thumbs"]
    _later(tmp_path, 28 * 24 * 60)
    _run(wired)
    assert "arch0000001" not in _state(tmp_path)["thumbs"]
    assert not any(p.name.startswith("arch") for p in (tmp_path / "thumbs").iterdir())


def test_a_frame_picked_by_youtube_is_version_0_and_a_thumbnail_set_later_is_announced(
        wired, tmp_path, monkeypatch):
    real = FakeYouTube.thumb_image

    def auto(self, video_id, version):  # i.ytimg.com: version 0 answers ?v=0 and the plain URL only
        if video_id == "auto0000001" and self.thumbs.get(video_id, 0) == 0:
            if version:
                raise YouTubeError("thumbnail download -> HTTP 404")
            return 0, b"jpeg auto0000001 0", "maxresdefault.jpg"
        return real(self, video_id, version)
    monkeypatch.setattr(FakeYouTube, "thumb_image", auto)
    FakeYouTube.thumbs["auto0000001"] = 0
    FakeYouTube.videos.append(_ref("auto0000001", "shorts"))
    _run(wired)
    entry = _state(tmp_path)["thumbs"]["auto0000001"]
    assert entry["v"] == entry["seen"] == 0 and entry["since"]
    _later(tmp_path, 31)
    _run(wired)
    assert _notices() == []
    version = _change("auto0000001")
    _later(tmp_path, 31)
    _run(wired)
    [(payload, files)] = _notices()
    assert files[0] == ("before.jpg", b"jpeg auto0000001 0")
    assert _state(tmp_path)["thumbs"]["auto0000001"]["v"] == version


def test_videos_whose_first_look_fails_never_hold_up_the_others(wired, tmp_path, monkeypatch):
    real = FakeYouTube.thumb_image
    broken = {f"aaa{i:08d}" for i in range(3)}

    def flaky(self, video_id, version):
        if video_id in broken:
            raise YouTubeError("thumbnail download -> HTTP 404")
        return real(self, video_id, version)
    monkeypatch.setattr(FakeYouTube, "thumb_image", flaky)
    FakeYouTube.videos += [_ref(vid, "videos") for vid in sorted(broken)] + [_ref("zzz00000001", "videos")]
    _run(wired)  # the three fail (and stop this run's first looks)
    _run(wired)
    thumbs = _state(tmp_path)["thumbs"]
    assert thumbs["zzz00000001"].get("since") and all(not thumbs[v].get("since") for v in broken)


def test_a_change_made_before_tracking_began_is_taken_over_quietly(wired, tmp_path):
    state = _state(tmp_path)
    since = datetime.fromisoformat(state["thumbs"]["arch0000001"]["since"])
    FakeYouTube.thumbs["arch0000001"] = int(since.timestamp()) - 3600  # a cached copy hid it
    _later(tmp_path, 31)
    _run(wired)
    assert _notices() == []
    assert _state(tmp_path)["thumbs"]["arch0000001"]["v"] == int(since.timestamp()) - 3600


def test_excluded_streams_and_identical_images_get_no_notice(wired, tmp_path, monkeypatch):
    FakeYouTube.videos[0] = _ref("arch0000001", "streams", title="【フリーチャット】待機所")
    _later(tmp_path, 31)
    version = _change("arch0000001")
    _run(wired)
    assert _notices() == [] and _state(tmp_path)["thumbs"]["arch0000001"]["v"] == version
    same = FakeYouTube.thumb_image
    monkeypatch.setattr(FakeYouTube, "thumb_image",
                        lambda self, vid, v: (lambda r: (r[0], f"jpeg {vid} {THUMB}".encode(), r[2]))(same(self, vid, v)))
    _change("vid00000001")
    _later(tmp_path, 31)
    _run(wired)
    assert _notices() == []


def test_a_burst_of_changes_is_taken_over_without_notices_and_reported(wired, tmp_path):
    FakeYouTube.videos = [_ref(f"many{i:07d}", "streams") for i in range(12)]
    _run(wired)
    _run(wired)
    _later(tmp_path, 31)
    for i in range(12):
        _change(f"many{i:07d}")
    _run(wired)
    assert _notices() == []
    assert any("サムネイル" in text and "通知しませんでした" in text for text in _admin())
    assert all(e["v"] == e["seen"] for e in _state(tmp_path)["thumbs"].values())


def test_a_notice_that_reached_nobody_is_sent_next_run(wired, tmp_path):
    _later(tmp_path, 31)
    _change("arch0000001")
    Discord.fail = {"1": 503}
    _run(wired)
    assert _notices() == []
    Discord.fail = {}
    _run(wired)
    [(payload, files)] = _notices()
    assert [name for name, _ in files] == ["before.jpg", "after.jpg"]


def test_a_destination_that_missed_it_gets_the_new_image_by_link(wired, tmp_path):
    _add_dest(tmp_path, "fans", "700", ["thumbnail"])
    _later(tmp_path, 31)
    version = _change("arch0000001")
    Discord.fail = {"700": 503}
    _run(wired)
    Discord.fail = {}
    _run(wired)
    [(payload, files)] = _notices("700")
    assert files == [] and len(payload["embeds"]) == 1
    assert payload["embeds"][0]["image"]["url"] == f"https://i.ytimg.com/vi/arch0000001/maxresdefault.jpg?v={version:x}"
    assert len(_notices("1")) == 1


def test_a_change_while_stopped_is_never_announced_later(wired, tmp_path, monkeypatch):
    monkeypatch.setattr(notifier.control, "pause_reason", lambda cfg, discord: "テスト")
    _later(tmp_path, 31)
    _change("arch0000001")
    _run(wired)
    monkeypatch.setattr(notifier.control, "pause_reason", lambda cfg, discord: None)
    _later(tmp_path, 31)
    _run(wired)
    assert _notices() == []


def test_without_a_local_copy_the_new_image_is_shown_alone(wired, tmp_path):
    for path in (tmp_path / "thumbs").iterdir():
        path.unlink()
    _later(tmp_path, 31)
    _change("arch0000001")
    _run(wired)
    [(payload, files)] = _notices()
    assert [name for name, _ in files] == ["after.jpg"] and len(payload["embeds"]) == 1
    assert "変更前の画像はありません" in payload["embeds"][0]["description"]


def test_checks_that_stop_working_are_reported_after_a_while(wired, tmp_path, monkeypatch):
    monkeypatch.setattr(FakeYouTube, "thumb_probe",
                        lambda self, vid, newer: None)  # every version "unknown", also the current one
    for _ in range(notifier.THUMB_ALERT_STREAK):
        _later(tmp_path, 31)
        _run(wired)
    assert len([t for t in _admin() if "サムネイル" in t]) == 1
    _later(tmp_path, 31)
    _run(wired)
    assert len([t for t in _admin() if "サムネイル" in t]) == 1  # once a day


def test_a_dry_run_keeps_the_kept_images(wired, tmp_path):
    _later(tmp_path, 31)
    _change("arch0000001")
    before = sorted(p.name for p in (tmp_path / "thumbs").iterdir())
    notifier.run(wired, role="primary", dry_run=True)
    assert set(before) <= {p.name for p in (tmp_path / "thumbs").iterdir()}


def test_versions_only_grow_on_merge_so_a_notice_is_never_sent_twice():
    sent = {"v": 20, "seen": 20, "since": "2026-11-01T00:00:00+00:00", "listed": "2026-11-02T00:00:00+00:00"}
    stale = {"v": 10, "seen": 20, "since": "2026-10-01T00:00:00+00:00", "listed": "2026-11-03T00:00:00+00:00",
             "title": "new title"}
    mine = State(thumbs={"a": dict(sent)})
    mine.merge_from(State(thumbs={"a": dict(stale)}))
    assert mine.thumbs["a"]["v"] == 20 and mine.thumbs["a"]["seen"] == 20
    assert mine.thumbs["a"]["since"] == "2026-10-01T00:00:00+00:00" and mine.thumbs["a"]["title"] == "new title"
    theirs = State(thumbs={"a": dict(stale)})
    theirs.merge_from(State(thumbs={"a": dict(sent)}))
    assert theirs.thumbs["a"]["v"] == 20


def test_the_thumbnail_checker_never_stops_the_other_notifications(wired, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(thumbnails.ThumbnailWatch, "run", boom)
    FakeYouTube.videos.append(_ref("newvid00001", "videos"))
    FakeYouTube.published = {"newvid00001": datetime.now(timezone.utc)}
    _run(wired)
    assert any(p["embeds"][0]["url"].endswith("newvid00001") for c, p, _ in Discord.posted if c == "1")


def test_files_are_uploaded_with_the_message():
    sent = {}

    class Session:
        headers: dict = {}

        def request(self, method, url, timeout, **kwargs):
            sent.update(kwargs)

            class Resp:
                status_code, ok, content = 200, True, b"{}"

                def json(self):
                    return {}
            return Resp()
    client = DiscordClient("t")
    client.session = Session()
    client.send("1", {"content": "x", "embeds": [{"image": {"url": "attachment://after.jpg"}}]},
                files=[("before.jpg", b"a"), ("after.jpg", b"b")])
    body = json.loads(sent["data"]["payload_json"])
    assert body["attachments"] == [{"id": 0, "filename": "before.jpg"}, {"id": 1, "filename": "after.jpg"}]
    assert sent["files"]["files[1]"] == ("after.jpg", b"b", "image/jpeg")


def test_a_channel_that_refuses_files_gets_the_notice_with_links_instead(wired, tmp_path):
    Discord.no_files = {"1"}
    _later(tmp_path, 31)
    version = _change("arch0000001")
    assert _run(wired) == 0
    [(payload, files)] = _notices()
    assert files == [] and payload["embeds"][0]["image"]["url"].endswith(f"maxresdefault.jpg?v={version:x}")
    assert _state(tmp_path)["thumbs"]["arch0000001"]["v"] == version


def test_a_change_that_could_not_be_sent_for_days_is_dropped_like_other_old_news(wired, tmp_path):
    _later(tmp_path, 31)
    version = _change("arch0000001")
    Discord.fail = {"1": 403}
    _run(wired)
    assert _run(wired) == 1  # still failing: retried
    Discord.fail = {}
    _later(tmp_path, 73 * 60)
    assert _run(wired) == 0
    assert _notices() == [] and _state(tmp_path)["thumbs"]["arch0000001"]["v"] == version


def test_checks_that_keep_failing_are_reported(wired, tmp_path, monkeypatch):
    def broken(self, video_id, newer_than):
        raise YouTubeError("thumbnail check -> HTTP 200 (ETag 'abc')")  # e.g. a new ETag format
    monkeypatch.setattr(FakeYouTube, "thumb_probe", broken)
    for _ in range(notifier.THUMB_ALERT_STREAK):
        _later(tmp_path, 31)
        _run(wired)
    assert [t for t in _admin() if "すべて失敗" in t]


def test_a_full_listing_that_keeps_failing_is_reported(wired, tmp_path, monkeypatch):
    def broken(self, tab):
        raise YouTubeError("/streams page 2 came back empty")
    monkeypatch.setattr(FakeYouTube, "fetch_tab_all", broken)
    _later(tmp_path, 31 * 60)
    for _ in range(notifier.THUMB_ALERT_STREAK):
        _later(tmp_path, 5)
        _run(wired)
    assert [t for t in _admin() if "全動画の一覧" in t]


@pytest.mark.parametrize("new_video", [True, False])  # down at the first look / at the checks
def test_an_unreachable_image_server_stops_this_runs_thumbnail_requests(wired, tmp_path, monkeypatch, new_video):
    calls = []

    def down(self, video_id, *args):
        calls.append(video_id)
        raise ThumbUnreachable("thumbnail check failed: ConnectTimeout")
    monkeypatch.setattr(FakeYouTube, "thumb_probe", down)
    monkeypatch.setattr(FakeYouTube, "thumb_image", down)
    if new_video:
        FakeYouTube.videos.append(_ref("newvid00001", "videos"))
    _later(tmp_path, 31)
    assert _run(wired) == 0
    assert len(calls) == 1


def test_thumbnail_work_stops_when_its_time_is_up(wired, tmp_path, monkeypatch):
    calls = []
    real = FakeYouTube.thumb_probe
    monkeypatch.setattr(FakeYouTube, "thumb_probe", lambda self, *a: calls.append(a) or real(self, *a))
    monkeypatch.setattr(thumbnails, "TIME_BUDGET", -1)
    _later(tmp_path, 31)
    _change("arch0000001")
    assert _run(wired) == 0
    assert calls == [] and _notices() == []
