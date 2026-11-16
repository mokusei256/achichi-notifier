"""Cross-platform tests for the refresh flow with a fake 'firefox' (no real browser, no cookies)."""

import logging
import sqlite3
import sys
import time
from pathlib import Path

import pytest

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
    same_day = [br.refresh(prof, Path(sys.executable), in_use=lambda p: True, cleanup=lambda p: 0,
                           sleep=lambda s: None, today=lambda: "2026-11-16") for _ in range(5)]
    assert same_day == ["skipped-in-use"] * 5  # manual re-runs on one day count once
    br._write_status(prof, {})
    results = [br.refresh(prof, Path(sys.executable), in_use=lambda p: True, cleanup=lambda p: 0,
                          sleep=lambda s: None, today=lambda d=d: f"2026-11-{d:02d}")
               for d in range(1, br.IN_USE_DAYS_LIMIT + 1)]
    assert results[:-1] == ["skipped-in-use"] * (br.IN_USE_DAYS_LIMIT - 1)
    assert results[-1] == "stuck-in-use"
    assert br.read_status(prof)["consecutive_failures"] == 1
    assert br.read_status(prof)["last_result"] == "stuck-in-use"


def test_stuck_profile_gets_its_own_alert(tmp_path):
    from achichi_notifier import notifier
    from achichi_notifier.state import State
    from test_notifier import FakeDiscordClient, _cfg

    prof = tmp_path / "app" / "firefox-profile"
    prof.mkdir(parents=True)
    br._write_status(prof, {"last_result": "stuck-in-use", "in_use_streak": 3, "consecutive_failures": 1})
    cfg = _cfg(tmp_path, discord_token="t", state_backend="discord", state_channel_id="999",
               firefox_profile=prof)
    discord, state = FakeDiscordClient("t"), State()
    notifier._browser_refresh_alert(cfg, discord, state)
    (channel, payload), = discord.sent
    assert channel == "999" and "3 日続けて" in payload["content"] and "閉じて" in payload["content"]
    notifier._browser_refresh_alert(cfg, discord, state)
    assert len(discord.sent) == 1  # throttled


def test_leftover_headless_is_cleaned_up_before_launch(tmp_path):
    prof = _profile(tmp_path)
    calls = {"in_use": 0, "cleanup": 0}

    def in_use(p):
        calls["in_use"] += 1
        return calls["cleanup"] == 0  # busy until the leftover is stopped

    def cleanup(p):
        calls["cleanup"] += 1
        return 1
    result = br.refresh(prof, Path(sys.executable), dwell=1,
                        popen=_popen_python(_fake_firefox(tmp_path, "idle")),
                        in_use=in_use, cleanup=cleanup, sleep=lambda s: None)
    assert calls["cleanup"] == 1 and result == "ok"


def test_browser_is_stopped_even_if_waiting_is_interrupted(tmp_path):
    prof = _profile(tmp_path)
    killed = []

    class Proc:
        pid = 1

        def wait(self, timeout=None):
            raise KeyboardInterrupt

        def poll(self):
            return None if not killed else 0

    import pytest
    with pytest.raises(KeyboardInterrupt):
        br.refresh(prof, Path(sys.executable), popen=lambda *a, **k: Proc(),
                   kill=lambda proc: killed.append(proc), in_use=lambda p: False)
    assert len(killed) == 1


def test_leftover_cleanup_passes_the_path_outside_the_script(monkeypatch, tmp_path):
    seen = {}

    def fake_run(argv, **kw):
        seen["script"], seen["env"] = argv[-1], kw["env"]

        class Out:
            stdout = "2\n"
        return Out()
    monkeypatch.setattr(br.sys, "platform", "win32")
    monkeypatch.setattr(br.subprocess, "run", fake_run)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret-token")
    odd = tmp_path / "it's [weird] *path*"
    assert br.kill_leftover_headless(odd) == 2
    assert str(odd) not in seen["script"] and "-like" not in seen["script"]
    assert seen["env"]["ACHICHI_FF_PROFILE"] == str(odd)
    assert "DISCORD_BOT_TOKEN" not in seen["env"]


