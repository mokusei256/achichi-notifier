"""Synthetic tests (no real cookies) for the Firefox cookie reader prototype."""

import logging
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from achichi_notifier import firefox_cookies as fc
from achichi_notifier.youtube import has_auth_cookies, parse_cookie_text

SCHEMA17 = """CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY,
 originAttributes TEXT NOT NULL DEFAULT '', name TEXT, value TEXT, host TEXT, path TEXT,
 expiry INTEGER, lastAccessed INTEGER, creationTime INTEGER, isSecure INTEGER,
 isHttpOnly INTEGER, inBrowserElement INTEGER DEFAULT 0, sameSite INTEGER DEFAULT 0,
 schemeMap INTEGER DEFAULT 0, isPartitionedAttributeSet INTEGER DEFAULT 0,
 updateTime INTEGER, CONSTRAINT moz_uniqueid UNIQUE (name, host, path, originAttributes))"""
NOW = 1_800_000_000
FUTURE_MS = (NOW + 86400 * 300) * 1000
SECRET = "SENTINELsecretVALUE"


def make_profile(tmp_path, rows, schema=17, wal_only_rows=(), keep_open=False):
    prof = tmp_path / "profile"
    prof.mkdir()
    (prof / fc.MARKER).write_text("bot profile")
    conn = sqlite3.connect(prof / "cookies.sqlite", isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute(f"PRAGMA user_version={schema}")
    conn.execute(SCHEMA17)
    ins = ("INSERT INTO moz_cookies(originAttributes,name,value,host,path,expiry,isSecure,"
           "isHttpOnly) VALUES(?,?,?,?,?,?,?,?)")
    conn.executemany(ins, rows)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.executemany(ins, wal_only_rows)
    if keep_open:
        return prof, conn
    # Simulate "Firefox still running": leave the WAL un-checkpointed by not closing.
    return prof, conn


def auth_rows(oa="", expiry=FUTURE_MS):
    return [(oa, "LOGIN_INFO", SECRET, ".youtube.com", "/", expiry, 1, 1),
            (oa, "SAPISID", "x", ".youtube.com", "/", expiry, 1, 0)]


def test_reads_schema17_ms_expiry_and_feeds_existing_parser(tmp_path):
    prof, conn = make_profile(tmp_path, auth_rows())
    res = fc.read_youtube_cookies(prof, now=NOW)
    conn.close()
    assert res.schema == 17 and res.kept == 2
    assert f"\t{NOW + 86400 * 300}\t" in res.netscape_text  # ms -> s
    jar = parse_cookie_text(res.netscape_text).jar
    assert has_auth_cookies(jar)
    assert any(c.has_nonstandard_attr("HttpOnly") for c in jar if c.name == "LOGIN_INFO")


def test_schema15_seconds_expiry(tmp_path):
    rows = [(oa, n, v, h, p, e // 1000, s, ho) for oa, n, v, h, p, e, s, ho in auth_rows()]
    prof, conn = make_profile(tmp_path, rows, schema=15)
    res = fc.read_youtube_cookies(prof, now=NOW)
    conn.close()
    assert res.kept == 2 and f"\t{NOW + 86400 * 300}\t" in res.netscape_text


def test_rows_only_in_wal_are_seen(tmp_path):
    prof, conn = make_profile(tmp_path, [], wal_only_rows=auth_rows())
    assert (prof / "cookies.sqlite-wal").stat().st_size > 0
    res = fc.read_youtube_cookies(prof, now=NOW)
    conn.close()
    assert res.kept == 2


def test_containers_partitioned_expired_and_other_hosts_ignored(tmp_path):
    rows = (auth_rows("^userContextId=2")
            + [("^partitionKey=%28https%2Cexample.com%29", "X", "1", ".youtube.com", "/",
                FUTURE_MS, 1, 0),
               ("", "OLD", "1", ".youtube.com", "/", (NOW - 10) * 1000, 1, 0),
               ("", "SID", "1", ".google.com", "/", FUTURE_MS, 1, 0),
               ("", "PREF", "1", "www.youtube.com", "/", FUTURE_MS, 1, 0)])
    prof, conn = make_profile(tmp_path, rows)
    res = fc.read_youtube_cookies(prof, now=NOW)
    conn.close()
    assert (res.kept, res.other_origin_attrs, res.expired) == (1, 3, 1)
    assert "www.youtube.com\tFALSE\t/" in res.netscape_text
    assert not has_auth_cookies(parse_cookie_text(res.netscape_text).jar)


def test_not_sqlite_is_reported_without_values(tmp_path):
    prof = tmp_path / "profile"
    prof.mkdir()
    (prof / fc.MARKER).write_text("bot profile")
    (prof / "cookies.sqlite").write_bytes(b"\x8f" * 4096 + SECRET.encode())
    with pytest.raises(fc.FirefoxCookieError) as err:
        fc.read_youtube_cookies(prof, now=NOW)
    assert "not a plain SQLite" in str(err.value) and SECRET not in str(err.value)


def test_missing_profile_db_is_transient_and_marker_is_required(tmp_path):
    with pytest.raises(fc.FirefoxCookieError, match="marker") as err:
        fc.read_youtube_cookies(tmp_path, now=NOW)
    assert not err.value.transient
    (tmp_path / fc.MARKER).write_text("x")
    with pytest.raises(fc.FirefoxCookieError, match="no cookies.sqlite") as err:
        fc.read_youtube_cookies(tmp_path, now=NOW)
    assert err.value.transient
    with pytest.raises(fc.FirefoxCookieError, match="does not exist") as err:
        fc.read_youtube_cookies(tmp_path / "nope", now=NOW)
    assert not err.value.transient


def test_missing_columns(tmp_path):
    prof = tmp_path / "profile"
    prof.mkdir()
    (prof / fc.MARKER).write_text("bot profile")
    with sqlite3.connect(prof / "cookies.sqlite") as c:
        c.execute("CREATE TABLE moz_cookies(host TEXT, name TEXT, value TEXT)")
    with pytest.raises(fc.FirefoxCookieError, match="lacks columns"):
        fc.read_youtube_cookies(prof, now=NOW)


def test_no_values_in_errors_or_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    prof, conn = make_profile(tmp_path, auth_rows())
    conn.close()
    with open(prof / "cookies.sqlite", "r+b") as fh:  # corrupt a page after the header
        fh.seek(4096)
        fh.write(b"\x00" * 4096)
    with pytest.raises(fc.FirefoxCookieError) as err:
        fc.read_youtube_cookies(prof, now=NOW)
    assert SECRET not in str(err.value) and SECRET not in caplog.text


def test_temp_dir_cleaned_up(tmp_path):
    prof, conn = make_profile(tmp_path, auth_rows())
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    fc.read_youtube_cookies(prof, now=NOW, tmp_parent=scratch)
    conn.close()
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("pause", [0.0, 0.02])
def test_concurrent_writer_never_yields_torn_snapshot(tmp_path, pause):
    prof = tmp_path / "profile"
    prof.mkdir()
    (prof / fc.MARKER).write_text("bot profile")
    db = prof / "cookies.sqlite"
    writer = f"""
import sqlite3, os, random, time
c = sqlite3.connect(r'{db}', isolation_level=None)
c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA wal_autocheckpoint=16')
c.execute('PRAGMA user_version=17'); c.execute('''{SCHEMA17}''')
for k in range(2000):
    c.execute("INSERT INTO moz_cookies(name,value,host,path,expiry,isSecure,isHttpOnly) VALUES(?,?,?,?,?,1,0)", (f'n{{k}}', os.urandom(30).hex(), '.youtube.com', '/', {FUTURE_MS}))
open(r'{tmp_path / "ready"}', 'w').close()
t0 = time.time()
while time.time() - t0 < 8:
    c.execute('BEGIN')
    for _ in range(20):
        c.execute("UPDATE moz_cookies SET value=? WHERE name=?", (os.urandom(30).hex(), f'n{{random.randrange(2000)}}'))
    c.execute('COMMIT')
    time.sleep({pause})
"""
    proc = subprocess.Popen([sys.executable, "-I", "-c", writer])
    while not (tmp_path / "ready").exists():
        time.sleep(0.05)
    ok = gave_up = 0
    while proc.poll() is None:
        try:
            res = fc.read_youtube_cookies(prof, now=NOW)
        except fc.FirefoxCookieError as exc:
            assert "kept changing" in str(exc)  # never a torn/corrupt snapshot
            gave_up += 1
            continue
        assert res.kept == 2000
        ok += 1
    print(f"pause={pause}: ok={ok} gave_up={gave_up}")
    if pause:  # Firefox-like pacing (it batches writes every 500 ms): must read cleanly
        assert ok > 5 and gave_up == 0
    else:  # nonstop writes: may give up often, but never return a torn snapshot
        assert ok >= 1


def test_injection_and_odd_values_are_dropped(tmp_path):
    rows = auth_rows() + [
        ("", "EVIL", "a\nfake.youtube.com\tTRUE\t/\tTRUE\t9999999999\tLOGIN_INFO\tx", ".youtube.com", "/", FUTURE_MS, 1, 0),
        ("", "SPACE", "a b", ".youtube.com", "/", FUTURE_MS, 1, 0),
        ("", "EMPTY", "", ".youtube.com", "/", FUTURE_MS, 1, 0),
        ("", "SECONDS", "1", ".youtube.com", "/", NOW + 100, 1, 0),  # seconds despite schema 17
    ]
    prof, conn = make_profile(tmp_path, rows)
    res = fc.read_youtube_cookies(prof, now=NOW)
    conn.close()
    assert (res.kept, res.invalid) == (3, 3)
    loaded = parse_cookie_text(res.netscape_text)
    assert loaded.malformed_lines == 0 and loaded.kept == 3
    assert f"\t{NOW + 100}\tSECONDS" in res.netscape_text


def test_firefox_files_are_never_opened_through_sqlite(tmp_path):
    prof, conn = make_profile(tmp_path, [], wal_only_rows=auth_rows())
    shm = prof / "cookies.sqlite-shm"
    before = {p.name: p.read_bytes() for p in prof.iterdir() if p.name != shm.name}
    shm.unlink(missing_ok=True)  # as on macOS unix-excl (heap WAL index) / after a crash
    fc.read_youtube_cookies(prof, now=NOW, tmp_parent=tmp_path / "t")
    assert not shm.exists()
    assert {p.name: p.read_bytes() for p in prof.iterdir()} == before
    conn.close()


def test_short_wal_is_ignored(tmp_path):
    prof, conn = make_profile(tmp_path, auth_rows())
    conn.close()
    (prof / "cookies.sqlite-wal").write_bytes(b"\x00" * 10)
    assert fc.read_youtube_cookies(prof, now=NOW).kept == 2


def test_stale_snapshot_dirs_are_removed(tmp_path):
    import os
    prof, conn = make_profile(tmp_path, auth_rows())
    scratch = tmp_path / "tmp"
    (scratch / "ffck-old").mkdir(parents=True)
    (scratch / "ffck-old" / "cookies.sqlite").write_text("x")
    os.utime(scratch / "ffck-old", (time.time() - 3600,) * 2)
    (scratch / "ffck-new").mkdir()
    fc.read_youtube_cookies(prof, now=NOW, tmp_parent=scratch)
    conn.close()
    assert sorted(p.name for p in scratch.iterdir()) == ["ffck-new"]


def test_wal_generation_change_forces_a_new_snapshot(tmp_path, monkeypatch):
    prof, conn = make_profile(tmp_path, auth_rows(), wal_only_rows=auth_rows("^userContextId=1"))
    generations = iter([b"A" * 12, b"B" * 12] * 10)  # WAL restarted between every pair of reads
    monkeypatch.setattr(fc, "_wal_generation", lambda path: next(generations))
    monkeypatch.setattr(fc.time, "sleep", lambda s: None)
    with pytest.raises(fc.FirefoxCookieError) as err:
        fc.read_youtube_cookies(prof, now=NOW)
    conn.close()
    assert "kept changing" in str(err.value) and err.value.transient
