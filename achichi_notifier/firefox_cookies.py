"""Read youtube.com cookies from the bot's dedicated Firefox profile.

Never opens Firefox's own files through SQLite. Reads cookies.sqlite, THEN cookies.sqlite-wal
(never -shm) as plain bytes, checks the WAL generation (checkpoint seq + salts) did not change
while copying, writes both into a private temp dir and opens that copy.
Errors never contain cookie names or values.
"""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

MARKER = "achichi-notifier-profile.txt"
DB_NAME = "cookies.sqlite"
WAL_NAME = DB_NAME + "-wal"
SQLITE_MAGIC = b"SQLite format 3\x00"
KNOWN_MAX_SCHEMA = 17  # Firefox 146-160 (Bug 1994761); 16 = FF142-145 (Bug 1972757, ms expiry)
MS_THRESHOLD = 10**11  # > 1e11 can only be milliseconds (1e11 s = year 5138)
REQUIRED_COLUMNS = {"host", "name", "value", "path", "expiry", "isSecure", "isHttpOnly",
                    "originAttributes"}
STALE_TMP_SECONDS = 600


class FirefoxCookieError(Exception):
    """Message is safe to log / post to the private state channel (no cookie names/values).
    transient=True: may fix itself (or by logging in); False: setup/format problem."""

    def __init__(self, message: str, transient: bool):
        super().__init__(message)
        self.transient = transient


@dataclass
class FirefoxCookies:
    netscape_text: str
    schema: int
    kept: int = 0
    other_origin_attrs: int = 0  # containers / partitioned / FPI rows ignored
    expired: int = 0
    invalid: int = 0  # names/values that are not printable ASCII (never put into the text)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"schema {self.schema}, {self.kept} youtube.com cookie(s), "
                f"{self.other_origin_attrs} container/partitioned skipped, {self.expired} expired, "
                f"{self.invalid} invalid")


def _printable(text: str) -> bool:
    return bool(text) and all(0x20 < ord(ch) < 0x7F for ch in text)


def _read_bytes(path: Path, tries: int = 5) -> bytes | None:
    for i in range(tries):
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except PermissionError:
            # Windows sharing violation (antivirus, backup tools): retry briefly.
            if i == tries - 1:
                raise FirefoxCookieError("cookies.sqlite is locked by another program", True) from None
            time.sleep(0.1 * (i + 1))
    return None


