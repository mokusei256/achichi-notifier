"""Fetching and parsing public YouTube pages (no API key required).

Sources used per check:
  * RSS feed           -> latest uploads incl. shorts (link contains /shorts/)
  * /videos, /shorts, /streams tabs -> fresher than RSS, also lists upcoming stream slots
  * /watch?v=ID        -> classification (upcoming / live / premiere / archive), schedule, publish date
  * /posts tab         -> community posts; members-only posts are visible only with member cookies
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

import requests

log = logging.getLogger(__name__)

BASE = "https://www.youtube.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
MEMBERS_MARKERS = ("MEMBERS_ONLY", "メンバー限定", "Members only", "Members-only")
ATOM_NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}

VIDEO_TABS = ("videos", "shorts", "streams")


class YouTubeError(Exception):
    pass


# --------------------------------------------------------------------------- data


@dataclass
class ChannelMeta:
    title: str | None = None
    avatar_url: str | None = None
    url: str | None = None


@dataclass
class VideoRef:
    """A video seen in a listing (RSS or channel tab)."""

    video_id: str
    sources: set[str] = field(default_factory=set)
    is_short: bool = False
    members_only: bool = False
    title: str | None = None
    published: datetime | None = None  # from RSS when available
    live_hint: str | None = None  # "upcoming" | "live" | None (from tab badges)

    def merge(self, other: VideoRef) -> None:
        self.sources |= other.sources
        self.is_short = self.is_short or other.is_short
        self.members_only = self.members_only or other.members_only
        self.title = self.title or other.title
        self.published = self.published or other.published
        self.live_hint = self.live_hint or other.live_hint


@dataclass
class VideoInfo:
    video_id: str
    kind: str  # video | short | premiere | live_upcoming | live_now | live_archive
    title: str
    url: str
    thumbnail: str
    members_only: bool = False
    published: datetime | None = None
    scheduled_start: datetime | None = None
    from_watch_page: bool = True


@dataclass
class Post:
    post_id: str
    text: str = ""
    image_url: str | None = None
    members_only: bool = False
    published_text: str | None = None
    poll_choices: list[str] = field(default_factory=list)
    video_id: str | None = None
    shared: bool = False

    @property
    def url(self) -> str:
        return f"{BASE}/post/{self.post_id}"


@dataclass
class PostsPage:
    posts: list[Post]
    logged_in: bool
    meta: ChannelMeta | None


# --------------------------------------------------------------------------- helpers


def _dig(obj: Any, *keys: Any) -> Any:
    for key in keys:
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and isinstance(key, int) and -len(obj) <= key < len(obj):
            obj = obj[key]
        else:
            return None
    return obj


def _walk(obj: Any) -> Iterator[dict]:
    """Pre-order traversal over every dict in a JSON tree (document order)."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(reversed(list(cur.values())))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))


def _extract_json_var(html: str, names: tuple[str, ...]) -> dict | None:
    decoder = json.JSONDecoder()
    for name in names:
        for match in re.finditer(re.escape(name) + r"\s*=\s*", html):
            start = match.end()
            if html.startswith("{", start):
                try:
                    obj, _ = decoder.raw_decode(html, start)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    return obj
    return None


def extract_initial_data(html: str) -> dict | None:
    return _extract_json_var(html, ("var ytInitialData", 'window["ytInitialData"]', "ytInitialData"))


def extract_player_response(html: str) -> dict | None:
    return _extract_json_var(html, ("var ytInitialPlayerResponse", "ytInitialPlayerResponse"))


def is_logged_in(html: str) -> bool:
    return re.search(r'"LOGGED_IN"\s*:\s*true', html) is not None


def _unwrap_redirect(url: str) -> str:
    if url.startswith("/"):
        url = BASE + url
    parsed = urlparse(url)
    if parsed.path == "/redirect":
        target = parse_qs(parsed.query).get("q")
        if target:
            return target[0]
    return url


def text_of(obj: Any) -> str:
    """Flatten a YouTube text object ({simpleText} / {runs} / {content})."""
    if not obj:
        return ""
    if isinstance(obj, str):
        return obj
    if "simpleText" in obj:
        return obj["simpleText"]
    if "content" in obj and isinstance(obj["content"], str):
        return obj["content"]
    parts = []
    for run in obj.get("runs", []):
        text = run.get("text", "")
        url = _dig(run, "navigationEndpoint", "urlEndpoint", "url")
        if url and text.rstrip().endswith(("...", "…")):
            text = _unwrap_redirect(url)  # display text is truncated; use the real link
        parts.append(text)
    return "".join(parts)