def test_refresh_alert_waits_for_a_new_attempt(tmp_path):
    from achichi_notifier import notifier
    from achichi_notifier.state import State
    from test_notifier import FakeDiscordClient, _cfg

    prof = tmp_path / "app" / "firefox-profile"
    prof.mkdir(parents=True)
    br._write_status(prof, {"last_result": "stuck-in-use", "in_use_streak": 3, "consecutive_failures": 1,
                            "last_attempt": "2026-11-10T20:10:00+00:00"})
    cfg = _cfg(tmp_path, discord_token="t", state_backend="discord", state_channel_id="999",
               firefox_profile=prof)
    discord = FakeDiscordClient("t")
    state = State(alerts={"browser_refresh": "2026-11-10T20:15:00+00:00"})  # > 24h ago, same attempt
    notifier._browser_refresh_alert(cfg, discord, state)
    assert discord.sent == []  # the status is the one already reported



def _leftover_regexes(path):
    """The two patterns the PowerShell cleanup uses, rebuilt from the generated script."""
    import re
    from unittest import mock
    seen = {}

    def fake_run(argv, **kw):
        seen["script"] = argv[-1]

        class Out:
            stdout = "0"
        return Out()
    with mock.patch.object(br.sys, "platform", "win32"), mock.patch.object(br.subprocess, "run", fake_run):
        br.kill_leftover_headless(path)
    script = seen["script"]
    head, tail = re.search(r"\$rx = '([^']*)' \+ \[regex\]::Escape\(\$p\) \+ '([^']*)'", script).groups()
    headless = re.search(r"-match '([^']*headless[^']*)'", script).group(1)
    return re.compile(head + re.escape(str(path)) + tail), re.compile(headless)


def test_leftover_cleanup_matches_only_this_profile_and_headless():
    prof = r"C:\Users\u\AppData\Local\achichi-notifier\firefox-profile"
    path_rx, headless_rx = _leftover_regexes(prof)
    ff = r'"C:\Program Files\Mozilla Firefox\firefox.exe"'
    ours = f"{ff} --headless --profile {prof} https://www.youtube.com/"
    quoted = f'{ff} --headless --profile "{prof}" https://www.youtube.com/'
    sibling = f"{ff} --headless --profile {prof}-old https://www.youtube.com/"
    window = f"{ff} --profile {prof} https://www.youtube.com/"
    assert path_rx.search(ours) and headless_rx.search(ours)
    assert path_rx.search(quoted) and headless_rx.search(quoted)
    assert not path_rx.search(sibling)
    assert not headless_rx.search(window)  # a window the user opened is never touched


def test_last_attempt_is_written_with_the_result(tmp_path):
    prof = _profile(tmp_path)
    br._write_status(prof, {"last_attempt": "2026-01-01T00:00:00+00:00", "last_result": "exited-early:3",
                            "consecutive_failures": 3})
    seen = {}

    def popen(*a, **k):
        seen["status_while_running"] = br.read_status(prof)
        raise OSError("no browser")
    br.refresh(prof, Path(sys.executable), popen=popen, in_use=lambda p: False)
    assert seen["status_while_running"]["last_attempt"] == "2026-01-01T00:00:00+00:00"
    assert seen["status_while_running"]["running_since"]
    final = br.read_status(prof)
    assert final["last_attempt"] != "2026-01-01T00:00:00+00:00" and final["last_result"].startswith("launch-failed")


def test_find_firefox_on_macos(tmp_path, monkeypatch):
    app = tmp_path / "Applications" / "Firefox.app" / "Contents" / "MacOS"
    app.mkdir(parents=True)
    (app / "firefox").write_text("")
    monkeypatch.setattr(br.os.path, "expanduser", lambda p: str(tmp_path / "Applications") if p == "~/Applications" else p)
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)
    found = br.find_firefox(None, platform="darwin")
    assert found in (Path("/Applications/Firefox.app/Contents/MacOS/firefox"), app / "firefox")
    if not Path("/Applications/Firefox.app/Contents/MacOS/firefox").is_file():
        assert found == app / "firefox"
    assert br.find_firefox(None, platform="linux") is None or sys.platform == "win32"


