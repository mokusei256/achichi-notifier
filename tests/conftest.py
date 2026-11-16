import pytest


@pytest.fixture(autouse=True)
def _not_on_github_actions(monkeypatch):
    """The code hides details on GitHub Actions; tests opt in explicitly when they need that."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


@pytest.fixture(autouse=True)
def _fresh_fake_discord_history():
    """FakeDiscordClient.history is class-level: a 🛑 or stop marker left by one test must not
    stop the bot in the next one."""
    import sys
    module = sys.modules.get("test_notifier")
    if module is not None:
        module.FakeDiscordClient.history = {}
    yield
