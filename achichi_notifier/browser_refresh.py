"""Once-a-day headless visit so Firefox itself refreshes the Google session.

Windows: run by the 'achichi-notifier-browser' scheduled task as
    pythonw.exe -m achichi_notifier --env-file ... --log-file ... refresh-browser
macOS: run by the launchd agent 'com.achichi.notifier.browser' (deploy/mac/install.sh).
Writes <app dir>/browser-refresh.json, which the 5-minute run reads for the #管理 alert.
Never logs cookie names or values (only booleans / counts).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import firefox_cookies as fc

log = logging.getLogger(__name__)

STATUS_NAME = "browser-refresh.json"
DEFAULT_URL = "https://www.youtube.com/"
DEFAULT_DWELL = 150
GOOD = ("ok", "skipped-in-use")
NOT_COUNTED = ("not-set-up",)
ROTATING = ("__Secure-1PSIDTS", "__Secure-3PSIDTS")
IN_USE_DAYS_LIMIT = 3  # "in use" this many times in a row is treated as a stuck profile
SECRET_ENV_PREFIXES = ("DISCORD_", "YOUTUBE_COOKIES", "MENTION_")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def status_path(profile: Path) -> Path:
    return Path(profile).parent / STATUS_NAME


def read_status(profile: Path) -> dict:
    try:
        data = json.loads(status_path(profile).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_status(profile: Path, data: dict) -> None:
    path = status_path(profile)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    for i in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # a reader has it open for a moment (Windows)
            time.sleep(0.2 * (i + 1))
    log.warning("could not update %s", path.name)


def _digests(profile: Path) -> dict[str, str] | None:
    """name -> short hash of value, in memory only (to tell whether anything rotated)."""
    try:
        res = fc.read_youtube_cookies(profile)
    except fc.FirefoxCookieError:
        return None
    out = {}
    for line in res.netscape_text.splitlines():
        if line.startswith("# ") or not line:
            continue
        parts = line.split("\t")
        out[parts[5]] = hashlib.sha256(parts[6].encode()).hexdigest()[:16]
    return out


def find_firefox(explicit: str | None = None, platform: str = sys.platform) -> Path | None:
    candidates = [explicit] if explicit else []
    if platform == "darwin":
        for apps in ("/Applications", os.path.expanduser("~/Applications")):
            candidates.append(os.path.join(apps, "Firefox.app", "Contents", "MacOS", "firefox"))
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            candidates.append(os.path.join(base, "Mozilla Firefox", "firefox.exe"))
    for cand in candidates:
        if cand and "\\windowsapps\\" not in cand.lower() and Path(cand).is_file():
            return Path(cand)
    return None


def _browser_env() -> dict:
    """Environment for Firefox without the bot's secrets (loaded from .env into os.environ)."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(SECRET_ENV_PREFIXES)}
    env.update(MOZ_DISABLE_UPDATE_PROCESSING="1", MOZ_CRASHREPORTER_NO_REPORT="1")
    return env


HEADLESS_ARG = re.compile(r"(^|\s)--?headless(\s|$)")


def _kill_leftover_posix(profile: Path, ps=None, kill=os.kill) -> int:
    """macOS / Linux: this user's processes started with --headless and this exact profile path
    (ps prints the arguments space-joined, so the path must be followed by a space or the end:
    .../firefox-profile-old never matches)."""
    if ps is None:
        try:
            ps = subprocess.run(["ps", "-x", "-o", "pid=,args="], capture_output=True, text=True,
                                timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            return 0
    path = re.compile(r"(^|\s)" + re.escape(str(Path(profile))) + r"(\s|$)")
    killed = 0
    for line in ps.splitlines():
        pid, _, args = line.strip().partition(" ")
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        if HEADLESS_ARG.search(args) and path.search(args):
            try:
                kill(int(pid), signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError):
                pass
    return killed


def kill_leftover_headless(profile: Path) -> int:
    """Stop headless Firefox processes left over on the bot profile (never touches windows the
    user opened: only processes started with --headless for this profile)."""
    if sys.platform != "win32":
        return _kill_leftover_posix(profile)
    # The path travels in an environment variable and is regex-escaped, so quotes, brackets or
    # wildcard characters in it can neither break the script nor widen the match; the path must
    # be a whole argument (so ...\firefox-profile-old or another profile never matches).
    script = ("$p = $env:ACHICHI_FF_PROFILE; $n = 0; "
              "if ($p) { $rx = '(?i)(^|[\\s\"])' + [regex]::Escape($p) + '($|[\\s\"])'; "
              "Get-CimInstance Win32_Process -Filter \"Name='firefox.exe'\" | "
              "Where-Object { $_.CommandLine -and "
              "$_.CommandLine -match '(?i)(^|\\s)--?headless(\\s|$)' -and $_.CommandLine -match $rx } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $n++ } }; $n")
    env = _browser_env()
    env["ACHICHI_FF_PROFILE"] = str(Path(profile))
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, creationflags=NO_WINDOW, timeout=60,
                             env=env)
        return int((out.stdout or "0").strip().splitlines()[-1] or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired, IndexError):
        return 0


