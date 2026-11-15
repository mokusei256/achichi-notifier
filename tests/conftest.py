import pytest


@pytest.fixture(autouse=True)
def _not_on_github_actions(monkeypatch):
    """The code hides details on GitHub Actions; tests opt in explicitly when they need that."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