def _https(url: str | None) -> str | None:
    if not url:
        return None
    return "https:" + url if url.startswith("//") else url


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _has_members_marker(*objs: Any) -> bool:
    blob = json.dumps([o for o in objs if o], ensure_ascii=False)
    return any(marker in blob for marker in MEMBERS_MARKERS)


_AGE_RE = re.compile(
    r"(\d+)\s*(秒|分|時間|日|週間|週|か月|ヶ月|ヵ月|カ月|年|"
    r"seconds?|minutes?|hours?|days?|weeks?|months?|years?)",
    re.IGNORECASE,
)
_AGE_HOURS = {
    "秒": 1 / 3600, "分": 1 / 60, "時間": 1, "日": 24, "週間": 168, "週": 168,
    "か月": 720, "ヶ月": 720, "ヵ月": 720, "カ月": 720, "年": 8760,
    "second": 1 / 3600, "minute": 1 / 60, "hour": 1, "day": 24, "week": 168,
    "month": 720, "year": 8760,
}


def approx_age_hours(text: str | None) -> float | None:
    """'5 時間前' -> 5.0, '2 days ago' -> 48.0. None when unparseable."""
    if not text:
        return None
    match = _AGE_RE.search(text)
    if not match:
        return None
    unit = match.group(2).lower().rstrip("s") if match.group(2).isascii() else match.group(2)
    return int(match.group(1)) * _AGE_HOURS[unit]


# --------------------------------------------------------------------------- parsers


def _selected_tab_content(data: dict) -> Any:
    for tab in _dig(data, "contents", "twoColumnBrowseResultsRenderer", "tabs") or []:
        renderer = tab.get("tabRenderer") or tab.get("expandableTabRenderer") or {}
        if renderer.get("selected"):
            return renderer.get("content", {})
    return data.get("contents", {})


def parse_channel_meta(data: dict) -> ChannelMeta | None:
    meta = _dig(data, "metadata", "channelMetadataRenderer")
    if not meta:
        return None
    return ChannelMeta(
        title=meta.get("title"),
        avatar_url=_https(_dig(meta, "avatar", "thumbnails", 0, "url")),
        url=meta.get("vanityChannelUrl") or meta.get("channelUrl"),
    )


def _lockup_live_hint(lockup: dict) -> str | None:
    badges = []
    for node in _walk(lockup.get("contentImage")):
        badge = node.get("thumbnailBadgeViewModel")
        if isinstance(badge, dict):
            badges.append((badge.get("text") or "", badge.get("badgeStyle") or ""))
    rows = _dig(lockup, "metadata", "lockupMetadataViewModel", "metadata",
                "contentMetadataViewModel", "metadataRows") or []
    meta_texts = [
        _dig(part, "text", "content") or ""
        for row in rows for part in row.get("metadataParts", [])
    ]
    if any("LIVE" in style for _, style in badges) or any(
        t in ("ライブ", "LIVE") for t, _ in badges
    ) or any("視聴中" in t or "watching" in t for t in meta_texts):
        return "live"
    if any(t in ("配信予定", "プレミア公開", "Upcoming", "Premiere") for t, _ in badges) or any(
        "公開予定" in t or "待機中" in t or "Scheduled" in t or "waiting" in t for t in meta_texts
    ):
        return "upcoming"
    return None


def parse_tab_videos(data: dict, source: str) -> list[VideoRef]:
    refs: list[VideoRef] = []
    seen: set[str] = set()
    for node in _walk(_selected_tab_content(data)):
        video_id = title = live_hint = None
        is_short = source == "shorts"
        members = False
        if isinstance(lockup := node.get("lockupViewModel"), dict):
            if lockup.get("contentType") not in (None, "LOCKUP_CONTENT_TYPE_VIDEO"):
                continue
            video_id = lockup.get("contentId")
            title = _dig(lockup, "metadata", "lockupMetadataViewModel", "title", "content")
            members = _has_members_marker(
                lockup.get("contentImage"),
                _dig(lockup, "metadata", "lockupMetadataViewModel", "metadata"),
            )
            live_hint = _lockup_live_hint(lockup)
        elif isinstance(shorts := node.get("shortsLockupViewModel"), dict):
            video_id = _dig(shorts, "onTap", "innertubeCommand", "reelWatchEndpoint", "videoId")
            if not video_id:
                video_id = (shorts.get("entityId") or "").removeprefix("shorts-shelf-item-")
            title = _dig(shorts, "overlayMetadata", "primaryText", "content")
            is_short = True
        else:
            for key in ("videoRenderer", "gridVideoRenderer", "reelItemRenderer"):
                renderer = node.get(key)
                if isinstance(renderer, dict) and renderer.get("videoId"):
                    video_id = renderer["videoId"]
                    title = text_of(renderer.get("title") or renderer.get("headline"))
                    members = _has_members_marker(renderer.get("badges"))
                    is_short = is_short or key == "reelItemRenderer"
                    if renderer.get("upcomingEventData"):
                        live_hint = "upcoming"
                    break
        if not video_id or video_id in seen or not VIDEO_ID_RE.fullmatch(video_id):
            continue
        seen.add(video_id)
        refs.append(VideoRef(video_id, {source}, is_short, members, title or None, None, live_hint))
    return refs


