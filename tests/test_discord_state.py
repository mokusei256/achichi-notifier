import json

from achichi_notifier.state import STATE_FILENAME, DiscordStateStore, State


class FakeDiscord:
    def __init__(self):
        self.messages_store = [{"id": "50", "author": {"id": "human"}, "attachments": []}]
        self.files = {}
        self.edits = 0

    def me(self):
        return {"id": "bot"}

    def messages(self, channel_id, limit=100, before=None):
        return [] if before else list(reversed(self.messages_store))

    def get_message(self, channel_id, message_id):
        return next(m for m in self.messages_store if m["id"] == message_id)

    def _attach(self, message_id, data):
        self.files[f"cdn://{message_id}"] = data
        return [{"filename": STATE_FILENAME, "url": f"cdn://{message_id}"}]

    def send_file(self, channel_id, content, filename, data):
        message = {"id": "100", "author": {"id": "bot"}, "attachments": self._attach("100", data)}
        self.messages_store.append(message)
        return message

    def edit_file(self, channel_id, message_id, content, filename, data):
        self.edits += 1
        self.get_message(channel_id, message_id)["attachments"] = self._attach(message_id, data)

    def download(self, url):
        return self.files[url]


def test_discord_state_roundtrip_create_then_edit():
    fake = FakeDiscord()
    store = DiscordStateStore(fake, "state-channel")
    assert store.load().videos == {}
    store.save(State(videos={"v1": "2026-11-15T00:00:00+00:00"}))
    assert store.message_id == "100" and fake.edits == 0

    other_runner = DiscordStateStore(fake, "state-channel")  # e.g. the Mac
    loaded = other_runner.load()
    assert set(loaded.videos) == {"v1"}
    loaded.posts["p1"] = "2026-11-15T00:00:00+00:00"
    other_runner.save(loaded)
    assert fake.edits == 1

    # first runner saves stale in-memory state: merge keeps the other runner's post
    store.save(State(videos={"v2": "2026-11-15T01:00:00+00:00"}))
    final = DiscordStateStore(fake, "state-channel").load()
    assert set(final.videos) == {"v1", "v2"} and set(final.posts) == {"p1"}


def test_merge_takes_other_roles_from_the_fresh_copy_and_newest_extra():
    stale = State(runners={"primary": {"last_run": "2026-11-15T00:00:00+00:00"},
                           "fallback": {"member_fail_streak": 5}},
                  extra={"future": {"v": 1}})
    stale.owner_role = "primary"
    fresh = State(runners={"fallback": {"member_fail_streak": 0,
                                        "last_member_check": "2026-11-15T00:10:00+00:00"}},
                  extra={"future": {"v": 2}})
    stale.merge_from(fresh)
    assert stale.runners["fallback"]["member_fail_streak"] == 0
    assert stale.runners["primary"]["last_run"] == "2026-11-15T00:00:00+00:00"
    assert stale.extra == {"future": {"v": 2}}


def test_unknown_keys_survive_a_round_trip():
    state = State.from_dict({"videos": {}, "streams": {"abc": {"status": "live"}}})
    assert State.from_dict(json.loads(state.dumps()))  # still loads
    assert json.loads(state.dumps())["streams"] == {"abc": {"status": "live"}}


def test_recent_urls_only_counts_this_bots_real_notifications():
    from achichi_notifier.discord_api import DiscordClient

    class Client(DiscordClient):
        def __init__(self):
            pass

        def me(self):
            return {"id": "bot"}

        def messages(self, channel_id, limit=100, before=None):
            return [
                {"author": {"id": "fan"}, "content": "https://www.youtube.com/watch?v=FANLINK0001",
                 "embeds": [{"url": "https://www.youtube.com/watch?v=FANLINK0001"}]},
                {"author": {"id": "bot"}, "content": "【プレビュー】📅 配信枠",
                 "embeds": [{"url": "https://www.youtube.com/watch?v=PREVIEW0001"}]},
                {"author": {"id": "bot"}, "content": "📅 配信枠が作成されました", "flags": 4096,
                 "embeds": [{"url": "https://www.youtube.com/watch?v=PREVIEW0002"}]},
                {"author": {"id": "bot"}, "content": "📅 配信枠が作成されました\u200b",
                 "embeds": [{"url": "https://www.youtube.com/watch?v=PREVIEW0003"}]},
                {"author": {"id": "bot"}, "content": "📅 配信枠が作成されました",
                 "embeds": [{"url": "https://www.youtube.com/watch?v=REALNOTE001"}]},
            ]

    assert Client().recent_urls("1") == {"https://www.youtube.com/watch?v=REALNOTE001"}
    # The text comes along: a stream's start notice shares its link with the slot notice.
    assert Client().recent_notices("1") == [("https://www.youtube.com/watch?v=REALNOTE001",
                                             "📅 配信枠が作成されました")]
