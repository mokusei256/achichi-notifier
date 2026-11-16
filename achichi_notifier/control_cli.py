"""`achichi_notifier stop | resume | status | delete`: emergency stop and clean-up.

stop posts a "⏸ Bot を止めています" marker in #管理 and resume deletes it (see control.py), so a
command run on the Mac stops the Windows primary too, from its next run. From a phone, a 🛑
reaction on a recent message in #管理 does the same. delete removes the bot's own messages from
the destination channels; it lists them first and deletes only with --yes.
"""

from __future__ import annotations

import re
import shlex
from datetime import datetime, timedelta, timezone

from . import control
from . import destinations as dests
from . import render
from .config import Config, ConfigError
from .discord_api import PREVIEW_MARK, DiscordClient, DiscordError, is_preview
from .state import STATE_FILENAME, now_iso, parse_time

DISCORD_EPOCH_MS = control.DISCORD_EPOCH_MS
DELETE_SCAN_PAGES = 10  # delete looks at most 1000 messages back in each channel
MAX_SINCE = timedelta(days=3650)
CMD = "uv run python -m achichi_notifier"
SNOWFLAKE_RE = re.compile(r"\d{17,20}")


def _client(cfg: Config) -> DiscordClient:
    if not cfg.discord_token:
        raise ConfigError("DISCORD_BOT_TOKEN is not set")
    return DiscordClient(cfg.discord_token)


def _store(cfg: Config, discord: DiscordClient):
    from .notifier import make_store
    return make_store(cfg, discord)


def _admin_channel(cfg: Config) -> str:
    if not control.switch_available(cfg):
        raise ConfigError("DISCORD_STATE_CHANNEL_ID（#管理）が設定されていないため、stop / resume は使えません")
    return cfg.state_channel_id


def _local(value: str | datetime | None) -> str:
    when = parse_time(value) if isinstance(value, str) else value
    if when is None:
        return "?"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone().strftime("%m/%d %H:%M")


def _warn_old_runners(state) -> None:
    """Every runner without the stop feature, however long ago it last ran: an idle Mac
    fallback does not update last_run, but it would ignore the stop if it took over."""
    old = sorted(role for role, r in state.runners.items() if "pause" not in (r.get("features") or []))
    if old:
        print(f"⚠️  [!!] {', '.join(old)} の PC は古い版で、止める仕組みがありません（その PC が動けば送ります）。"
              "Windows は git pull → install.ps1、Mac は ./deploy/mac/install.sh で更新してください。"
              "すぐ止めたいときは、その PC のタスクを止めるか、Discord で Bot の送信権限を外してください。")


def _print_muted(state, since: datetime | None, limit: int = 30) -> None:
    entries = []
    for url, entry in state.muted.items():
        at = parse_time(entry.get("at"))
        if since is None or (at is not None and at >= since):
            entries.append((entry.get("at") or "", url, entry))
    entries.sort()
    scope = f"{_local(since)} 以降" if since else "直近 7 日"
    if not entries:
        print(f"   止めていた間に送らなかった通知（{scope}）: なし")
        return
    print(f"   止めていた間に送らなかった通知（{scope}）: {len(entries)} 件"
          "（再開しても送りません。必要なら手動で共有してください）")
    for at, url, entry in entries[-limit:]:
        headline = render.HEADLINES.get(str(entry.get("kind")), str(entry.get("kind")))
        print(f"     {_local(at)}  {headline}  {entry.get('title') or ''}")
        if url.startswith("http"):
            print(f"       {url}")
    if len(entries) > limit:
        print(f"     （古い {len(entries) - limit} 件は省略）")


