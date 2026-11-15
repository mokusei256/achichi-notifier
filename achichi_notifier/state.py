"""Seen-item state shared between runners (GitHub Actions / Mac).

The canonical store is a JSON attachment on a bot message in a private Discord
channel, so every runner sees the same state without any extra infrastructure.
A local file store is available for development / single-host setups.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .discord_api import DiscordClient, DiscordError

log = logging.getLogger(__name__)

STATE_VERSION = 1
STATE_FILENAME = "achichi-notifier-state.json"
MAX_ENTRIES = 1500


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _max_iso(a: str | None, b: str | None) -> str | None:
    if not a or not b:
        return a or b
    return a if (parse_time(a) or datetime.min.replace(tzinfo=timezone.utc)) >= (
        parse_time(b) or datetime.min.replace(tzinfo=timezone.utc)) else b


@dataclass
class State:
    videos: dict[str, str] = field(default_factory=dict)  # video_id -> first seen
    posts: dict[str, str] = field(default_factory=dict)  # post_id -> first seen
    seeded: dict[str, str] = field(default_factory=dict)  # scope -> when seeded
    runners: dict[str, dict[str, str]] = field(default_factory=dict)  # role -> times
    alerts: dict[str, str] = field(default_factory=dict)  # alert key -> last sent

    @classmethod
    def from_dict(cls, data: dict) -> State:
        return cls(
            videos=dict(data.get("videos") or {}),
            posts=dict(data.get("posts") or {}),
            seeded=dict(data.get("seeded") or {}),
            runners={k: dict(v) for k, v in (data.get("runners") or {}).items()},
            alerts=dict(data.get("alerts") or {}),
        )

    def to_dict(self) -> dict:
        return {
            "version": STATE_VERSION,
            "videos": self.videos,
            "posts": self.posts,
            "seeded": self.seeded,
            "runners": self.runners,
            "alerts": self.alerts,
        }

    def merge_from(self, other: State) -> None:
        """Union with a concurrently written state (keeps everything either side has seen)."""
        for mine, theirs in ((self.videos, other.videos), (self.posts, other.posts)):
            for key, value in theirs.items():
                mine.setdefault(key, value)
        for key, value in other.seeded.items():
            self.seeded.setdefault(key, value)
        for role, times in other.runners.items():
            mine = self.runners.setdefault(role, {})
            for key, value in times.items():
                if key.startswith("last_"):
                    mine[key] = _max_iso(mine.get(key), value)
                else:
                    mine.setdefault(key, value)
        for key, value in other.alerts.items():
            self.alerts[key] = _max_iso(self.alerts.get(key), value)

    def prune(self, limit: int = MAX_ENTRIES) -> None:
        for name in ("videos", "posts"):
            items: dict[str, str] = getattr(self, name)
            if len(items) > limit:
                newest = sorted(items.items(), key=lambda kv: kv[1], reverse=True)[:limit]
                setattr(self, name, dict(newest))

    def dumps(self) -> bytes:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=1, sort_keys=True).encode()


class FileStateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> State:
        if not self.path.is_file():
            return State()
        return State.from_dict(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, state: State) -> None:
        state.merge_from(self.load())
        state.prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".json")
        with os.fdopen(fd, "wb") as fh:
            fh.write(state.dumps())
        os.replace(tmp, self.path)

    def describe(self) -> str:
        return f"file:{self.path}"


class DiscordStateStore:
    """State stored as a JSON attachment on one bot message in a private channel."""

    SEARCH_PAGES = 5

    def __init__(self, client: DiscordClient, channel_id: str):
        self.client = client
        self.channel_id = channel_id
        self.message_id: str | None = None

    def _is_state_message(self, message: dict, bot_id: str) -> bool:
        return message.get("author", {}).get("id") == bot_id and any(
            a.get("filename") == STATE_FILENAME for a in message.get("attachments", [])
        )

    def _find(self) -> dict | None:
        if self.message_id:
            try:
                return self.client.get_message(self.channel_id, self.message_id)
            except DiscordError:
                self.message_id = None
        bot_id = self.client.me()["id"]
        before = None
        for _ in range(self.SEARCH_PAGES):
            batch = self.client.messages(self.channel_id, limit=100, before=before)
            if not batch:
                break
            for message in batch:
                if self._is_state_message(message, bot_id):
                    self.message_id = message["id"]
                    return message
            before = batch[-1]["id"]
        return None

    def load(self) -> State:
        message = self._find()
        if not message:
            return State()
        attachment = next(a for a in message["attachments"] if a.get("filename") == STATE_FILENAME)
        return State.from_dict(json.loads(self.client.download(attachment["url"])))

    def save(self, state: State) -> None:
        state.merge_from(self.load())
        state.prune()
        ts = int(datetime.now(timezone.utc).timestamp())
        content = f"🗂 achichi-notifier の状態ファイルです（自動更新・削除しないでください）\n最終更新: <t:{ts}:R>"
        if self.message_id:
            self.client.edit_file(self.channel_id, self.message_id, content, STATE_FILENAME, state.dumps())
        else:
            message = self.client.send_file(self.channel_id, content, STATE_FILENAME, state.dumps())
            self.message_id = message["id"]

    def describe(self) -> str:
        return f"discord:{self.channel_id}"