def test_posix_leftover_cleanup_only_hits_this_profiles_headless_firefox(tmp_path):
    prof = tmp_path / "Application Support" / "achichi-notifier" / "firefox-profile"
    ff = "/Applications/Firefox.app/Contents/MacOS/firefox"
    ps = "\n".join([
        f"  101 {ff} --headless --profile {prof} https://www.youtube.com/",          # ours: kill
        f"  102 {ff} --profile {prof} https://www.youtube.com/",                     # visible: keep
        f"  103 {ff} --headless --profile {prof}-old https://www.youtube.com/",      # sibling: keep
        f"  104 {ff} --headless --profile /other/profile https://www.youtube.com/",  # other: keep
        f"  105 {ff} -headless -profile {prof}",                                     # ours: kill
        "  abc garbage line",
    ])
    killed = []
    n = br._kill_leftover_posix(prof, ps=ps, kill=lambda pid, sig: killed.append(pid))
    assert n == 2 and killed == [101, 105]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_posix_kill_stops_the_whole_browser_group(tmp_path):
    import subprocess
    # parent + a child in the same new session, like Firefox and its content processes
    script = ("import subprocess, sys, time; "
              "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
              "print(c.pid, flush=True); time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-I", "-c", script], stdout=subprocess.PIPE, text=True,
                            **br._launch_options())
    child = int(proc.stdout.readline())
    br._taskkill(proc)
    proc.wait(timeout=10)
    import os
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(child, 9)
        raise AssertionError("the child survived")
    assert os.getpgrp() != proc.pid  # and our own group was never signalled (we are alive)


@pytest.mark.skipif(sys.platform == "win32", reason="launchd/POSIX signals")
def test_sigterm_to_the_refresh_job_still_stops_the_browser(tmp_path):
    """launchd stops the job with SIGTERM (install.sh re-run, uninstall.sh): the headless browser
    runs in its own process group, so the job itself has to take it down."""
    import os
    import signal
    import subprocess
    prof = _profile(tmp_path)
    pidfile = tmp_path / "ff.pid"
    fake = tmp_path / "firefox"
    fake.write_text(f"#!{sys.executable}\nimport os, time\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
                    "time.sleep(120)\n")
    fake.chmod(0o755)
    # refresh-browser is a no-op on Linux (only Windows and macOS have the daily task), but the
    # code past that gate is the same POSIX path on both, so on Linux CI the CLI alone is told it
    # runs on macOS. (Setting sys.platform itself breaks imports such as urllib's _scproxy.)
    runner = (
        "import sys, logging; from pathlib import Path\n"
        "from achichi_notifier import __main__ as cli\n"
        "class MacSys:\n"
        "    platform = 'darwin'\n"
        "    def __getattr__(self, name): return getattr(sys, name)\n"
        "cli.sys = MacSys()\n"
        "class C: firefox_profile = Path(sys.argv[1]); firefox_exe = Path(sys.argv[2]); refresh_seconds = 100\n"
        "sys.exit(cli._refresh_browser(C, None, 'about:blank'))\n")
    job = subprocess.Popen([sys.executable, "-c", runner, str(prof), str(fake)])
    try:
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text():
                break
            time.sleep(0.1)
        assert pidfile.exists(), f"the refresh job never started the browser (exit code {job.poll()})"
        browser = int(pidfile.read_text())
        job.send_signal(signal.SIGTERM)
        assert job.wait(timeout=20) == 128 + signal.SIGTERM
        for _ in range(50):
            try:
                os.kill(browser, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(browser, signal.SIGKILL)
            raise AssertionError("the headless browser outlived the job")
    finally:
        if job.poll() is None:
            job.kill()
