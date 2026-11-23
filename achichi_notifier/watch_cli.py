"""`watch`: announce the start of a stream on another channel (e.g. a collab).

The bot only reads this channel's tabs, so a stream hosted elsewhere is registered by hand.
It goes into state.live marked "external"; every full run then looks at its watch page from
LIVE_START_LEAD before its start time on and sends one "🔴 配信が始まりました" to the same
destinations as this channel's streams (see Notifier.check_live_starts).
"""

from __future__ import annotations

import re

from .config import Config, ConfigError
from .control_cli import CMD, _client, _store
from .notifier import LIVE_START_LEAD
from .state import next_reg, now_iso, parse_time
from .youtube import VIDEO_ID_RE, VideoRef, YouTubeClient

_URL_ID = re.compile(r"(?:[?&]v=|youtu\.be/|/live/|/shorts/|/embed/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")
STATUS = {None: "待機中（開始したら通知）", "started": "開始を通知済み", "ended": "終わっていた（通知なし）",
          "stale": "開始予定（不明なら登録）から 12 時間たっても始まらず、確認をやめた", "removed": "登録を外した",
          "unreadable": "視聴ページを読めない状態が続き、確認をやめた（削除・非公開の可能性）",
          "excluded": "除外タイトル（通知なし）", "not_live": "配信ではなかった（通知なし）"}


def video_id_of(text: str) -> str:
    text = text.strip()
    if VIDEO_ID_RE.fullmatch(text):
        return text
    if match := _URL_ID.search(text):
        return match.group(1)
    raise ConfigError(f"YouTube の動画 URL か 11 文字の動画 ID を指定してください: {text}")


def cmd_add(cfg: Config, target: str) -> int:
    video_id = video_id_of(target)
    info = YouTubeClient(cfg.youtube_channel_id).fetch_video_info(VideoRef(video_id))
    if not info.from_watch_page:
        print(f"⚠️  [!!] 視聴ページを読めませんでした（{video_id}）。少し待ってもう一度実行してください。")
        return 1
    if info.kind not in ("live_upcoming", "live_now"):
        print(f"⚠️  [!!] 配信の枠ではないので登録しませんでした（種類: {info.kind}）: {info.title}")
        return 1
    discord = _client(cfg)
    store = _store(cfg, discord)
    state = store.load()
    entry = state.live.get(video_id) or {}
    if entry.get("done") == "started":
        print(f"この配信の開始はもう通知済みです: {info.title}")
        return 0
    start = info.scheduled_start if info.kind == "live_upcoming" else info.started
    # A new "reg" makes this registration win over an older copy (after --remove, or a stream
    # that was given up), also against a runner saving its older state; an announced start
    # still wins on merge (state._live_wins), so this can never cause a second notice.
    reg = next_reg(entry)
    now = now_iso()
    state.live[video_id] = {
        "scheduled": start.isoformat() if start else None, "done": None, "at": now,
        "external": True, "title": info.title[:120], "channel": info.channel_title, "reg": reg,
        "checked": now,  # its page was just read: rechecks count from here, not an older look
    }
    store.save(state)
    print(f"✅ 登録しました: {info.title}（{info.channel_title or 'チャンネル不明'}）")
    if info.kind == "live_now":
        print("   もう配信中です。次の確認（主系は 5 分ごと）で「🔴 配信が始まりました」を送ります。")
    elif start:
        minutes = int(LIVE_START_LEAD.total_seconds() // 60)
        print(f"   開始予定 {start.astimezone().strftime('%m/%d %H:%M')}。その {minutes} 分前から毎回確かめ、"
              "始まったら「🔴 配信が始まりました」を送ります（枠の通知は送りません）。")
    else:
        print("   開始予定時刻が分からないため、10 分ごとに確かめます（登録から 12 時間たっても始まらなければ確認をやめます）。")
    print(f"   取り消す: {CMD} watch --remove {video_id}")
    return 0


def cmd_list(cfg: Config) -> int:
    state = _store(cfg, _client(cfg)).load()
    entries = [(k, v) for k, v in state.live.items() if v.get("external")]
    if not entries:
        print("watch で登録した配信はありません。")
        return 0
    for video_id, entry in sorted(entries, key=lambda kv: str(kv[1].get("scheduled") or "")):
        start = parse_time(entry.get("scheduled"))
        when = start.astimezone().strftime("%m/%d %H:%M") if start else "開始予定不明"
        print(f"{video_id}  {when}  {STATUS.get(entry.get('done'), entry.get('done'))}  "
              f"{entry.get('title') or ''}（{entry.get('channel') or '?'}）")
    return 0


def cmd_remove(cfg: Config, target: str) -> int:
    video_id = video_id_of(target)
    store = _store(cfg, _client(cfg))
    state = store.load()
    entry = state.live.get(video_id)
    if not entry or not entry.get("external"):
        print(f"watch で登録した配信ではありません: {video_id}")
        return 1
    if entry.get("done"):
        print(f"すでに確認を終えています（{STATUS.get(entry.get('done'), entry.get('done'))}）。")
        return 0
    # Settled rather than deleted: a runner saving its older copy merges entries back in. A new
    # "reg" makes the removal win over that copy (and a later `watch` wins over the removal).
    state.live[video_id] = {**entry, "done": "removed", "at": now_iso(), "reg": next_reg(entry)}
    store.save(state)
    print(f"✅ 登録を外しました: {entry.get('title') or video_id}")
    return 0
