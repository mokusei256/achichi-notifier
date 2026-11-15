import json

import pytest

from achichi_notifier import notifier
from achichi_notifier.config import Config
from achichi_notifier.youtube import YouTubeClient

from test_firefox_cookies import FUTURE_MS, auth_rows, make_profile
from test_notifier import FakeDiscordClient, FakeYouTube, _cfg, _state, fake_youtube  # noqa: F401
from achichi_notifier.state import FileStateStore


def _config(tmp_path, **overrides):
    values = dict(
        discord_token=None, channel_ids={"video": "1"}, state_backend="file", state_channel_id=None,
        state_file=tmp_path / "s.json", youtube_channel_id="UC", youtube_handle="@x",
        cookies_file=None, cookies_secret=None, mentions={}, fallback_after_minutes=15,
        max_age_hours=72, max_new_per_run=10, member_check_minutes=10,
    )
    values.update(overrides)
    return Config(**values)


def test_firefox_profile_wins_and_never_falls_back(tmp_path):
    prof, conn = make_profile(tmp_path, auth_rows())
    conn.close()
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    cfg = _config(tmp_path, firefox_profile=prof, cookies_file=cookie_file)
    source = cfg.load_cookies()
    assert source.origin == "firefox" and source.error is None and "LOGIN_INFO" in source.text
    assert "youtube.com cookie" in source.info and cfg.cookies_configured

    broken = _config(tmp_path, firefox_profile=tmp_path / "missing", cookies_file=cookie_file)
    source = broken.load_cookies()
    assert source.origin == "firefox" and source.text is None and source.transient is False
    client = YouTubeClient.from_config(broken)
    assert client.cookies_configured and not client.has_cookies and client.cookie_error


def test_profile_without_login_is_a_transient_problem(tmp_path):
    prof, conn = make_profile(tmp_path, [("", "PREF", "f6=1", ".youtube.com", "/", FUTURE_MS, 1, 0)])
    conn.close()
    client = YouTubeClient.from_config(_config(tmp_path, firefox_profile=prof))
    assert client.cookie_source == "firefox" and client.cookie_transient
    assert "not logged in" in client.cookie_error and not client.has_cookies


@pytest.fixture
def wired_ff(monkeypatch, tmp_path):
    FakeDiscordClient.instances = []
    monkeypatch.setattr(notifier, "DiscordClient", FakeDiscordClient)
    monkeypatch.setattr(notifier, "make_store", lambda cfg, discord: FileStateStore(tmp_path / "state.json"))
    prof = tmp_path / "app" / "firefox-profile"
    prof.mkdir(parents=True)
    return _cfg(tmp_path, discord_token="t", state_backend="discord", state_channel_id="999",
                firefox_profile=prof)


def _alerts():
    return [p["content"] for d in FakeDiscordClient.instances for c, p in d.sent if c == "999"]


def test_read_failures_alert_only_after_a_streak_with_login_steps(monkeypatch, wired_ff, tmp_path):
    monkeypatch.setattr(FakeYouTube, "cookie_error", "Firefox profile: cookies.sqlite kept changing")
    monkeypatch.setattr(FakeYouTube, "cookie_transient", True, raising=False)
    monkeypatch.setattr(FakeYouTube, "cookie_source", "firefox", raising=False)
    counts = []
    for _ in range(notifier.FETCH_FAIL_ALERT_STREAK):
        notifier.run(wired_ff, role="primary")
        counts.append(len(_alerts()))
    assert counts[:-1] == [0] * (notifier.FETCH_FAIL_ALERT_STREAK - 1) and counts[-1] == 1
    assert "achichi-notifier YouTube login" in _alerts()[0] and "cookies.txt" not in _alerts()[0]
    assert _state(tmp_path)["runners"]["primary"]["member_fail_streak"] == notifier.FETCH_FAIL_ALERT_STREAK


def test_firefox_format_problem_alerts_immediately(monkeypatch, wired_ff, tmp_path):
    monkeypatch.setattr(FakeYouTube, "cookie_error", "Firefox profile: not a plain SQLite file")
    monkeypatch.setattr(FakeYouTube, "cookie_transient", False, raising=False)
    monkeypatch.setattr(FakeYouTube, "cookie_source", "firefox", raising=False)
    notifier.run(wired_ff, role="primary")
    assert len(_alerts()) == 1 and "install.ps1 -FirefoxProfile" in _alerts()[0]


@pytest.mark.parametrize("failures, expected", [(2, 0), (3, 1)])
def test_browser_refresh_failures_alert_once_a_day(wired_ff, tmp_path, failures, expected):
    status_file = wired_ff.firefox_profile.parent / "browser-refresh.json"
    status_file.write_text(json.dumps({"consecutive_failures": failures, "last_result": "exited-early:1"}))
    notifier.run(wired_ff, role="primary")
    notifier.run(wired_ff, role="primary")
    refresh_alerts = [a for a in _alerts() if "自動更新" in a]
    assert len(refresh_alerts) == expected
    if expected:
        assert "exited-early:1" in refresh_alerts[0]


def test_fallback_never_reads_the_browser_refresh_status(wired_ff, tmp_path):
    status_file = wired_ff.firefox_profile.parent / "browser-refresh.json"
    status_file.write_text(json.dumps({"consecutive_failures": 9}))
    notifier.run(wired_ff, role="fallback")
    assert not [a for a in _alerts() if "自動更新" in a]


def test_cookie_fix_text_depends_on_the_source():
    assert "achichi-notifier YouTube login" in notifier._cookie_fix("primary", "logged_out", "firefox")
    assert "install.ps1 -FirefoxProfile" in notifier._cookie_fix("primary", "unreadable", "firefox")
    assert "install.sh" in notifier._cookie_fix("fallback", "logged_out", "file")
    assert notifier._cookie_fix("fallback", "logged_out", None) == notifier._cookie_fix("fallback", "logged_out")