def cmd_stop(cfg: Config, reason: str | None = None) -> int:
    discord = _client(cfg)
    channel = _admin_channel(cfg)
    try:  # nice to have; must never keep the stop from being written
        _warn_old_runners(_store(cfg, discord).load())
    except (DiscordError, ValueError, KeyError, TypeError) as exc:
        print(f"ℹ️  共有の状態を読めませんでした（古い版の PC の確認を省きます）: {exc}")
    try:
        before = control.read_stop(discord, channel)
    except (DiscordError, ValueError, KeyError, TypeError):
        before = control.StopState()
    if before.markers:
        print(f"⏸ [OK] すでに止めています（{_local(before.since)} から）。")
        print(f"   再開: {CMD} resume")
        return 0
    ts = int(datetime.now(timezone.utc).timestamp())
    lines = [f"{control.MARKER_PREFIX}（<t:{ts}:f> から・stop コマンド）"]
    if reason:
        lines.append(f"理由: {reason[:200]}")
    lines.append("Discord には何も送りません（通知・送り直し・#管理 への警告もすべて）。")
    lines.append(f"再開: `{CMD} resume`、またはこのメッセージを削除")
    try:
        discord.send(channel, {"content": "\n".join(lines), "allowed_mentions": {"parse": []}})
    except DiscordError as exc:
        print(f"❌ [NG] #管理 に停止の印を書けませんでした: {exc}")
        print(f"   代わりに、スマホなどで #管理 のメッセージに {control.STOP_EMOJI} のリアクションを付けてください。")
        return 1
    after = control.read_stop(discord, channel)
    if not after.markers:
        print("❌ [NG] 停止の印を書いたのに読み取れません。もう一度実行するか、"
              f"#管理 のメッセージに {control.STOP_EMOJI} を付けてください。")
        return 1
    print(f"⏸ [OK] Bot を止めました（#管理 に「Bot を止めています」を書きました）。")
    print("   次の確認（主系は 5 分ごと）から、Discord には何も送りません（通知・送り直し・#管理 への警告もすべて）。"
          "その瞬間に送信中の回は止まりません。")
    print("   止めている間も YouTube の確認は続け、見つかった新着は送らずに記録します。再開しても送りません。")
    print(f"   再開: {CMD} resume（スマホからは #管理 の「Bot を止めています」を削除）")
    return 0


def cmd_resume(cfg: Config) -> int:
    discord = _client(cfg)
    channel = _admin_channel(cfg)
    state = _store(cfg, discord).load()
    _warn_old_runners(state)
    before = control.read_stop(discord, channel)
    since = before.since
    reacted_markers = [m for m in before.markers if control.has_stop_reaction(m)]
    failed = 0
    for marker in before.markers:
        try:
            discord.delete_message(channel, marker["id"])
        except DiscordError as exc:
            if exc.status != 404:
                failed += 1
                print(f"❌ [NG] 「Bot を止めています」を削除できませんでした: {exc}")
    after = control.read_stop(discord, channel)
    if failed or after.markers:
        print("❌ [NG] まだ「Bot を止めています」が残っています。もう一度実行するか、#管理 で手動で削除してください。")
        return 1
    if before.markers:
        print(f"▶️  [OK] stop を解除しました（{_local(since)} から止めていました）。")
        if reacted_markers:
            print(f"   ※「Bot を止めています」に付いていた {control.STOP_EMOJI} も一緒に消えました"
                  f"（{control.STOP_EMOJI} でも止めておきたいなら、別のメッセージに付け直してください）。")
    else:
        print("ℹ️  stop コマンドでは止めていません。")
    if after.reacted:
        print(f"⚠️  [!!] #管理 に {control.STOP_EMOJI} のリアクションが残っているので、まだ止まっています。"
              f"{control.STOP_EMOJI} を外すと次の確認から送ります。")
        _print_muted(state, since)
        return 1
    if before.markers:
        print("   次の確認から通知を送ります。")
    _print_muted(state, since)
    return 0


