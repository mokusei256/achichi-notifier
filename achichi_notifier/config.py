"""Configuration loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import base64
import binascii
import os
import tempfile
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


def _cookies_from_b64(b64: str) -> Path:
    try:
        content = base64.b64decode(b64, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ConfigError("YOUTUBE_COOKIES_B64 is not valid base64") from exc
    fd, name = tempfile.mkstemp(prefix="yt-cookies-", suffix=".txt")
    with os.fdopen(fd, "wb") as fh:
        fh.write(content)
    os.chmod(name, 0o600)
    return Path(name)


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
    mentions: dict[str, str]
    fallback_after_minutes: int
    max_age_hours: float
    max_new_per_run: int

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

        cookies_path: Path | None = None
        cookies_file = _env("YOUTUBE_COOKIES_FILE")
        if cookies_file:
            cookies_path = Path(cookies_file).expanduser()
            if not cookies_path.is_file():
                raise ConfigError(f"YOUTUBE_COOKIES_FILE not found: {cookies_path}")
        elif b64 := _env("YOUTUBE_COOKIES_B64"):
            cookies_path = _cookies_from_b64(b64)

        return cls(
            discord_token=_env("DISCORD_BOT_TOKEN"),
            channel_ids=channel_ids,
            state_backend=backend,
            state_channel_id=state_channel,
            state_file=Path(_env("STATE_FILE", "state.json")).expanduser(),
            youtube_channel_id=_env("YOUTUBE_CHANNEL_ID", DEFAULT_YOUTUBE_CHANNEL_ID),
            youtube_handle=_env("YOUTUBE_HANDLE", DEFAULT_YOUTUBE_HANDLE),
            cookies_file=cookies_path,
            mentions=mentions,
            fallback_after_minutes=_env_number("FALLBACK_AFTER_MINUTES", 15),
            max_age_hours=_env_number("MAX_AGE_HOURS", 72.0, float),
            max_new_per_run=_env_number("MAX_NEW_PER_RUN", 10),
        )

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
