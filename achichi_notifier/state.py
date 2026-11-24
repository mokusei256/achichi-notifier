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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .discord_api import DiscordClient, DiscordError

log = logging.getLogger(__name__)

STATE_VERSION = 3
STATE_FILENAME = "achichi-notifier-state.json"
MAX_ENTRIES = 1500
PENDING_TTL_HOURS = 72
BACKLOG_PREFIX = "backlog@"  # pending entries recorded at the members-only comparison baseline
KNOWN_KEYS = {"version", "videos", "posts", "seeded", "runners", "alerts", "pending",
              "destinations", "destinations_updated", "undelivered", "muted", "live",
              "thumbs", "thumbs_listed", "thumbs_listed_ok"}
UNDELIVERED_KEEP_HOURS = 24
LOST_REPORT_KEEP_DAYS = 7  # an unreported lost notification waits this long for the #管理 alert
UNHANDLED_KEEP_DAYS = 30  # an undelivered entry no run has handled yet (bot stopped meanwhile)
MUTED_KEEP_DAYS = 7  # notifications held back by the emergency stop stay listed this long
MUTED_MAX_ENTRIES = 200
LIVE_KEEP_DAYS = 30  # stream start tracking entries (see Notifier.check_live_starts)
LIVE_MAX_ENTRIES = 300
THUMB_KEEP_DAYS = 30  # thumbnail tracking of a video no listing has shown since (see thumbnails.py)
THUMB_MAX_ENTRIES = 3000


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


def _min_iso(a: str | None, b: str | None) -> str | None:
    if not a or not b:
        return a or b
    return b if _max_iso(a, b) == a and a != b else a


def as_int(value) -> int:
    """A stored integer (e.g. a thumbnail version); 0 when missing or unreadable."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return int(value) if isinstance(value, str) and value.isdigit() else 0


def next_reg(entry: dict) -> str:
    """A `watch` registration time later than the entry's previous one (microseconds), so
    the newer registration (or revival) wins on merge (see _live_wins)."""
    now = datetime.now(timezone.utc)
    previous = parse_time(entry.get("reg"))
    if previous and previous + timedelta(microseconds=2) > now:
        now = previous + timedelta(microseconds=2)  # also past bump_reg of the same copy
    return now.isoformat()


def bump_reg(entry: dict) -> str:
    """The smallest step past the entry's "reg", for a change the bot makes to a `watch` entry:
    it wins over that copy on merge, while a `watch` / `watch --remove` saved from the same copy
    (next_reg, two steps or more) still wins over the bot's change."""
    previous = parse_time(entry.get("reg")) or datetime(2000, 1, 1, tzinfo=timezone.utc)
    return (previous + timedelta(microseconds=1)).isoformat()


def _live_rank(entry: dict) -> int:
    done = entry.get("done")
    return 0 if not done else 1 if done in ("stale", "unreadable") else 2


def _live_wins(theirs: dict, mine: dict) -> bool:
    """An announced start always wins (never announced twice). Otherwise a newer `watch`
    registration (or removal) wins, and between copies of one registration the more settled."""
    started_theirs, started_mine = theirs.get("done") == "started", mine.get("done") == "started"
    if started_theirs != started_mine:
        return started_theirs
    reg_theirs = parse_time(theirs.get("reg")) or datetime.min.replace(tzinfo=timezone.utc)
    reg_mine = parse_time(mine.get("reg")) or datetime.min.replace(tzinfo=timezone.utc)
    if reg_theirs != reg_mine:
        return reg_theirs > reg_mine
    return _live_rank(theirs) > _live_rank(mine)