def cmd_status(cfg: Config) -> int:
    discord = _client(cfg)
    state = _store(cfg, discord).load()
    try:
        stop = control.read_stop(discord, _admin_channel(cfg))
    except (DiscordError, ValueError, KeyError, TypeError) as exc:
        print(f"⚠️  [!!] #管理 を読めず、止まっているか確認できませんでした: {exc}")
        stop = None
    if stop is None:
        pass
    elif stop.stopped:
        print("⏸ Bot は止まっています（Discord には何も送りません）。")
    else:
        print("▶️  Bot は動いています。")
    if stop is not None:
        print(f"   - stop コマンド: {_local(stop.since) + ' から' if stop.markers else 'なし'}")
        print(f"   - #管理 の {control.STOP_EMOJI} リアクション: {'あり（外すと再開）' if stop.reacted else 'なし'}")
    now = datetime.now(timezone.utc)
    for role, runner in sorted(state.runners.items()):
        last = parse_time(runner.get("last_run"))
        ago = f"{int((now - last).total_seconds() // 60)} 分前" if last else "記録なし"
        support = "対応" if "pause" in (runner.get("features") or []) else "古い版（止まりません）"
        print(f"   - {role}: 最終確認 {ago} / 停止機能 {support}")
    _print_muted(state, None)
    return 0


# ---------------------------------------------------------------------- delete

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(m|min|h|d)")
VIDEO_ID = r"[A-Za-z0-9_-]{11}"
POST_ID = r"Ug[A-Za-z0-9_-]{10,}"
_ID_PATTERNS = (
    re.compile(rf"[?&]v=({VIDEO_ID})(?![A-Za-z0-9_-])"),
    re.compile(rf"youtu\.be/({VIDEO_ID})(?![A-Za-z0-9_-])"),
    re.compile(rf"/(?:shorts|live|embed)/({VIDEO_ID})(?![A-Za-z0-9_-])"),
    re.compile(rf"/post/({POST_ID})"),
    re.compile(rf"[?&]lb=({POST_ID})"),
)


def parse_duration(text: str) -> timedelta:
    match = _DURATION_RE.fullmatch(text.strip().lower())
    if not match:
        raise ConfigError("--since は 30m・2h・1d のように指定してください")
    value, unit = float(match[1]), match[2]
    minutes = value * (1440 if unit == "d" else 60 if unit == "h" else 1)
    if not 0 < minutes <= MAX_SINCE.total_seconds() / 60:
        raise ConfigError("--since には 0 より大きく 3650 日以下の時間を指定してください")
    return timedelta(minutes=minutes)


def item_id(text: str) -> str:
    for pattern in _ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return match[1]
    bare = text.strip()
    if re.fullmatch(VIDEO_ID, bare) or re.fullmatch(POST_ID, bare):
        return bare
    raise ConfigError("--url には YouTube の動画・投稿の URL か ID（動画は 11 文字、投稿は Ug で始まる ID）を指定してください")


def _embed_urls(message: dict) -> list[str]:
    return [e.get("url") or "" for e in message.get("embeds") or []]


def _mentions_item(message: dict, item: str) -> bool:
    pattern = re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(item)}(?![A-Za-z0-9_-])")
    return any(pattern.search(url) for url in _embed_urls(message))


def _sent_at(message: dict) -> datetime:
    return control.snowflake_time(message["id"])


def _is_state_message(message: dict) -> bool:
    return any(a.get("filename") == STATE_FILENAME for a in message.get("attachments") or [])


def _scan(discord: DiscordClient, channel_id: str, bot_id: str, cutoff: datetime | None,
          last: int | None, item: str | None, ids: set[int] | None = None) -> tuple[list[dict], bool]:
    """The bot's own messages in the channel matching the filters, newest first.
    With ids, only those messages (the ones a previous list showed) are looked for.
    Returns (messages, complete): complete is False when the scan limit cut it short."""
    found: list[dict] = []
    newest, oldest = (max(ids), min(ids)) if ids else (None, None)
    before = str(newest + 1) if newest is not None else None  # Discord: strictly older than `before`
    for _ in range(DELETE_SCAN_PAGES):
        batch = discord.messages(channel_id, limit=100, before=before)
        if not batch:
            return found, True
        for message in batch:
            mid = int(message["id"])
            if ids is not None:
                if mid < oldest:
                    return found, True
                if mid not in ids:
                    continue
            if cutoff is not None and _sent_at(message) < cutoff:
                return found, True
            if (message.get("author") or {}).get("id") != bot_id or _is_state_message(message):
                continue
            if item and not _mentions_item(message, item):
                continue
            found.append(message)
            if last and ids is None and len(found) >= last:
                return found, True
        if len(batch) < 100:
            return found, True
        before = batch[-1]["id"]
    return found, False


