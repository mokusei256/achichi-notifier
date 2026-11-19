"""Minimal Discord REST client (bot token). No gateway connection needed for posting."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests

from . import __version__

log = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
# Previews are sent like real notifications (sound and push included) and end with this
# invisible character, which is how recent_urls() tells them from the real thing.
PREVIEW_MARK = "\u200b"  # zero-width space
# Earlier versions marked previews differently; those messages still count as previews.
SUPPRESS_NOTIFICATIONS = 1 << 12  # "@silent" flag (previews of the previous version)
LEGACY_PREVIEW_PREFIX = "【プレビュー】"  # previews of older versions


def mark_preview(content: str) -> str:
    return content + PREVIEW_MARK


def is_preview(message: dict) -> bool:
    content = message.get("content") or ""
    return (content.endswith(PREVIEW_MARK) or content.startswith(LEGACY_PREVIEW_PREFIX)
            or bool(int(message.get("flags") or 0) & SUPPRESS_NOTIFICATIONS))


class DiscordError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status  # HTTP status, None for network problems

    @property
    def transient(self) -> bool:
        """Worth retrying later (network trouble, rate limit, Discord-side errors)."""
        return self.status is None or self.status == 429 or self.status >= 500


class DiscordClient:
    def __init__(self, token: str, timeout: float = 20.0):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bot {token}",
            "User-Agent": f"DiscordBot (https://github.com/achichi-notifier, {__version__})",
        })
        self._me: dict | None = None

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(6):
            try:
                resp = self.session.request(method, API + path, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                if attempt < 2:
                    time.sleep(2)
                    continue
                raise DiscordError(f"{method} {path} failed: {exc.__class__.__name__}") from exc
            if resp.status_code == 429:
                try:
                    wait = float(resp.json().get("retry_after", 1.0))
                except ValueError:
                    wait = 1.0
                log.info("Discord rate limit, waiting %.1fs", wait)
                time.sleep(min(wait, 30.0) + 0.2)
                continue
            if resp.status_code >= 500 and attempt < 2:
                time.sleep(2)
                continue
            if not resp.ok:
                raise DiscordError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}",
                                   status=resp.status_code)
            return resp.json() if resp.content else None
        raise DiscordError(f"{method} {path}: too many retries", status=429)

    def me(self) -> dict:
        if self._me is None:
            self._me = self._request("GET", "/users/@me")
        return self._me

    def send(self, channel_id: str, payload: dict) -> dict:
        return self._request("POST", f"/channels/{channel_id}/messages", json=payload)

    def edit_message(self, channel_id: str, message_id: str, payload: dict) -> dict:
        return self._request("PATCH", f"/channels/{channel_id}/messages/{message_id}", json=payload)

    def delete_message(self, channel_id: str, message_id: str) -> None:
        """Deleting the bot's own messages needs no extra permission (others' would need
        Manage Messages, which this bot is not given)."""
        self._request("DELETE", f"/channels/{channel_id}/messages/{message_id}")

    def get_message(self, channel_id: str, message_id: str) -> dict:
        return self._request("GET", f"/channels/{channel_id}/messages/{message_id}")

    def messages(self, channel_id: str, limit: int = 100, before: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if before:
            params["before"] = before
        return self._request("GET", f"/channels/{channel_id}/messages", params=params)

    def recent_notices(self, channel_id: str, limit: int = 50) -> list[tuple[str, str]]:
        """(embed link, message text) of this bot's own recent notifications in the channel.
        Links posted by people or other bots, and the bot's previews (see is_preview), are
        ignored. The text tells a stream's start notice from its slot notice (same link)."""
        bot_id = self.me()["id"]
        notices: list[tuple[str, str]] = []
        for message in self.messages(channel_id, limit=limit):
            if message.get("author", {}).get("id") != bot_id:
                continue
            if is_preview(message):
                continue
            for embed in message.get("embeds") or []:
                if embed.get("url"):
                    notices.append((embed["url"], message.get("content") or ""))
        return notices

    def recent_urls(self, channel_id: str, limit: int = 50) -> set[str]:
        return {url for url, _ in self.recent_notices(channel_id, limit=limit)}

    def _file_parts(self, content: str, filename: str, data: bytes) -> dict:
        payload = {
            "content": content,
            "attachments": [{"id": 0, "filename": filename}],
            "allowed_mentions": {"parse": []},
        }
        return {
            "data": {"payload_json": json.dumps(payload)},
            "files": {"files[0]": (filename, data, "application/json")},
        }

    def send_file(self, channel_id: str, content: str, filename: str, data: bytes) -> dict:
        return self._request("POST", f"/channels/{channel_id}/messages",
                             **self._file_parts(content, filename, data))

    def edit_file(self, channel_id: str, message_id: str, content: str, filename: str,
                  data: bytes) -> dict:
        return self._request("PATCH", f"/channels/{channel_id}/messages/{message_id}",
                             **self._file_parts(content, filename, data))

    def download(self, url: str) -> bytes:
        try:
            resp = requests.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise DiscordError(f"attachment download failed: {exc.__class__.__name__}") from exc
        if not resp.ok:
            raise DiscordError(f"attachment download -> HTTP {resp.status_code}")
        return resp.content