@dataclass
class State:
    videos: dict[str, str] = field(default_factory=dict)  # video_id -> first seen
    posts: dict[str, str] = field(default_factory=dict)  # post_id -> first seen / announced
    seeded: dict[str, str] = field(default_factory=dict)  # scope -> when seeded
    runners: dict[str, dict] = field(default_factory=dict)  # role -> times / counters
    alerts: dict[str, str] = field(default_factory=dict)  # alert key -> last sent
    # Posts that looked members-only only by comparison (no badge); announced after a
    # second confirming observation. post_id -> first seen.
    pending: dict[str, str] = field(default_factory=dict)
    # Extra notification destinations managed with `achichi_notifier dest ...` (name -> dict),
    # and when that list was last changed (the newer list wins on merge).
    destinations: dict[str, dict] = field(default_factory=dict)
    destinations_updated: str | None = None
    # Notifications that reached some destinations but not this one because of a temporary
    # Discord problem: "dest|item_type|item_id" -> {channel, payload (without mention), url,
    # kind, first, tries, done, lost, reported}. Only these are retried, so channels that
    # already got the post never get it twice. lost = given up; reported = told #管理.
    undelivered: dict[str, dict] = field(default_factory=dict)
    # Notifications not sent because the bot was stopped (see control.py; the stop switches
    # themselves live in #管理, not here): url -> {kind, title, at, why}.
    muted: dict[str, dict] = field(default_factory=dict)
    # Streams whose start is watched for (see Notifier.check_live_starts): video_id ->
    # {scheduled: start time or None, done: None while waiting, else why it is settled
    # ("started" = start announced or held back, "ended", "excluded", "stale", "not_live",
    # "removed", "unreadable"), at}. Entries from `watch` also carry external, reg, checked...
    live: dict[str, dict] = field(default_factory=dict)
    # Thumbnails of this channel's videos (see thumbnails.py): video_id -> {v: the version
    # announced last (at first the one found), seen: the newest found (newer than v: a notice is
    # due), since, found, tab, title, listed, checked}; and when every tab page was last read.
    thumbs: dict[str, dict] = field(default_factory=dict)
    thumbs_listed: str | None = None  # when it is next due (a failed listing moves it an hour on)
    thumbs_listed_ok: str | None = None  # when it last worked
    # Unknown top-level keys written by a newer version are carried through untouched,
    # so an older copy of the bot (e.g. on the Mac) never erases them.
    extra: dict = field(default_factory=dict)
    # The role that is saving this state (not persisted). Each role's runner entry is
    # written only by that role, so on merge the other roles' entries come from the
    # freshly loaded copy instead of this run's possibly stale snapshot.
    owner_role: str | None = field(default=None, compare=False)

    @classmethod
    def from_dict(cls, data: dict) -> State:
        return cls(
            videos=dict(data.get("videos") or {}),
            posts=dict(data.get("posts") or {}),
            seeded=dict(data.get("seeded") or {}),
            runners={k: dict(v) for k, v in (data.get("runners") or {}).items()},
            alerts=dict(data.get("alerts") or {}),
            pending=dict(data.get("pending") or {}),
            destinations={k: dict(v) for k, v in (data.get("destinations") or {}).items()},
            destinations_updated=data.get("destinations_updated"),
            undelivered={k: dict(v) for k, v in (data.get("undelivered") or {}).items()
                         if isinstance(v, dict)},
            muted={k: dict(v) for k, v in (data.get("muted") or {}).items() if isinstance(v, dict)},
            live={k: dict(v) for k, v in (data.get("live") or {}).items() if isinstance(v, dict)},
            thumbs={k: dict(v) for k, v in (data.get("thumbs") or {}).items() if isinstance(v, dict)},
            thumbs_listed=data.get("thumbs_listed"),
            thumbs_listed_ok=data.get("thumbs_listed_ok"),
            extra={k: v for k, v in data.items() if k not in KNOWN_KEYS},
        )

    def to_dict(self) -> dict:
        return {
            **self.extra,
            "version": STATE_VERSION,
            "videos": self.videos,
            "posts": self.posts,
            "seeded": self.seeded,
            "runners": self.runners,
            "alerts": self.alerts,
            "pending": self.pending,
            "destinations": self.destinations,
            "destinations_updated": self.destinations_updated,
            "undelivered": self.undelivered,
            "muted": self.muted,
            "live": self.live,
            "thumbs": self.thumbs,
            "thumbs_listed": self.thumbs_listed,
            "thumbs_listed_ok": self.thumbs_listed_ok,
        }

    def merge_from(self, other: State) -> None:
        """Union with a concurrently written state (keeps everything either side has seen)."""
        for mine, theirs in ((self.videos, other.videos), (self.posts, other.posts),
                             (self.pending, other.pending)):
            for key, value in theirs.items():
                mine.setdefault(key, value)
        for key, value in other.seeded.items():
            self.seeded.setdefault(key, value)
        for role, times in other.runners.items():
            if self.owner_role is not None and role != self.owner_role:
                self.runners[role] = dict(times)
                continue
            mine = self.runners.setdefault(role, {})
            for key, value in times.items():
                if key.startswith("last_"):
                    mine[key] = _max_iso(mine.get(key), value)
                else:
                    mine.setdefault(key, value)
        for key, value in other.alerts.items():
            self.alerts[key] = _max_iso(self.alerts.get(key), value)
        self.extra.update(other.extra)  # this version never edits extra; newest copy wins
        if _max_iso(self.destinations_updated, other.destinations_updated) != self.destinations_updated:
            self.destinations = {k: dict(v) for k, v in other.destinations.items()}
            self.destinations_updated = other.destinations_updated
        for key, theirs in other.undelivered.items():
            mine = self.undelivered.get(key)
            if mine is None:
                self.undelivered[key] = dict(theirs)
                continue
            # Finished / given up / reported on either side stays so (never resend or re-alert).
            for stamp in ("done", "lost", "reported"):
                mine[stamp] = _max_iso(mine.get(stamp), theirs.get(stamp))
            mine["tries"] = max(int(mine.get("tries") or 0), int(theirs.get("tries") or 0))
        for key, value in other.muted.items():
            self.muted.setdefault(key, dict(value))
        for key, theirs in other.live.items():
            mine = self.live.get(key)
            # Settled on either side stays settled, so a start is never announced twice.
            # "stale" (gave up waiting) can still turn into "started", so it ranks lower.
            # Registering again with `watch` (newer "reg") starts over (see watch_cli).
            if mine is None:
                self.live[key] = dict(theirs)
                continue
            keep, drop = (dict(theirs), mine) if _live_wins(theirs, mine) else (mine, theirs)
            if drop.get("checked"):  # a throttle stamp: the newest look counts, whichever copy wins
                keep["checked"] = _max_iso(keep.get("checked"), drop.get("checked"))
            self.live[key] = keep
        for key, theirs in other.thumbs.items():
            mine = self.thumbs.get(key)
            if mine is None:
                self.thumbs[key] = dict(theirs)
                continue
            # Versions only grow, so a notice sent on either side is never sent again; the
            # newest listing's title and tab win.
            newer = _max_iso(mine.get("listed"), theirs.get("listed")) != mine.get("listed")
            merged = dict(theirs) if newer else dict(mine)
            merged["v"] = max(as_int(mine.get("v")), as_int(theirs.get("v")))
            merged["seen"] = max(as_int(mine.get("seen")), as_int(theirs.get("seen")), merged["v"])
            for stamp in ("since", "listed", "checked", "found"):
                value = (_min_iso if stamp == "since" else _max_iso)(mine.get(stamp), theirs.get(stamp))
                if value:
                    merged[stamp] = value
            self.thumbs[key] = merged
        self.thumbs_listed = _max_iso(self.thumbs_listed, other.thumbs_listed)
        self.thumbs_listed_ok = _max_iso(self.thumbs_listed_ok, other.thumbs_listed_ok)
        # A pending post that either side has since announced or seen publicly is settled.
        for key in [k for k in self.pending if k in self.posts]:
            del self.pending[key]

    def prune(self, limit: int = MAX_ENTRIES) -> None:
        for name in ("videos", "posts"):
            items: dict[str, str] = getattr(self, name)
            if len(items) > limit:
                newest = sorted(items.items(), key=lambda kv: kv[1], reverse=True)[:limit]
                setattr(self, name, dict(newest))
        now_ts = datetime.now(timezone.utc).timestamp()

        def keep_undelivered(entry: dict) -> bool:
            if not entry.get("done") and not entry.get("lost"):
                # Not handled yet (e.g. the bot was stopped for days): the next run that may send
                # gives it up and reports it, so it is never dropped unreported.
                t = parse_time(entry.get("first"))
                return t is None or t.timestamp() >= now_ts - UNHANDLED_KEEP_DAYS * 86400
            if entry.get("lost") and not entry.get("reported"):
                t = parse_time(entry.get("lost"))
                return t is None or t.timestamp() >= now_ts - LOST_REPORT_KEEP_DAYS * 86400
            t = parse_time(entry.get("done") or entry.get("first"))
            return t is None or t.timestamp() >= now_ts - UNDELIVERED_KEEP_HOURS * 3600

        self.undelivered = {k: v for k, v in self.undelivered.items()
                            if isinstance(v, dict) and keep_undelivered(v)}
        muted_cutoff = now_ts - MUTED_KEEP_DAYS * 86400
        muted = sorted(((k, v) for k, v in self.muted.items() if isinstance(v, dict)
                        and ((t := parse_time(v.get("at"))) is None or t.timestamp() >= muted_cutoff)),
                       key=lambda kv: str(kv[1].get("at") or ""), reverse=True)
        self.muted = dict(muted[:MUTED_MAX_ENTRIES])
        live_cutoff = now_ts - LIVE_KEEP_DAYS * 86400
        live = sorted(((k, v) for k, v in self.live.items() if isinstance(v, dict)
                       and ((t := parse_time(v.get("at"))) is None or t.timestamp() >= live_cutoff)),
                      key=lambda kv: str(kv[1].get("at") or ""), reverse=True)
        self.live = dict(live[:LIVE_MAX_ENTRIES])
        thumb_cutoff = now_ts - THUMB_KEEP_DAYS * 86400
        thumbs = sorted(((k, v) for k, v in self.thumbs.items() if isinstance(v, dict)
                         and ((t := parse_time(v.get("listed"))) is None or t.timestamp() >= thumb_cutoff)),
                        key=lambda kv: str(kv[1].get("listed") or ""), reverse=True)
        self.thumbs = dict(thumbs[:THUMB_MAX_ENTRIES])
        cutoff = datetime.now(timezone.utc).timestamp() - PENDING_TTL_HOURS * 3600
        self.pending = {k: v for k, v in self.pending.items()
                        if (t := parse_time(v.removeprefix(BACKLOG_PREFIX))) is None
                        or t.timestamp() >= cutoff}

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