def _describe(message: dict) -> str:
    content = (message.get("content") or "").replace(PREVIEW_MARK, "").strip()
    headline = content.splitlines()[0] if content else ""
    embed = (message.get("embeds") or [{}])[0]
    parts = [headline[:80] or "（本文なし）"]
    if is_preview(message):
        parts.append("〔プレビュー〕")
    if embed.get("title"):
        parts.append(str(embed["title"])[:60])
    return "  ".join(parts)


def _delete_command(since, last, url, dest_names, channel_ids, ids: list[str]) -> str:
    """The command that deletes exactly the listed messages (the same filters, pinned to ids)."""
    parts = []
    if since:
        parts.append(f"--since={since}")
    if last:
        parts.append(f"--last={last}")
    if url:
        parts.append(f"--url={url}")
    parts += [f"--dest={name}" for name in dest_names or []]
    parts += [f"--channel={channel_id}" for channel_id in channel_ids or []]
    parts.append("--ids=" + ",".join(ids))
    return " ".join([CMD, "delete", *map(shlex.quote, parts), "--yes"])


def _settle_retries(cfg: Config, discord: DiscordClient, deleted: set[tuple[str, str]]) -> int:
    """A deleted notification must not come back through a pending retry to that channel."""
    if not deleted:
        return 0
    store = _store(cfg, discord)
    state = store.load()
    settled = 0
    for entry in state.undelivered.values():
        if not entry.get("done") and (entry.get("channel"), entry.get("url")) in deleted:
            entry["done"] = now_iso()
            settled += 1
    if settled:
        store.save(state)  # "done" wins on merge, so a concurrent runner save keeps it
    return settled


