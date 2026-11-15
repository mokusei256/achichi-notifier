"""Configuration loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_YOUTUBE_CHANNEL_ID = "UC8eitCE9Z6EwUCs-VUi1blg"  # @AchichiMela
DEFAULT_YOUTUBE_HANDLE = "@AchichiMela"

# Destination groups. Each can be routed to its own Discord channel / mention.
KINDS = ("video", "short", "live", "post")


class ConfigError(Exception):
    pass


def load_dotenv(path: Path) -> None:
    """Minimal .env loader. Existing environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_number(name: str, default: float, cast=int):
    raw = _env(name)
    if raw is None:
        return default
    try:
        return cast(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number (got {raw!r})") from exc


def _decode_text(data: bytes) -> tuple[str | None, str | None]:
    try:
        return data.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return None, "cookie data is not UTF-8 text"


def decode_cookie_secret(value: str) -> tuple[str | None, str | None]:
    """YOUTUBE_COOKIES_B64 may hold base64 (std or url-safe, wrapped or not) or the raw
    cookies.txt text. Returns (text, error); errors never contain cookie values."""
    stripped = value.strip().lstrip("\ufeff")
    if stripped.startswith("#") or "\t" in stripped:
        return stripped, None
    compact = "".join(stripped.split())
    compact += "=" * (-len(compact) % 4)
    for altchars in (None, b"-_"):
        try:
            raw = base64.b64decode(compact, altchars=altchars, validate=True)
        except (binascii.Error, ValueError):
            continue
        return _decode_text(raw)
    return None, "YOUTUBE_COOKIES_B64 is neither base64 nor cookies.txt text"


@dataclass(frozen=True)
class Config:
    discord_token: str | None
    channel_ids: dict[str, str]
    state_backend: str
    state_channel_id: str | None
    state_file: Path
    youtube_channel_id: str
    youtube_handle: str
    cookies_file: Path | None
    cookies_secret: str | None
    mentions: dict[str, str]
    fallback_after_minutes: int
    max_age_hours: float
    max_new_per_run: int
    member_check_minutes: int

    @classmethod
    def from_env(cls) -> Config:
        default_channel = _env("DISCORD_CHANNEL_ID")
        channel_ids: dict[str, str] = {}
        for kind in KINDS:
            channel = _env(f"DISCORD_CHANNEL_ID_{kind.upper()}", default_channel)
            if channel:
                channel_ids[kind] = channel

        mentions: dict[str, str] = {}
        for key in (*KINDS, "member_post"):
            value = _env(f"MENTION_{key.upper()}")
            if value:
                mentions[key] = value

        state_channel = _env("DISCORD_STATE_CHANNEL_ID")
        backend = (_env("STATE_BACKEND") or ("discord" if state_channel else "file")).lower()
        if backend not in ("discord", "file"):
            raise ConfigError("STATE_BACKEND must be 'discord' or 'file'")

        cookies_file = _env("YOUTUBE_COOKIES_FILE")

        return cls(
            discord_token=_env("DISCORD_BOT_TOKEN"),
            channel_ids=channel_ids,
            state_backend=backend,
            state_channel_id=state_channel,
            state_file=Path(_env("STATE_FILE", "state.json")).expanduser(),
            youtube_channel_id=_env("YOUTUBE_CHANNEL_ID", DEFAULT_YOUTUBE_CHANNEL_ID),
            youtube_handle=_env("YOUTUBE_HANDLE", DEFAULT_YOUTUBE_HANDLE),
            cookies_file=Path(cookies_file).expanduser() if cookies_file else None,
            cookies_secret=None if cookies_file else _env("YOUTUBE_COOKIES_B64"),
            mentions=mentions,
            fallback_after_minutes=_env_number("FALLBACK_AFTER_MINUTES", 15),
            max_age_hours=_env_number("MAX_AGE_HOURS", 72.0, float),
            max_new_per_run=_env_number("MAX_NEW_PER_RUN", 10),
            member_check_minutes=_env_number("MEMBER_CHECK_MINUTES", 10),
        )

    @property
    def cookies_configured(self) -> bool:
        return bool(self.cookies_file or self.cookies_secret)

    def read_cookie_text(self) -> tuple[str | None, str | None]:
        """Return (cookies.txt text, error). A broken cookie setup must never stop the
        rest of the bot, so problems are reported instead of raised."""
        if self.cookies_file:
            try:
                data = self.cookies_file.read_bytes()
            except OSError as exc:
                return None, f"cookie file unreadable ({exc.__class__.__name__})"
            return _decode_text(data)
        if self.cookies_secret:
            return decode_cookie_secret(self.cookies_secret)
        return None, None

    def require_discord(self) -> None:
        if not self.discord_token:
            raise ConfigError("DISCORD_BOT_TOKEN is not set")
        missing = [k for k in KINDS if k not in self.channel_ids]
        if missing:
            raise ConfigError(
                "DISCORD_CHANNEL_ID is not set (missing channel for: " + ", ".join(missing) + ")"
            )
        if self.state_backend == "discord" and not self.state_channel_id:
            raise ConfigError("STATE_BACKEND=discord requires DISCORD_STATE_CHANNEL_ID")
