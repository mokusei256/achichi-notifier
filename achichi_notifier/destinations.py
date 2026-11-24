"""Where notifications go.

The channels in .env form the built-in "main" destination. More destinations (e.g. another,
larger Discord server) are added from the terminal with `achichi_notifier dest add ...` and
are stored in the shared state, so every runner (Windows primary, Mac fallback) picks them up
without editing any file.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import render

MAIN = "main"
# What a destination can receive. member_post is separate from post so a server can get
# public posts without the members-only links (or the other way round). The opt-in kinds go
# to the .env destination ("main") only, unless a `dest add` destination lists them in --kinds:
# collab = start notices of `watch` streams on other channels; thumbnail = a changed thumbnail
# of this channel's videos, shorts or streams.
KIND_CHOICES = ("video", "short", "live", "post", "member_post", "collab", "thumbnail")
OPT_IN_KINDS = ("collab", "thumbnail")
DEFAULT_KINDS = tuple(k for k in KIND_CHOICES if k not in OPT_IN_KINDS)
CHANNEL_GROUPS = ("video", "short", "live", "post")
# Destination kind -> the kind whose mention it uses when it has none of its own.
MENTION_FALLBACK = {"member_post": "post", "collab": "live"}


def kind_key(kind: str) -> str:
    """Notification kind (render.HEADLINES key) -> destination kind key."""
    return render.DEST_KINDS.get(kind) or render.GROUPS[kind]


@dataclass
class Destination:
    name: str
    channels: dict[str, str]  # "default" and/or video/short/live/post -> channel id
    mentions: dict[str, str] = field(default_factory=dict)  # kind key -> mention text
    kinds: set[str] = field(default_factory=lambda: set(KIND_CHOICES))  # main: everything
    enabled: bool = True

    def wants(self, kind: str) -> bool:
        return self.enabled and kind_key(kind) in self.kinds

    def channel_for(self, kind: str) -> str | None:
        group = render.GROUPS[kind]
        return self.channels.get(group) or self.channels.get("default")

    def mention_for(self, kind: str) -> str | None:
        key = kind_key(kind)
        return self.mentions.get(key) or self.mentions.get(MENTION_FALLBACK.get(key, ""))

    def all_channels(self) -> list[str]:
        return sorted(set(self.channels.values()))

    def to_dict(self) -> dict:
        return {
            "channels": dict(self.channels),
            "mentions": dict(self.mentions),
            "kinds": sorted(self.kinds),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, name: str, data: dict) -> Destination:
        kinds = data.get("kinds")
        return cls(
            name=name,
            channels={k: str(v) for k, v in (data.get("channels") or {}).items() if v},
            mentions={k: str(v) for k, v in (data.get("mentions") or {}).items() if v},
            kinds=set(kinds) & set(KIND_CHOICES) if kinds is not None else set(DEFAULT_KINDS),
            enabled=bool(data.get("enabled", True)),
        )


def main_destination(cfg) -> Destination | None:
    """The destination defined by .env (DISCORD_CHANNEL_ID / _VIDEO ... and MENTION_*)."""
    if not cfg.channel_ids:
        return None
    mentions = dict(cfg.mentions)
    return Destination(name=MAIN, channels=dict(cfg.channel_ids), mentions=mentions)


def resolve(cfg, state) -> list[Destination]:
    """All destinations: main (from .env) first, then the ones stored in the shared state."""
    result = [d for d in (main_destination(cfg),) if d is not None]
    for name, data in sorted((state.destinations or {}).items()):
        if name == MAIN:
            continue
        dest = Destination.from_dict(name, data)
        if dest.channels:
            result.append(dest)
    return result