def _taskkill(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        # /T: the launcher (our PID) waits for the browser under --headless, so the whole
        # tree (browser + content processes) hangs below it.
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                       creationflags=NO_WINDOW, timeout=60)
        return
    # POSIX: Firefox was started in its own session, so its content processes share its
    # process group; never signal a group this program itself belongs to.
    try:
        group = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        return
    if group == proc.pid and group != os.getpgrp():
        try:
            os.killpg(group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    else:
        proc.kill()


def _launch_options() -> dict:
    if sys.platform == "win32":
        return {"creationflags": NO_WINDOW}
    return {"start_new_session": True}  # own process group: one kill stops every helper


def _today() -> str:
    return datetime.now().astimezone().date().isoformat()


def refresh(profile: Path, firefox: Path | None, dwell: float = DEFAULT_DWELL,
            url: str = DEFAULT_URL, popen=subprocess.Popen, kill=_taskkill,
            in_use=fc.profile_in_use, sleep=time.sleep, cleanup=kill_leftover_headless,
            today=_today) -> str:
    profile = Path(profile)
    status = read_status(profile)
    started = _now()  # written together with the result, so last_attempt always pairs with it

    def finish(result: str, **extra) -> str:
        if result == "skipped-in-use":
            day = today()
            if status.get("in_use_last_day") != day:  # count days, not attempts (manual runs)
                status["in_use_streak"] = int(status.get("in_use_streak") or 0) + 1
                status["in_use_last_day"] = day
            if int(status.get("in_use_streak") or 0) >= IN_USE_DAYS_LIMIT:
                result = "stuck-in-use"  # in use at refresh time for days: likely left open
        else:
            status["in_use_streak"] = 0
            status.pop("in_use_last_day", None)
        status.update(extra, last_result=result, last_attempt=started)
        status.pop("running_since", None)
        if result in GOOD:
            status["consecutive_failures"] = 0
            status["last_ok"] = status["last_attempt"]
        elif result not in NOT_COUNTED:
            status["consecutive_failures"] = int(status.get("consecutive_failures") or 0) + 1
        _write_status(profile, status)
        level = logging.INFO if result in GOOD + NOT_COUNTED else logging.WARNING
        log.log(level, "browser refresh: %s", result)
        return result

    if not (profile / fc.MARKER).is_file() or not (profile / fc.DB_NAME).is_file():
        return finish("not-set-up")
    if firefox is None or not Path(firefox).is_file():
        return finish("firefox-missing")
    if in_use(profile) and cleanup(profile):
        sleep(3)  # a headless Firefox from an earlier run was still holding the profile
    if in_use(profile):
        return finish("skipped-in-use")  # a visible window already keeps the session fresh
    before = _digests(profile)
    status["running_since"] = _now()
    _write_status(profile, status)
    env = _browser_env()
    try:
        proc = popen([str(firefox), "--headless", "--profile", str(profile), url], env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **_launch_options())
    except OSError as exc:
        return finish(f"launch-failed:{exc.__class__.__name__}")
    try:
        try:
            code = proc.wait(timeout=dwell)
            result = f"exited-early:{code}"
        except subprocess.TimeoutExpired:
            result = "ok"
            kill(proc)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                result = "kill-timeout"
    finally:
        if proc.poll() is None:  # interrupted or kill() raised: do not leave it running
            try:
                kill(proc)
            except Exception:  # noqa: BLE001 - best effort; the next run's cleanup retries
                log.warning("could not stop the headless Firefox")
    for _ in range(10):
        if not in_use(profile):
            break
        sleep(1)
    else:
        result = "lock-still-held"
    after = _digests(profile)
    changed = None if before is None or after is None else before != after
    rotated = (None if before is None or after is None
               else any(before.get(n) != after.get(n) for n in ROTATING if n in after))
    log.info("browser refresh dwell=%ss: youtube.com cookies changed=%s, PSIDTS rotated=%s",
             dwell, changed, rotated)
    return finish(result, cookies_changed=changed, psidts_rotated=rotated)