def parse_rss(xml_text: str) -> tuple[list[VideoRef], str | None]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise YouTubeError(f"RSS parse error: {exc}") from exc
    refs = []
    for entry in root.findall("a:entry", ATOM_NS):
        video_id = entry.findtext("yt:videoId", namespaces=ATOM_NS)
        if not video_id:
            continue
        link = entry.find("a:link", ATOM_NS)
        href = link.get("href", "") if link is not None else ""
        refs.append(VideoRef(
            video_id=video_id,
            sources={"rss"},
            is_short="/shorts/" in href,
            title=entry.findtext("a:title", namespaces=ATOM_NS),
            published=_parse_iso(entry.findtext("a:published", namespaces=ATOM_NS)),
        ))
    return refs, root.findtext("a:title", namespaces=ATOM_NS)


def _members_only_playability(status: dict) -> bool:
    if not status or status.get("status") in (None, "OK"):
        return False
    blob = json.dumps(status, ensure_ascii=False)
    return "メンバー" in blob or "members" in blob.lower()


def classify_video(player: dict | None, ref: VideoRef) -> VideoInfo:
    """Turn a watch-page player response (or, failing that, listing hints) into VideoInfo."""
    video_id = ref.video_id
    player = player or {}
    details = player.get("videoDetails") or {}
    micro = _dig(player, "microformat", "playerMicroformatRenderer") or {}
    status = player.get("playabilityStatus") or {}
    broadcast = micro.get("liveBroadcastDetails") or {}
    usable = bool(details or micro)

    if usable:
        is_live_content = details["isLiveContent"] if "isLiveContent" in details else bool(broadcast)
        upcoming = bool(details.get("isUpcoming")) or status.get("status") == "LIVE_STREAM_OFFLINE"
        live_now = bool(broadcast.get("isLiveNow")) or bool(details.get("isLive"))
        if upcoming:
            kind = "live_upcoming" if is_live_content else "premiere"
        elif live_now:
            kind = "live_now" if is_live_content else "premiere"
        elif is_live_content:
            kind = "live_archive"
        else:
            kind = "short" if ref.is_short else "video"
    else:
        # Watch page unavailable (bot check, network...). Fall back to listing hints.
        if ref.live_hint == "upcoming":
            kind = "live_upcoming"
        elif ref.live_hint == "live":
            kind = "live_now"
        elif "streams" in ref.sources:
            kind = "live_archive"
        else:
            kind = "short" if ref.is_short else "video"

    scheduled = None
    if kind in ("live_upcoming", "premiere"):
        unix = _dig(status, "liveStreamability", "liveStreamabilityRenderer", "offlineSlate",
                    "liveStreamOfflineSlateRenderer", "scheduledStartTime")
        if unix and str(unix).isdigit():
            scheduled = datetime.fromtimestamp(int(unix), tz=timezone.utc)
        else:
            scheduled = _parse_iso(broadcast.get("startTimestamp"))

    thumbs = (details.get("thumbnail") or micro.get("thumbnail") or {}).get("thumbnails") or []
    jpgs = [t.get("url") for t in thumbs if t.get("url") and "webp" not in t["url"]]
    thumbnail = _https(jpgs[-1]) if jpgs else None
    url = f"{BASE}/shorts/{video_id}" if kind == "short" else f"{BASE}/watch?v={video_id}"

    return VideoInfo(
        video_id=video_id,
        kind=kind,
        title=details.get("title") or text_of(micro.get("title")) or ref.title or video_id,
        url=url,
        thumbnail=thumbnail or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        members_only=ref.members_only or _members_only_playability(status),
        published=_parse_iso(micro.get("publishDate") or micro.get("uploadDate")) or ref.published,
        scheduled_start=scheduled,
        from_watch_page=usable,
    )


