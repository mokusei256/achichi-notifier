"""Cross-platform tests for the refresh flow with a fake 'firefox' (no real browser, no cookies)."""

import logging
import sqlite3
import sys
from pathlib import Path

from achichi_notifier import browser_refresh as br
from achichi_notifier import firefox_cookies as fc
from test_firefox_cookies import FUTURE_MS, SCHEMA17, SECRET

FAKE_FF = r'''
import sqlite3, sys, time
args = sys.argv[1:]
prof = args[args.index("--profile") + 1]
mode = __MODE__
if mode == "crash":
    sys.exit(3)
c = sqlite3.connect(prof + "/cookies.sqlite", isolation_level=None)
c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA wal_autocheckpoint=0")
if mode == "rotate":
    c.execute("UPDATE moz_cookies SET value='ROTATED' WHERE name='__Secure-1PSIDTS'")
time.sleep(600)  # stays up like a browser until killed
'''


def _profile(tmp_path):
    prof = tmp_path / "app" / "firefox-profile"
    prof.mkdir(parents=True)
    (prof / fc.MARKER).write_text("bot profile")
    with sqlite3.connect(prof / "cookies.sqlite") as c:
        c.execute(SCHEMA17)
        c.execute("PRAGMA user_version=17")
        for name in ("LOGIN_INFO", "SAPISID", "__Secure-1PSIDTS"):
            c.execute("INSERT INTO moz_cookies(originAttributes,name,value,host,path,expiry,isSecure,"
                      "isHttpOnly) VALUES('',?,?,?,'/',?,1,0)", (name, SECRET, ".youtube.com", FUTURE_MS))
    return prof


def _fake_firefox(tmp_path, mode):
    script = tmp_path / "fakeff.py"
    script.write_text(FAKE_FF.replace("__MODE__", repr(mode)))
    return script


def _popen_python(script):
    import subprocess

    def popen(argv, **kw):
        kw.pop("creationflags", None)
        return subprocess.Popen([sys.executable, "-I", str(script), *argv[1:]], **kw)
    return popen


def test_ok_run_kills_browser_and_reports_rotation_without_values(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    prof = _profile(tmp_path)
    result = br.refresh(prof, Path(sys.executable), dwell=2,
                        popen=_popen_python(_fake_firefox(tmp_path, "rotate")),
                        in_use=lambda p: False)
    assert result == "ok"
    st = br.read_status(prof)
    assert st["consecutive_failures"] == 0 and st["psidts_rotated"] is True
    assert "running_since" not in st and SECRET not in caplog.text and "ROTATED" not in caplog.text


def test_early_exit_counts_as_failure(tmp_path):
    prof = _profile(tmp_path)
    for n in (1, 2, 3):
        assert br.refresh(prof, Path(sys.executable), dwell=5,
                          popen=_popen_python(_fake_firefox(tmp_path, "crash")),
                          in_use=lambda p: False) == "exited-early:3"
        assert br.read_status(prof)["consecutive_failures"] == n


def test_in_use_is_skipped_and_resets_failures(tmp_path):
    prof = _profile(tmp_path)
    br._write_status(prof, {"consecutive_failures": 2})

    def boom(*a, **k):
        raise AssertionError("must not launch")
    assert br.refresh(prof, Path(sys.executable), popen=boom, in_use=lambda p: True) == "skipped-in-use"
    assert br.read_status(prof)["consecutive_failures"] == 0


def test_missing_firefox_and_not_set_up(tmp_path):
    prof = _profile(tmp_path)
    assert br.refresh(prof, tmp_path / "nope.exe") == "firefox-missing"
    assert br.read_status(prof)["consecutive_failures"] == 1
    (prof / "cookies.sqlite").unlink()
    assert br.refresh(prof, Path(sys.executable)) == "not-set-up"
    assert br.read_status(prof)["consecutive_failures"] == 1  # not counted


def test_firefox_never_gets_the_bots_secrets(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("YOUTUBE_COOKIES_B64", "secret-cookies")
    monkeypatch.setenv("MENTION_LIVE", "<@&1>")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = br._browser_env()
    assert "DISCORD_BOT_TOKEN" not in env and "YOUTUBE_COOKIES_B64" not in env and "MENTION_LIVE" not in env
    assert env["PATH"] == "/usr/bin" and env["MOZ_DISABLE_UPDATE_PROCESSING"] == "1"


def test_profile_in_use_for_days_counts_as_a_failure(tmp_path):
    prof = tmp_path / "profile"
    prof.mkdir()
    (prof / fc.MARKER).write_text("bot profile")
    (prof / fc.DB_NAME).write_bytes(b"")
    results = [br.refresh(prof, Path(sys.executable), in_use=lambda p: True, cleanup=lambda p: 0,
                          sleep=lambda s: None) for _ in range(br.IN_USE_DAYS_LIMIT)]
    assert results[:-1] == ["skipped-in-use"] * (br.IN_USE_DAYS_LIMIT - 1)
    assert results[-1] == "stuck-in-use"
    assert br.read_status(prof)["consecutive_failures"] == 1
