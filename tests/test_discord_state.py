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
