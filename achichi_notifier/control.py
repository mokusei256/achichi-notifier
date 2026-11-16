"""Emergency stop.

Two switches, both read live from #管理 by every runner right before it would send:
- `achichi_notifier stop` posts a "⏸ Bot を止めています" marker message there (the bot's own
  message; `resume`, or deleting the message by hand, lifts the stop);
- a 🛑 reaction by anyone but the bot on a recent message there (works from a phone; remove
  it to go on).
Neither lives in the shared state JSON, so a runner saving an older copy of the state can never
undo a stop (that JSON has no compare-and-swap).

While stopped, runners keep checking YouTube and saving the state, but send nothing to
Discord: no notifications, no retries, no #管理 alerts. What they would have announced is
marked as seen (so it is never sent later, e.g. stale "stream starting" notices after a long
stop) and listed in state.muted for `resume` / `status` to show.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .discord_api import DiscordClient, DiscordError

log = logging.getLogger(__name__)

STOP_EMOJI = "🛑"
SCAN_MESSAGES = 100  # the latest messages in #管理 that are looked at (one request)
MARKER_PREFIX = "⏸ **Bot を止めています**"
DISCORD_EPOCH_MS = 1420070400000


def snowflake_time(message_id: str) -> datetime:
    return datetime.fromtimestamp(((int(message_id) >> 22) + DISCORD_EPOCH_MS) / 1000, tz=timezone.utc)


def _is_stop_emoji(emoji: dict) -> bool:
    return not emoji.get("id") and (emoji.get("name") or "").replace("️", "") == STOP_EMOJI


@dataclass
class StopState:
    markers: list[dict] = field(default_factory=list)  # the bot's stop markers, newest first
    reacted: bool = False  # someone put 🛑 on a recent message

    @property
    def stopped(self) -> bool:
        return bool(self.markers) or self.reacted

    @property
    def since(self) -> datetime | None:
        return min((snowflake_time(m["id"]) for m in self.markers), default=None)

    def reason(self) -> str | None:
        if self.markers:
            return "stop コマンド"
        if self.reacted:
            return f"#管理 の {STOP_EMOJI} リアクション"
        return None


def is_marker(message: dict, bot_id: str) -> bool:
    return ((message.get("author") or {}).get("id") == bot_id
            and (message.get("content") or "").startswith(MARKER_PREFIX))


def has_stop_reaction(message: dict) -> bool:
    """Someone other than this bot put 🛑 on the message."""
    return any(_is_stop_emoji(r.get("emoji") or {}) and int(r.get("count") or 0) - (1 if r.get("me") else 0) > 0
               for r in message.get("reactions") or [])


def read_stop(discord: DiscordClient, channel_id: str) -> StopState:
    bot_id = discord.me()["id"]
    state = StopState()
    for message in discord.messages(channel_id, limit=SCAN_MESSAGES) or []:
        if is_marker(message, bot_id):
            state.markers.append(message)
        if has_stop_reaction(message):
            state.reacted = True
    return state


def switch_available(cfg) -> bool:
    return bool(cfg.state_channel_id)


def pause_reason(cfg, discord: DiscordClient | None) -> str | None:
    """Why the bot is stopped right now, or None. A failed #管理 read does not stop the bot
    (that channel was just read for the state, so this is rare, and it is logged)."""
    if discord is None or not switch_available(cfg):
        return None
    try:
        return read_stop(discord, cfg.state_channel_id).reason()
    except (DiscordError, ValueError, KeyError, TypeError) as exc:  # Value/Key/Type: garbled data
        log.warning("could not read #管理 for the stop switches (%s marker / %s): %s",
                    "stop", STOP_EMOJI, exc)
        return None


RESUME_HINT = ("再開: stop コマンドで止めたなら `uv run python -m achichi_notifier resume`"
               f"（または #管理 の「Bot を止めています」を削除）、{STOP_EMOJI} で止めたなら {STOP_EMOJI} を外す")
