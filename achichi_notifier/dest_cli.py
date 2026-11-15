"""`achichi_notifier dest ...`: manage notification destinations from the terminal.

Destinations live in the shared state (the JSON on the private #管理 channel), so a change made
on the Mac is used by the Windows primary on its next run (within ~5 minutes).
"""

from __future__ import annotations

import re

from . import destinations as dests
from .config import Config, ConfigError
from .discord_api import DiscordClient, DiscordError
from .state import now_iso, parse_time

import time
from datetime import datetime, timedelta, timezone

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
SNOWFLAKE_RE = re.compile(r"\d{17,20}")


def _store(cfg: Config):
    from .notifier import make_store
    if not cfg.discord_token:
        raise ConfigError("DISCORD_BOT_TOKEN is not set")
    discord = DiscordClient(cfg.discord_token)
    return discord, make_store(cfg, discord)


def _warn_old_runners(state) -> None:
    """Runners that ran recently but predate destinations only post to main."""
    now = datetime.now(timezone.utc)
    for role, runner in sorted(state.runners.items()):
        last = parse_time(runner.get("last_run"))
        if last and now - last < timedelta(hours=24) and "destinations" not in (runner.get("features") or []):
            print(f"⚠️  [!!] {role} の PC は古い版のようです（追加の送り先に送れません）。"
                  "Windows は git pull → install.ps1、Mac は ./deploy/mac/install.sh で更新してください。")


def _save_destinations(store, mutate) -> bool:
    """Apply mutate(state) and save; re-check because a runner saving at the same moment can
    overwrite the change (Discord has no compare-and-swap). Returns True when it stuck."""
    for _ in range(3):
        state = store.load()
        mutate(state)
        state.destinations_updated = now_iso()
        stamp = state.destinations_updated
        expected = {k: dict(v) for k, v in state.destinations.items()}
        store.save(state)
        time.sleep(2)
        check = store.load()
        if check.destinations_updated == stamp or check.destinations == expected:
            return True
    return False


def _describe_channel(discord: DiscordClient, channel_id: str) -> str:
    """'#name (server)' if the bot can see the channel, else raise DiscordError."""
    channel = discord._request("GET", f"/channels/{channel_id}")
    guild = discord._request("GET", f"/guilds/{channel['guild_id']}") if channel.get("guild_id") else {}
    discord.messages(channel_id, limit=1)  # needs View Channel + Read Message History
    return f"#{channel.get('name', '?')} ({guild.get('name', '?')})"


def cmd_list(cfg: Config) -> int:
    discord, store = _store(cfg)
    state = store.load()
    _warn_old_runners(state)
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
    _warn_old_runners(state)
    existed = name in state.destinations

    def mutate(st) -> None:
        old = dests.Destination.from_dict(name, st.destinations[name]) if name in st.destinations else None
        merged_mentions = dict(old.mentions) if old else {}
        merged_mentions.update(mentions)  # re-adding keeps mentions/kinds/enabled you did not pass
        st.destinations[name] = dests.Destination(
            name=name, channels=channels, mentions=merged_mentions,
            kinds=kinds if kinds is not None else (set(old.kinds) if old else set(dests.KIND_CHOICES)),
            enabled=old.enabled if old else True,
        ).to_dict()

    if not _save_destinations(store, mutate):
        print("❌ [NG] 保存できませんでした（同時に Bot が状態を書き換えた可能性）。もう一度実行してください。")
        return 1
    print(f"✅ [OK] 送り先「{name}」を{'更新' if existed else '追加'}しました。次の確認（5 分以内）から通知が届きます。")
    print("   テスト送信: uv run python -m achichi_notifier dest check " + name + " --send-test")
    return 0


def cmd_remove(cfg: Config, name: str) -> int:
    name = name.lower()
    discord, store = _store(cfg)
    if name not in store.load().destinations:
        print(f"送り先「{name}」はありません（main は .env で設定します）。")
        return 1
    if not _save_destinations(store, lambda st: st.destinations.pop(name, None)):
        print("❌ [NG] 保存できませんでした。もう一度実行してください。")
        return 1
    print(f"✅ [OK] 送り先「{name}」を削除しました。")
    return 0


def cmd_enable(cfg: Config, name: str, enabled: bool) -> int:
    name = name.lower()
    discord, store = _store(cfg)
    if name not in store.load().destinations:
        print(f"送り先「{name}」はありません。")
        return 1

    def mutate(st) -> None:
        if name in st.destinations:
            st.destinations[name]["enabled"] = enabled

    if not _save_destinations(store, mutate):
        print("❌ [NG] 保存できませんでした。もう一度実行してください。")
        return 1
    print(f"✅ [OK] 送り先「{name}」を{'有効' if enabled else '停止'}にしました。")
    return 0


def cmd_check(cfg: Config, name: str, send_test: bool) -> int:
    name = name.lower()
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