def cmd_delete(cfg: Config, since: str | None = None, last: int | None = None, url: str | None = None,
               dest_names: list[str] | None = None, yes: bool = False,
               channel_ids: list[str] | None = None, ids: str | None = None) -> int:
    if not (since or last or url):
        raise ConfigError("消す範囲を --since（例: 1h）・--last（件数）・--url のどれかで指定してください")
    if last is not None and last < 1:
        raise ConfigError("--last には 1 以上を指定してください")
    for channel_id in channel_ids or []:
        if not SNOWFLAKE_RE.fullmatch(channel_id):
            raise ConfigError(f"チャンネル ID ではありません: {channel_id}")
    pinned: set[int] | None = None
    if ids is not None:
        values = [v.strip() for v in ids.split(",") if v.strip()]
        if not values or not all(SNOWFLAKE_RE.fullmatch(v) for v in values):
            raise ConfigError("--ids には一覧の最後に表示されるコマンドの値（メッセージ ID のカンマ区切り）を指定してください")
        pinned = {int(v) for v in values}
    cutoff = datetime.now(timezone.utc) - parse_duration(since) if since else None
    item = item_id(url) if url else None

    discord = _client(cfg)
    state = _store(cfg, discord).load()
    destinations = dests.resolve(cfg, state)  # disabled ones too: they may hold runaway posts
    users: dict[str, list[str]] = {}  # channel -> every destination posting there
    for dest in destinations:
        for channel_id in dest.all_channels():
            users.setdefault(channel_id, []).append(dest.name)
    wanted = {n.lower() for n in dest_names} if dest_names else None
    if wanted:
        unknown = sorted(wanted - {d.name for d in destinations})
        if unknown:
            raise ConfigError(f"送り先がありません: {', '.join(unknown)}（dest list で確認できます）")
    channels: set[str] = set()
    if wanted or not channel_ids:
        for dest in destinations:
            if wanted is None or dest.name in wanted:
                channels.update(dest.all_channels())
    channels.update(channel_ids or [])
    if cfg.state_channel_id and cfg.state_channel_id in channels:
        channels.discard(cfg.state_channel_id)
        print("ℹ️  #管理（状態保存のチャンネル）は対象外です。")
    label = {c: "/".join(users.get(c) or [f"チャンネル {c}"]) for c in channels}
    for channel_id in sorted(channels):
        if wanted and len(users.get(channel_id) or []) > 1:
            print(f"ℹ️  チャンネル {channel_id} は送り先 {label[channel_id]} の共用です（ほかの送り先の分も対象になります）。")

    bot_id = discord.me()["id"]
    found: list[tuple[str, dict]] = []
    unread = cut_short = 0
    for channel_id in sorted(channels):
        try:
            messages, complete = _scan(discord, channel_id, bot_id, cutoff, last, item, pinned)
        except DiscordError as exc:
            unread += 1
            print(f"❌ [NG] [{label[channel_id]}]（{channel_id}）を読めません: {exc}")
            continue
        found += [(channel_id, m) for m in messages]
        if not complete:
            cut_short += 1
            print(f"ℹ️  [{label[channel_id]}]: 直近 {DELETE_SCAN_PAGES * 100} 件より前は見ていません。")
    found.sort(key=lambda cm: int(cm[1]["id"]), reverse=True)  # newest first across channels
    if last and pinned is None:
        found = found[:last]  # --last counts across every selected channel
    incomplete = bool(unread or cut_short)
    if pinned is not None:
        missing = len(pinned) - len(found)
        if missing > 0:
            print(f"ℹ️  一覧にあった {missing} 件は見つかりませんでした（すでに消えている、または条件に合わなくなった）。")
    if not found:
        print("該当する Bot のメッセージはありませんでした" + ("（読めなかったチャンネルがあります）。" if unread else "。"))
        return 1 if unread else 0

    print(f"Bot が送ったメッセージ {len(found)} 件{'を消します' if yes else 'が見つかりました'}"
          "（人や他の Bot の投稿は対象外）:")
    for channel_id, message in found:
        print(f"  {_local(_sent_at(message))}  [{label[channel_id]}]  {_describe(message)}")
    if not yes:
        if incomplete and last:
            print("⚠️  [!!] 読めなかった・途中までしか見ていないチャンネルがあるため、--last の「新しい順」は正確でない可能性があります。")
        print("※ まだ消していません。この一覧のメッセージだけを消すには:")
        print("  " + _delete_command(since, last, url, dest_names, channel_ids, [m["id"] for _, m in found]))
        return 1 if unread else 0
    if pinned is None and last and incomplete:
        print("❌ [NG] 読めなかった・途中までしか見ていないチャンネルがあり、--last の対象を正しく決められないので消しません。"
              "--yes を外して一覧を確認してください。")
        return 1

    deleted = failed = 0
    gone: set[tuple[str, str]] = set()  # (channel, url) of deleted real notifications
    for channel_id, message in found:
        try:
            discord.delete_message(channel_id, message["id"])
        except DiscordError as exc:
            if exc.status != 404:  # 404: already gone
                failed += 1
                print(f"❌ [NG] [{label[channel_id]}] {_describe(message)}: 削除できません: {exc}")
                continue
        deleted += 1
        if not is_preview(message):
            gone.update((channel_id, u) for u in _embed_urls(message) if u)
    print(f"✅ [OK] {deleted} 件削除しました。" + (f"（{failed} 件は削除できませんでした）" if failed else ""))
    try:
        settled = _settle_retries(cfg, discord, gone)
        if settled:
            print(f"   消した通知の送り直し待ち {settled} 件も取り消しました。")
    except DiscordError as exc:
        print(f"⚠️  [!!] 送り直し待ちを確認できませんでした（消した通知が送り直される可能性があります）: {exc}")
    if incomplete:
        print("⚠️  [!!] 読めなかった・途中までしか見ていないチャンネルがあります。もう一度実行して確認してください。")
    return 1 if failed or unread else 0
