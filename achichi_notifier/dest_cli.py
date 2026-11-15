"""`achichi_notifier dest ...`: manage notification destinations from the terminal.

Destinations live in the shared state (the JSON on the private #管理 channel), so a change made
on the Mac is used by the Windows primary on its next run (within ~5 minutes).
"""

from __future__ import annotations

import re

from . import destinations as dests
from .config import Config, ConfigError
from .discord_api import DiscordClient, DiscordError
from .state import now_iso

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
SNOWFLAKE_RE = re.compile(r"\d{17,20}")


def _store(cfg: Config):
    from .notifier import make_store
    if not cfg.discord_token:
        raise ConfigError("DISCORD_BOT_TOKEN is not set")
    discord = DiscordClient(cfg.discord_token)
    return discord, make_store(cfg, discord)


def _describe_channel(discord: DiscordClient, channel_id: str) -> str:
    """'#name (server)' if the bot can see the channel, else raise DiscordError."""
    channel = discord._request("GET", f"/channels/{channel_id}")
    guild = discord._request("GET", f"/guilds/{channel['guild_id']}") if channel.get("guild_id") else {}
    discord.messages(channel_id, limit=1)  # needs View Channel + Read Message History
    return f"#{channel.get('name', '?')} ({guild.get('name', '?')})"


def cmd_list(cfg: Config) -> int:
    discord, store = _store(cfg)
    state = store.load()
    for dest in dests.resolve(cfg, state):
        status = "on " if dest.enabled else "off"
        channels = ", ".join(f"{k}={v}" for k, v in sorted(dest.channels.items()))
        mentions = ", ".join(f"{k}={v}" for k, v in sorted(dest.mentions.items())) or "-"
        source = ".env" if dest.name == dests.MAIN else "dest add"
        print(f"[{status}] {dest.name} ({source})")
        print(f"      channels: {channels}")
        print(f"      kinds:    {', '.join(sorted(dest.kinds))}")
        print(f"      mentions: {mentions}")
    return 0


def _parse_kinds(raw: str | None) -> set[str] | None:
    if raw is None:
        return None
    kinds = {k.strip() for k in raw.split(",") if k.strip()}
    unknown = kinds - set(dests.KIND_CHOICES)
    if unknown or not kinds:
        raise ConfigError(f"--kinds must be a comma list of {', '.join(dests.KIND_CHOICES)}")
    return kinds


def cmd_add(cfg: Config, args) -> int:
    name = args.name.lower()
    if not NAME_RE.fullmatch(name) or name == dests.MAIN:
        raise ConfigError("name: use lowercase letters, digits, '-' or '_' (and not 'main')")
    channels = {"default": args.channel}
    for group in dests.CHANNEL_GROUPS:
        value = getattr(args, f"{group}_channel")
        if value:
            channels[group] = value
    for value in channels.values():
        if not SNOWFLAKE_RE.fullmatch(value):
            raise ConfigError(f"not a Discord channel id: {value}")
    mentions = {}
    for key in dests.KIND_CHOICES:
        value = getattr(args, f"mention_{key}")
        if value:
            mentions[key] = value
    kinds = _parse_kinds(args.kinds)

    discord, store = _store(cfg)
    for group, channel_id in sorted(channels.items()):
        try:
            print(f"  {group}: {channel_id} -> {_describe_channel(discord, channel_id)}")
        except DiscordError as exc:
            print(f"❌ [NG] Bot がチャンネル {channel_id} を見られません: {exc}")
            print("   Bot をそのサーバーに招待し、チャンネルの権限（見る・送信・埋め込みリンク・履歴を読む）を付けてください。")
            return 1
    state = store.load()
    existed = name in state.destinations
    state.destinations[name] = dests.Destination(
        name=name, channels=channels, mentions=mentions,
        kinds=kinds if kinds is not None else set(dests.KIND_CHOICES), enabled=True,
    ).to_dict()
    state.destinations_updated = now_iso()
    store.save(state)
    print(f"✅ [OK] 送り先「{name}」を{'更新' if existed else '追加'}しました。次の確認（5 分以内）から通知が届きます。")
    print("   テスト送信: uv run python -m achichi_notifier dest check " + name + " --send-test")
    return 0


def cmd_remove(cfg: Config, name: str) -> int:
    discord, store = _store(cfg)
    state = store.load()
    if name not in state.destinations:
        print(f"送り先「{name}」はありません（main は .env で設定します）。")
        return 1
    del state.destinations[name]
    state.destinations_updated = now_iso()
    store.save(state)
    print(f"✅ [OK] 送り先「{name}」を削除しました。")
    return 0


def cmd_enable(cfg: Config, name: str, enabled: bool) -> int:
    discord, store = _store(cfg)
    state = store.load()
    if name not in state.destinations:
        print(f"送り先「{name}」はありません。")
        return 1
    state.destinations[name]["enabled"] = enabled
    state.destinations_updated = now_iso()
    store.save(state)
    print(f"✅ [OK] 送り先「{name}」を{'有効' if enabled else '停止'}にしました。")
    return 0


def cmd_check(cfg: Config, name: str, send_test: bool) -> int:
    discord, store = _store(cfg)
    state = store.load()
    found = [d for d in dests.resolve(cfg, state) if d.name == name]
    if not found:
        print(f"送り先「{name}」はありません。")
        return 1
    dest, problems = found[0], 0
    for channel_id in dest.all_channels():
        try:
            print(f"✅ [OK] {channel_id} -> {_describe_channel(discord, channel_id)}")
        except DiscordError as exc:
            problems += 1
            print(f"❌ [NG] {channel_id}: {exc}")
            continue
        if send_test:
            try:
                discord.send(channel_id, {"content": "🔧 achichi-notifier のテスト送信です（この送り先は使えます）",
                                          "allowed_mentions": {"parse": []}})
                print(f"✅ [OK] {channel_id}: テスト送信しました")
            except DiscordError as exc:
                problems += 1
                print(f"❌ [NG] {channel_id}: 送信できません: {exc}")
    return 1 if problems else 0
