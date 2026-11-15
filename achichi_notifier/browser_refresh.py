"""Once-a-day headless visit so Firefox itself refreshes the Google session.

Windows: run by the 'achichi-notifier-browser' scheduled task as
    pythonw.exe -m achichi_notifier --env-file ... --log-file ... refresh-browser
Writes <app dir>/browser-refresh.json, which the 5-minute run reads for the #管理 alert.
Never logs cookie names or values (only booleans / counts).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
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


def find_firefox(explicit: str | None = None) -> Path | None:
    candidates = [explicit] if explicit else []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            candidates.append(os.path.join(base, "Mozilla Firefox", "firefox.exe"))
    for cand in candidates:
        if cand and "\\windowsapps\\" not in cand.lower() and Path(cand).is_file():
            return Path(cand)
    return None


def _taskkill(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        # /T: the launcher (our PID) waits for the browser under --headless, so the whole
        # tree (browser + content processes) hangs below it.
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                       creationflags=NO_WINDOW, timeout=60)
    else:
        proc.kill()


def refresh(profile: Path, firefox: Path | None, dwell: float = DEFAULT_DWELL,
            url: str = DEFAULT_URL, popen=subprocess.Popen, kill=_taskkill,
            in_use=fc.profile_in_use, sleep=time.sleep) -> str:
    profile = Path(profile)
    status = read_status(profile)
    status["last_attempt"] = _now()

    def finish(result: str, **extra) -> str:
        status.update(extra, last_result=result)
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
    if in_use(profile):
        return finish("skipped-in-use")  # a visible window already keeps the session fresh
    before = _digests(profile)
    status["running_since"] = _now()
    _write_status(profile, status)
    env = dict(os.environ, MOZ_DISABLE_UPDATE_PROCESSING="1", MOZ_CRASHREPORTER_NO_REPORT="1")
    try:
        proc = popen([str(firefox), "--headless", "--profile", str(profile), url], env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except OSError as exc:
        return finish(f"launch-failed:{exc.__class__.__name__}")
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
