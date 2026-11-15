import base64
import logging

import requests

from achichi_notifier.config import Config, decode_cookie_secret
from achichi_notifier.youtube import YouTubeClient, has_auth_cookies, parse_cookie_text

SECRET = "FAKE_SECRET_VALUE_123"
LINES = [
    ".youtube.com\tTRUE\t/\tFALSE\t1829317744\tHSID\tAAAA",
    ".youtube.com\tTRUE\t/\tTRUE\t1829317744\tSAPISID\t" + SECRET,
    "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1829317745\tLOGIN_INFO\tLLLL",
    ".youtube.com\tTRUE\t/\tTRUE\t0\tYSC\tsession",
    ".google.com\tTRUE\t/\tTRUE\t1829317745\tSID\tgoogle-secret",
]
COOKIES = "# Netscape HTTP Cookie File\n# generated\n\n" + "\n".join(LINES) + "\n"


def _sent_cookie_header(jar, url="https://www.youtube.com/channel/x/posts"):
    session = requests.Session()
    session.cookies = jar
    return session.prepare_request(requests.Request("GET", url)).headers.get("Cookie", "")


def test_parse_standard_file_keeps_youtube_only():
    loaded = parse_cookie_text(COOKIES)
    assert loaded.kept == 4 and loaded.other_domains == 1 and loaded.malformed_lines == 0
    header = _sent_cookie_header(loaded.jar)
    for name in ("HSID", "SAPISID", "LOGIN_INFO", "YSC"):  # incl. HttpOnly + expires=0
        assert name + "=" in header
    assert "google-secret" not in header
    assert has_auth_cookies(loaded.jar)
    assert _sent_cookie_header(loaded.jar, "https://i.ytimg.com/vi/x/hqdefault.jpg") == ""


def test_parse_tolerates_bom_crlf_and_missing_header():
    text = "﻿" + "\r\n".join(LINES) + "\r\n"
    loaded = parse_cookie_text(text)
    assert loaded.kept == 4 and has_auth_cookies(loaded.jar)


def test_malformed_lines_are_counted_not_echoed(caplog):
    text = COOKIES + "garbage line with " + SECRET + "\n.youtube.com\tTRUE\t/\tTRUE\tsoon\tX\ty\n"
    with caplog.at_level(logging.DEBUG):
        client = YouTubeClient("UC", text)
    assert client.has_cookies
    assert "2 malformed" in caplog.text and SECRET not in caplog.text


def test_missing_login_cookies_disable_member_session():
    client = YouTubeClient("UC", LINES[0] + "\n")
    assert client.cookies_configured and not client.has_cookies
    assert "LOGIN_INFO" in client.cookie_error and "AAAA" not in client.cookie_error


def test_decode_secret_variants():
    raw = COOKIES.encode()
    std = base64.b64encode(raw).decode()
    wrapped = "\n".join(std[i:i + 76] for i in range(0, len(std), 76)) + "\n"
    raw_q = ("# ????\n" + COOKIES).encode()  # chosen so url-safe base64 contains '_'
    urlsafe = base64.urlsafe_b64encode(raw_q).decode().rstrip("=")
    assert "_" in urlsafe or "-" in urlsafe
    for value in (std, wrapped, urlsafe, COOKIES, "﻿" + COOKIES):
        text, error = decode_cookie_secret(value)
        assert error is None and "SAPISID" in text, value[:20]
    text, error = decode_cookie_secret("not base64 at all !!!")
    assert text is None and "neither" in error
    text, error = decode_cookie_secret(base64.b64encode(b"\xff\xfe\x00bad").decode())
    assert text is None and SECRET not in error


def test_missing_cookie_file_is_reported_not_raised(tmp_path):
    cfg = Config(
        discord_token=None, channel_ids={}, state_backend="file", state_channel_id=None,
        state_file=tmp_path / "s.json", youtube_channel_id="UC", youtube_handle="@x",
        cookies_file=tmp_path / "missing.txt", cookies_secret=None, mentions={},
        fallback_after_minutes=15, max_age_hours=72, max_new_per_run=10, member_check_minutes=10,
    )
    text, error = cfg.read_cookie_text()
    assert text is None and "unreadable" in error
    client = YouTubeClient.from_config(cfg)
    assert client.cookies_configured and not client.has_cookies and client.cookie_error


def test_non_ascii_cookie_values_are_dropped_without_crashing():
    text = COOKIES + ".youtube.com\tTRUE\t/\tTRUE\t1829317745\tPREF\tf6=40\u2019x\n"
    loaded = parse_cookie_text(text)
    assert loaded.malformed_lines == 1 and has_auth_cookies(loaded.jar)
    broken_login = COOKIES.replace("LLLL", "LL\u2019LL")
    client = YouTubeClient("UC", broken_login)
    assert not client.has_cookies and client.cookie_error


def test_auth_cookie_check_needs_both_parts():
    only_login = parse_cookie_text(LINES[2] + "\n").jar
    only_sapisid = parse_cookie_text(LINES[1] + "\n").jar
    assert not has_auth_cookies(only_login) and not has_auth_cookies(only_sapisid)


def test_member_cookies_never_reach_the_logged_out_session():
    client = YouTubeClient("UC", COOKIES)
    member_header = _sent_cookie_header(client.member_session.cookies)
    public_header = _sent_cookie_header(client.session.cookies)
    assert "SAPISID=" in member_header and "LOGIN_INFO=" in member_header
    assert "SAPISID" not in public_header and "LOGIN_INFO" not in public_header
    assert "SOCS=CAI" in public_header
