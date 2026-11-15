"""Build Discord message payloads (content + embed) for each notification type."""

from __future__ import annotations

from datetime import datetime

from .youtube import ChannelMeta, Post, VideoInfo

HEADLINES = {
    "video": "🎬 新しい動画が公開されました",
    "short": "📱 新しいショートが公開されました",
    "premiere": "🎬 プレミア公開の予定が作成されました",
    "live_upcoming": "📅 配信枠が作成されました",
    "live_now": "🔴 配信が始まりました",
    "live_archive": "📼 配信アーカイブが公開されました",
    "post": "📝 新しい投稿がありました",
    "member_post": "🔒 メンバー限定の投稿がありました",
}
LABELS = {
    "video": "動画", "short": "ショート", "premiere": "プレミア公開",
    "live_upcoming": "配信予定", "live_now": "ライブ配信中", "live_archive": "配信アーカイブ",
    "post": "投稿", "member_post": "メンバー限定投稿",
}
COLORS = {
    "video": 0xFF0033, "short": 0xFF4E8A, "premiere": 0xFF0033,
    "live_upcoming": 0x2ECC71, "live_now": 0xE01E1E, "live_archive": 0x95A5A6,
    "post": 0x5865F2, "member_post": 0xD4AF37,
}
# Which destination group (channel / mention) each notification kind belongs to.
GROUPS = {
    "video": "video", "premiere": "video", "short": "short",
    "live_upcoming": "live", "live_now": "live", "live_archive": "live",
    "post": "post", "member_post": "post",
}


def _cut(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _author(meta: ChannelMeta | None) -> dict | None:
    if not meta or not meta.title:
        return None
    author = {"name": _cut(meta.title, 256)}
    if meta.url:
        author["url"] = meta.url
    if meta.avatar_url:
        author["icon_url"] = meta.avatar_url
    return author


def _content(kind: str, mention: str | None) -> str:
    headline = HEADLINES[kind]
    return f"{mention} {headline}" if mention else headline


def _allowed_mentions(mention: str | None) -> dict:
    return {"parse": ["roles", "users", "everyone"]} if mention else {"parse": []}


def _discord_time(dt: datetime) -> str:
    ts = int(dt.timestamp())
    return f"<t:{ts}:F>（<t:{ts}:R>）"


def video_payload(info: VideoInfo, meta: ChannelMeta | None, mention: str | None) -> dict:
    kind = info.kind
    embed: dict = {
        "title": _cut(info.title, 256),
        "url": info.url,
        "color": COLORS[kind],
        "image": {"url": info.thumbnail},
        "footer": {"text": f"YouTube・{LABELS[kind]}"},
    }
    if author := _author(meta):
        embed["author"] = author
    if info.published:
        embed["timestamp"] = info.published.isoformat()
    fields = []
    if info.scheduled_start and kind in ("live_upcoming", "premiere"):
        fields.append({"name": "開始予定", "value": _discord_time(info.scheduled_start), "inline": False})
    if info.members_only:
        fields.append({"name": "公開範囲", "value": "🔒 メンバー限定", "inline": True})
    if fields:
        embed["fields"] = fields
    return {
        "content": _content(kind, mention),
        "embeds": [embed],
        "allowed_mentions": _allowed_mentions(mention),
    }


def post_payload(post: Post, meta: ChannelMeta | None, mention: str | None) -> dict:
    footer = "YouTube・投稿" + (f"・{post.published_text}" if post.published_text else "")
    if post.members_only:
        # Members-only: never include text, images, poll choices or attachments.
        embed: dict = {
            "title": "🔒 メンバー限定の投稿",
            "url": post.url,
            "description": "内容はメンバー限定のため表示していません。\nリンク先（メンバーのみ閲覧可）で確認してください。",
            "color": COLORS["member_post"],
            "footer": {"text": footer},
        }
        kind = "member_post"
    else:
        lines = post.text.strip().splitlines()
        title = _cut(lines[0], 200) if lines and lines[0].strip() else "投稿"
        body = "\n".join(lines[1:]).strip() if lines and lines[0].strip() else post.text.strip()
        embed = {
            "title": title,
            "url": post.url,
            "color": COLORS["post"],
            "footer": {"text": footer},
        }
        if body:
            embed["description"] = _cut(body, 2000)
        if post.image_url:
            embed["image"] = {"url": post.image_url}
        fields = []
        if post.poll_choices:
            choices = "\n".join(f"・{_cut(c, 80)}" for c in post.poll_choices[:10])
            fields.append({"name": "アンケート", "value": _cut(choices, 1024), "inline": False})
        if post.video_id:
            fields.append({"name": "添付動画", "value": f"https://youtu.be/{post.video_id}",
                           "inline": False})
        if post.shared:
            fields.append({"name": "種類", "value": "共有された投稿", "inline": True})
        if fields:
            embed["fields"] = fields
        kind = "post"
    if author := _author(meta):
        embed["author"] = author
    return {
        "content": _content(kind, mention),
        "embeds": [embed],
        "allowed_mentions": _allowed_mentions(mention),
    }


def apply_mention(payload: dict, mention: str | None) -> dict:
    """Copy of a payload rendered without a mention, with this destination's mention added."""
    result = dict(payload)
    if mention:
        result["content"] = f"{mention} {payload['content']}"
    result["allowed_mentions"] = _allowed_mentions(mention)
    return result