def _parse_post_renderer(post: dict, shared: bool) -> Post | None:
    post_id = post.get("postId")
    if not post_id:
        return None
    members_only = any(
        value and key.lower().startswith(("sponsorsonly", "membersonly"))
        for key, value in post.items()
    )
    text = text_of(post.get("contentText") or post.get("content"))
    image = poll = video_id = None
    attachment = post.get("backstageAttachment") or {}
    if img := attachment.get("backstageImageRenderer"):
        image = _dig(img, "image", "thumbnails", -1, "url")
    elif multi := attachment.get("postMultiImageRenderer"):
        image = _dig(multi, "images", 0, "backstageImageRenderer", "image", "thumbnails", -1, "url")
    elif poll_r := attachment.get("pollRenderer"):
        poll = [text_of(c.get("text")) for c in poll_r.get("choices", [])]
    elif video := attachment.get("videoRenderer"):
        video_id = video.get("videoId")
    return Post(
        post_id=post_id,
        text=text,
        image_url=_https(image),
        members_only=members_only,
        published_text=text_of(post.get("publishedTimeText")) or None,
        poll_choices=poll or [],
        video_id=video_id,
        shared=shared,
    )


def parse_posts(data: dict) -> list[Post]:
    posts: list[Post] = []
    seen: set[str] = set()
    content = _selected_tab_content(data)
    threads = [n["backstagePostThreadRenderer"] for n in _walk(content)
               if isinstance(n.get("backstagePostThreadRenderer"), dict)]
    candidates: list[tuple[dict, bool]] = []
    if threads:
        for thread in threads:
            wrapper = thread.get("post") or {}
            if isinstance(r := wrapper.get("backstagePostRenderer"), dict):
                candidates.append((r, False))
            elif isinstance(r := wrapper.get("sharedPostRenderer"), dict):
                candidates.append((r, True))
    else:  # unknown layout: take top-level post renderers only
        for node in _walk(content):
            if isinstance(r := node.get("backstagePostRenderer"), dict):
                candidates.append((r, False))
    for renderer, shared in candidates:
        post = _parse_post_renderer(renderer, shared)
        if post and post.post_id not in seen:
            seen.add(post.post_id)
            posts.append(post)
    return posts


# --------------------------------------------------------------------------- client


def _load_cookie_jar(path: Path) -> http.cookiejar.MozillaCookieJar:
    jar = http.cookiejar.MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError) as exc:
        raise YouTubeError(f"cannot read cookies file ({exc.__class__.__name__})") from exc
    return jar


class YouTubeClient:
    def __init__(self, channel_id: str, cookies_file: Path | None = None, timeout: float = 20.0):
        self.channel_id = channel_id
        self.timeout = timeout
        self.session = self._new_session()
        self.session.cookies.set("SOCS", "CAI", domain=".youtube.com")  # skip consent wall
        self.member_session: requests.Session | None = None
        if cookies_file:
            self.member_session = self._new_session()
            self.member_session.cookies = _load_cookie_jar(cookies_file)

    @staticmethod
    def _new_session() -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.5",
        })
        return session

    @property
    def has_cookies(self) -> bool:
        return self.member_session is not None

    def _get(self, url: str, session: requests.Session | None = None, **params: str) -> str:
        try:
            resp = (session or self.session).get(url, params=params or None, timeout=self.timeout)
        except requests.RequestException as exc:
            raise YouTubeError(f"GET {url} failed: {exc.__class__.__name__}") from exc
        if resp.status_code != 200:
            raise YouTubeError(f"GET {url} -> HTTP {resp.status_code}")
        return resp.text

    def _tab_url(self, tab: str) -> str:
        return f"{BASE}/channel/{self.channel_id}/{tab}"

    def fetch_rss(self) -> tuple[list[VideoRef], str | None]:
        xml_text = self._get(f"{BASE}/feeds/videos.xml", channel_id=self.channel_id)
        return parse_rss(xml_text)

    def fetch_tab(self, tab: str) -> tuple[list[VideoRef], ChannelMeta | None]:
        html = self._get(self._tab_url(tab), hl="ja", gl="JP")
        data = extract_initial_data(html)
        if data is None:
            raise YouTubeError(f"ytInitialData not found on /{tab}")
        return parse_tab_videos(data, tab), parse_channel_meta(data)

    def fetch_video_info(self, ref: VideoRef) -> VideoInfo:
        try:
            html = self._get(f"{BASE}/watch", v=ref.video_id, hl="ja", gl="JP")
            player = extract_player_response(html)
        except YouTubeError as exc:
            log.warning("watch page for %s unavailable (%s); using listing hints", ref.video_id, exc)
            player = None
        if player:
            status = player.get("playabilityStatus") or {}
            if status.get("status") == "LOGIN_REQUIRED" and not player.get("videoDetails"):
                log.warning("watch page for %s asks for sign-in (bot check?)", ref.video_id)
        return classify_video(player, ref)

    def fetch_posts(self) -> PostsPage:
        session = self.member_session or self.session
        html = self._get(self._tab_url("posts"), session=session, hl="ja", gl="JP")
        data = extract_initial_data(html)
        if data is None:
            raise YouTubeError("ytInitialData not found on /posts")
        return PostsPage(parse_posts(data), is_logged_in(html), parse_channel_meta(data))