def _wal_generation(path: Path) -> bytes | None:
    """Checkpoint sequence + salt-1 + salt-2 of the WAL header (changes on WAL restart)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
    except FileNotFoundError:
        return None
    except PermissionError:
        raise FirefoxCookieError("cookies.sqlite-wal is locked by another program", True) from None
    return head[12:24] if len(head) == 32 else None


def profile_in_use(profile_dir: Path) -> bool | None:
    """Windows: Firefox keeps parent.lock open with share mode 0 (and never deletes it)."""
    if sys.platform != "win32":
        return None
    try:
        with open(profile_dir / "parent.lock", "rb"):
            return False
    except FileNotFoundError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


def _snapshot(profile_dir: Path, workdir: Path, attempts: int = 4) -> Path:
    db, wal = profile_dir / DB_NAME, profile_dir / WAL_NAME
    for attempt in range(attempts):
        gen_before = _wal_generation(wal)
        db_bytes = _read_bytes(db)          # 1) main database first
        wal_bytes = _read_bytes(wal)        # 2) then the WAL
        gen_after = _wal_generation(wal)
        if db_bytes is None:
            raise FirefoxCookieError("cookies.sqlite disappeared while reading", True)
        if wal_bytes is not None and len(wal_bytes) < 32:
            wal_bytes = None  # no complete header: nothing committed in it
        copied_gen = wal_bytes[12:24] if wal_bytes else None
        if wal_bytes is not None and not (gen_before == gen_after == copied_gen):
            time.sleep(0.25 * (attempt + 1))  # WAL restarted mid-copy: take a new snapshot
            continue
        if not db_bytes.startswith(SQLITE_MAGIC):
            raise FirefoxCookieError(
                "cookies.sqlite is not a plain SQLite file (encrypted by a newer Firefox, or damaged)",
                False)
        target = workdir / DB_NAME
        target.write_bytes(db_bytes)
        if wal_bytes:
            (workdir / WAL_NAME).write_bytes(wal_bytes)
        return target
    raise FirefoxCookieError("cookies.sqlite kept changing while copying", True)


def _clean_stale(tmp_parent: Path, now: float) -> None:
    for old in tmp_parent.glob("ffck-*"):
        try:
            if now - old.stat().st_mtime > STALE_TMP_SECONDS:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass


def read_youtube_cookies(profile_dir: Path, now: float | None = None,
                         tmp_parent: Path | None = None) -> FirefoxCookies:
    now = time.time() if now is None else now
    profile_dir = Path(profile_dir)
    if not profile_dir.is_dir():
        raise FirefoxCookieError("the Firefox profile folder does not exist", False)
    if not (profile_dir / MARKER).is_file():
        raise FirefoxCookieError("not the bot's Firefox profile (marker file missing)", False)
    if not (profile_dir / DB_NAME).is_file():
        raise FirefoxCookieError("no cookies.sqlite yet (nobody has logged in to this profile)", True)
    tmp_parent = Path(tmp_parent) if tmp_parent else profile_dir.parent / "tmp"
    tmp_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _clean_stale(tmp_parent, time.time())
    with tempfile.TemporaryDirectory(prefix="ffck-", dir=tmp_parent,
                                     ignore_cleanup_errors=True) as tmp:
        copy = _snapshot(profile_dir, Path(tmp))
        try:
            # contextlib.closing: sqlite3's own context manager does NOT close; an open
            # handle would keep the temp files locked on Windows.
            with contextlib.closing(sqlite3.connect(copy)) as conn:
                if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise FirefoxCookieError("cookies.sqlite copy failed an integrity check", True)
                schema = conn.execute("PRAGMA user_version").fetchone()[0]
                cols = {row[1] for row in conn.execute("PRAGMA table_info(moz_cookies)")}
                missing = REQUIRED_COLUMNS - cols
                if missing:
                    raise FirefoxCookieError(
                        f"moz_cookies lacks columns {sorted(missing)} (schema {schema})", False)
                rows = conn.execute(
                    "SELECT host, name, value, path, expiry, isSecure, isHttpOnly, "
                    "originAttributes FROM moz_cookies "
                    "WHERE host = 'youtube.com' OR host LIKE '%.youtube.com'").fetchall()
        except sqlite3.Error as exc:
            raise FirefoxCookieError(
                f"cookies.sqlite copy unreadable ({exc.__class__.__name__})", True) from None

    result = FirefoxCookies(netscape_text="", schema=schema)
    if schema > KNOWN_MAX_SCHEMA:
        result.warnings.append(f"newer Firefox cookie schema {schema} (known up to {KNOWN_MAX_SCHEMA})")
    lines = ["# Netscape HTTP Cookie File (generated from the bot's Firefox profile)"]
    for host, name, value, path, expiry, secure, http_only, oa in rows:
        if oa:  # '' = normal window, no container, unpartitioned
            result.other_origin_attrs += 1
            continue
        if not isinstance(expiry, int) or expiry <= 0:
            result.invalid += 1  # Firefox never persists session cookies
            continue
        expiry_s = expiry // 1000 if expiry > MS_THRESHOLD else expiry
        if expiry_s <= now:
            result.expired += 1
            continue
        host, path, value = str(host), str(path or "/"), "" if value is None else str(value)
        if not (_printable(str(name)) and _printable(value) and _printable(host)
                and _printable(path)):
            result.invalid += 1  # also blocks tab/newline injection into the text format
            continue
        flag = "TRUE" if host.startswith(".") else "FALSE"
        lines.append("\t".join(("#HttpOnly_" + host if http_only else host, flag, path,
                                "TRUE" if secure else "FALSE", str(expiry_s), str(name), value)))
        result.kept += 1
    result.netscape_text = "\n".join(lines) + "\n"
    return result
