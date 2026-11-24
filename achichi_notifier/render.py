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
    "collab_live": "🔴 配信が始まりました",  # a `watch` stream on another channel
    # A thumbnail of this channel's stream (slot or archive), video or short was changed.
    "thumb_live": "🖼️ サムネイルが変更されました",
    "thumb_video": "🖼️ サムネイルが変更されました",
    "thumb_short": "🖼️ サムネイルが変更されました",
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
    "post": "post", "member_post": "post", "collab_live": "live",
    "thumb_live": "live", "thumb_video": "video", "thumb_short": "short",
}
# Notification kinds a destination takes or leaves on their own (destinations.KIND_CHOICES);
# every other kind goes by its GROUPS entry.
DEST_KINDS = {"member_post": "member_post", "collab_live": "collab",
              "thumb_live": "thumbnail", "thumb_video": "thumbnail", "thumb_short": "thumbnail"}
THUMB_COLOR = 0xF1C40F


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
    # A stream that is on air shows when it began (its publish date is when the slot was made).
    stamp = info.started if kind == "live_now" and info.started else info.published
    if stamp:
        embed["timestamp"] = stamp.isoformat()
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


def thumb_payload(kind: str, title: str, url: str, label: str, meta: ChannelMeta | None,
                  changed: datetime, after: str, before: str | None) -> dict:
    """A changed thumbnail: the old and the new image side by side (two embeds with the same
    link make one embed with both images), or the new one alone when the old one is unknown.
    `after` / `before` are image URLs (attachment://... for uploaded files)."""
    embed: dict = {
        "title": _cut(title, 256),
        "url": url,
        "color": THUMB_COLOR,
        "description": "左が変更前、右が変更後です。" if before else "変更後のサムネイルです（変更前の画像はありません）。",
        "image": {"url": before or after},
        "footer": {"text": f"YouTube・サムネイル変更・{label}"},
        "timestamp": changed.isoformat(),
    }
    if author := _author(meta):
        embed["author"] = author
    embeds = [embed, {"url": url, "image": {"url": after}}] if before else [embed]
    return {"content": _content(kind, None), "embeds": embeds, "allowed_mentions": _allowed_mentions(None)}


def apply_mention(payload: dict, mention: str | None) -> dict:
    """Copy of a payload rendered without a mention, with this destination's mention added."""
    result = dict(payload)
    if mention:
        result["content"] = f"{mention} {payload['content']}"
    result["allowed_mentions"] = _allowed_mentions(mention)
    return result
