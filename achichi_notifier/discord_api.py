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
# Message flag: posted without a push / desktop notification ("@silent"). Previews use it, and
# it is how recent_urls() tells them from real notifications.
SUPPRESS_NOTIFICATIONS = 1 << 12
LEGACY_PREVIEW_PREFIX = "【プレビュー】"  # previews sent by older versions


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

    def get_message(self, channel_id: str, message_id: str) -> dict:
        return self._request("GET", f"/channels/{channel_id}/messages/{message_id}")

    def messages(self, channel_id: str, limit: int = 100, before: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if before:
            params["before"] = before
        return self._request("GET", f"/channels/{channel_id}/messages", params=params)

    def recent_urls(self, channel_id: str, limit: int = 50) -> set[str]:
        """Embed links of this bot's own recent notifications in the channel. Links posted
        by people or other bots, and the bot's previews (silent messages, or 【プレビュー】
        from older versions), are ignored."""
        bot_id = self.me()["id"]
        urls: set[str] = set()
        for message in self.messages(channel_id, limit=limit):
            if message.get("author", {}).get("id") != bot_id:
                continue
            if int(message.get("flags") or 0) & SUPPRESS_NOTIFICATIONS:
                continue
            if (message.get("content") or "").startswith(LEGACY_PREVIEW_PREFIX):
                continue
            for embed in message.get("embeds") or []:
                if embed.get("url"):
                    urls.add(embed["url"])
        return urls

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
