from datetime import datetime, timedelta, timezone

from achichi_notifier import render
from achichi_notifier.state import State
from achichi_notifier.youtube import (
    Post, VideoRef, approx_age_hours, classify_video, extract_initial_data, parse_posts,
    parse_rss, parse_tab_videos, text_of,
)


def _tab_data(items):
    return {"contents": {"twoColumnBrowseResultsRenderer": {"tabs": [
        {"tabRenderer": {"title": "home", "content": {"x": [{"videoRenderer": {"videoId": "AAAAAAAAAAA"}}]}}},
        {"tabRenderer": {"selected": True, "content": {"richGridRenderer": {"contents": items}}}},
    ]}}}


def _lockup(video_id, badge_text="12:34", badge_style="THUMBNAIL_OVERLAY_BADGE_STYLE_DEFAULT", meta=()):
    return {"richItemRenderer": {"content": {"lockupViewModel": {
        "contentId": video_id,
        "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
        "contentImage": {"thumbnailViewModel": {"overlays": [{"thumbnailBottomOverlayViewModel": {
            "badges": [{"thumbnailBadgeViewModel": {"text": badge_text, "badgeStyle": badge_style}}]}}]}},
        "metadata": {"lockupMetadataViewModel": {
            "title": {"content": f"title {video_id}"},
            "metadata": {"contentMetadataViewModel": {"metadataRows": [
                {"metadataParts": [{"text": {"content": m}} for m in meta]}]}},
        }},
    }}}}


def test_extract_initial_data_handles_braces_in_strings():
    html = '<script>var ytInitialData = {"a": "};</script>", "b": [1, {"c": 2}]};</script>'
    assert extract_initial_data(html) == {"a": "};</script>", "b": [1, {"c": 2}]}


def test_parse_tab_videos_only_selected_tab_and_hints():
    data = _tab_data([
        _lockup("upcoming001", "配信予定", meta=("3 人が待機中", "2028/09/24 3:00 に公開予定")),
        _lockup("liveNow0001", "ライブ", "THUMBNAIL_OVERLAY_BADGE_STYLE_LIVE"),
        _lockup("members0001", meta=("メンバー限定",)),
        _lockup("normal00001"),
    ])
    refs = {r.video_id: r for r in parse_tab_videos(data, "streams")}
    assert list(refs) == ["upcoming001", "liveNow0001", "members0001", "normal00001"]
    assert "AAAAAAAAAAA" not in refs  # from a non-selected tab
    assert refs["upcoming001"].live_hint == "upcoming"
    assert refs["liveNow0001"].live_hint == "live"
    assert refs["members0001"].members_only and not refs["normal00001"].members_only


def test_parse_tab_shorts():
    data = _tab_data([{"richItemRenderer": {"content": {"shortsLockupViewModel": {
        "entityId": "shorts-shelf-item-VNRJ-v9tiGw",
        "overlayMetadata": {"primaryText": {"content": "short title"}},
    }}}}])
    (ref,) = parse_tab_videos(data, "shorts")
    assert ref.video_id == "VNRJ-v9tiGw" and ref.is_short and ref.title == "short title"


def test_parse_rss_detects_shorts():
    xml = """<?xml version="1.0"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
 <title>Mela Ch.</title>
 <entry><yt:videoId>VNRJ-v9tiGw</yt:videoId><title>s</title>
  <link rel="alternate" href="https://www.youtube.com/shorts/VNRJ-v9tiGw"/>
  <published>2026-11-15T06:00:03+00:00</published></entry>
 <entry><yt:videoId>YL-6y3sZnbs</yt:videoId><title>v</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=YL-6y3sZnbs"/>
  <published>2026-11-15T12:23:01+00:00</published></entry>
</feed>"""
    refs, title = parse_rss(xml)
    assert title == "Mela Ch."
    assert [(r.video_id, r.is_short) for r in refs] == [("VNRJ-v9tiGw", True), ("YL-6y3sZnbs", False)]
    assert refs[0].published == datetime(2026, 11, 15, 6, 0, 3, tzinfo=timezone.utc)


def _player(**details):
    micro = details.pop("micro", {})
    status = details.pop("status", {"status": "OK"})
    return {"videoDetails": {"title": "t", **details}, "playabilityStatus": status,
            "microformat": {"playerMicroformatRenderer": {"publishDate": "2026-11-15T04:23:01-08:00", **micro}}}


def test_classify_upcoming_stream_with_schedule():
    player = _player(isLiveContent=True, isUpcoming=True, status={
        "status": "LIVE_STREAM_OFFLINE",
        "liveStreamability": {"liveStreamabilityRenderer": {"offlineSlate": {
            "liveStreamOfflineSlateRenderer": {"scheduledStartTime": "1853402400"}}}}})
    info = classify_video(player, VideoRef("4uuYjSf0rpU"))
    assert info.kind == "live_upcoming"
    assert info.scheduled_start == datetime.fromtimestamp(1853402400, tz=timezone.utc)


def test_classify_variants():
    assert classify_video(_player(isLiveContent=False), VideoRef("x" * 11, is_short=True)).kind == "short"
    assert classify_video(_player(isLiveContent=False), VideoRef("x" * 11)).kind == "video"
    assert classify_video(_player(isLiveContent=False, isUpcoming=True), VideoRef("x" * 11)).kind == "premiere"
    live = _player(isLiveContent=True, micro={"liveBroadcastDetails": {"isLiveNow": True}})
    assert classify_video(live, VideoRef("x" * 11)).kind == "live_now"
    assert classify_video(_player(isLiveContent=True), VideoRef("x" * 11)).kind == "live_archive"


def test_classify_keeps_the_stream_times_used_for_the_age_check():
    # Shape of QclGfuKx8XI's microformat on 2026-11-17: slot created 11-08, stream on 11-18.
    upcoming = _player(isLiveContent=True, isUpcoming=True, micro={
        "publishDate": "2026-11-07T22:05:23-08:00",
        "liveBroadcastDetails": {"isLiveNow": False, "startTimestamp": "2026-11-17T22:00:00+00:00"}})
    info = classify_video(upcoming, VideoRef("QclGfuKx8XI"))
    assert info.kind == "live_upcoming" and info.ended is None
    assert info.published == datetime(2026, 11, 8, 6, 5, 23, tzinfo=timezone.utc)
    assert info.scheduled_start == datetime(2026, 11, 17, 22, 0, tzinfo=timezone.utc)
    archive = _player(isLiveContent=True, micro={"liveBroadcastDetails": {
        "isLiveNow": False, "startTimestamp": "2026-11-17T22:00:00+00:00",
        "endTimestamp": "2026-11-17T23:31:05+00:00"}})
    info = classify_video(archive, VideoRef("QclGfuKx8XI"))
    assert info.kind == "live_archive"
    assert info.ended == datetime(2026, 11, 17, 23, 31, 5, tzinfo=timezone.utc)


def test_classify_falls_back_to_listing_hints_when_watch_page_blocked():
    blocked = {"playabilityStatus": {"status": "LOGIN_REQUIRED", "reason": "Sign in to confirm you're not a bot"}}
    ref = VideoRef("abcdefghijk", sources={"streams"}, live_hint="upcoming", title="枠")
    info = classify_video(blocked, ref)
    assert info.kind == "live_upcoming" and info.title == "枠" and not info.from_watch_page
    assert info.thumbnail == "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg"


def test_classify_members_only_from_playability():
    player = {"playabilityStatus": {"status": "UNPLAYABLE", "reason": "この動画はメンバー限定です"},
              "microformat": {"playerMicroformatRenderer": {"title": {"simpleText": "限定"}}}}
    info = classify_video(player, VideoRef("abcdefghijk"))
    assert info.members_only and info.title == "限定"


def _posts_data(*renderers):
    return _tab_data([{"backstagePostThreadRenderer": {"post": r}} for r in renderers])


def test_parse_posts_public_member_and_shared():
    public = {"backstagePostRenderer": {
        "postId": "UgkxPUBLIC",
        "contentText": {"runs": [
            {"text": "告知なのじゃ\n"},
            {"text": "https://shop.example.com/pro...", "navigationEndpoint": {"urlEndpoint": {
                "url": "https://www.youtube.com/redirect?event=x&q=https%3A%2F%2Fshop.example.com%2Fproducts%2F1"}}},
        ]},
        "publishedTimeText": {"runs": [{"text": "5 時間前"}]},
        "backstageAttachment": {"backstageImageRenderer": {"image": {"thumbnails": [
            {"url": "//yt3.ggpht.com/small"}, {"url": "//yt3.ggpht.com/large"}]}}},
    }}
    member = {"backstagePostRenderer": {
        "postId": "UgkxMEMBER",
        "contentText": {"runs": [{"text": "ひみつの日記"}]},
        "sponsorsOnlyBadge": {"sponsorsOnlyBadgeRenderer": {"label": {"simpleText": "メンバー限定"}}},
        "publishedTimeText": {"runs": [{"text": "1 日前"}]},
        "backstageAttachment": {"backstageImageRenderer": {"image": {"thumbnails": [{"url": "https://img/secret"}]}}},
    }}
    shared = {"sharedPostRenderer": {
        "postId": "UgkxSHARED", "content": {"runs": [{"text": "共有"}]},
        "originalPost": {"backstagePostRenderer": {"postId": "UgkxORIGINAL"}},
    }}
    posts = {p.post_id: p for p in parse_posts(_posts_data(public, member, shared))}
    assert list(posts) == ["UgkxPUBLIC", "UgkxMEMBER", "UgkxSHARED"]  # nested original skipped
    assert posts["UgkxPUBLIC"].text == "告知なのじゃ\nhttps://shop.example.com/products/1"
    assert posts["UgkxPUBLIC"].image_url == "https://yt3.ggpht.com/large"
    assert not posts["UgkxPUBLIC"].members_only
    assert posts["UgkxMEMBER"].members_only
    assert posts["UgkxSHARED"].shared


def test_member_post_payload_hides_everything():
    post = Post("UgkxMEMBER", text="ひみつの日記", image_url="https://img/secret",
                members_only=True, published_text="1 日前", poll_choices=["a"], video_id="abcdefghijk")
    payload = render.post_payload(post, None, "<@&1>")
    blob = repr(payload)
    for secret in ("ひみつ", "https://img/secret", "abcdefghijk"):
        assert secret not in blob
    assert payload["content"].startswith("<@&1> 🔒")
    assert payload["embeds"][0]["url"] == "https://www.youtube.com/post/UgkxMEMBER"


def test_public_post_payload():
    post = Post("UgkxPUBLIC", text="タイトル行\n本文です", image_url="https://img/a", published_text="5 時間前")
    embed = render.post_payload(post, None, None)["embeds"][0]
    assert embed["title"] == "タイトル行" and embed["description"] == "本文です"
    assert embed["image"]["url"] == "https://img/a"


def test_approx_age_hours():
    assert approx_age_hours("5 時間前") == 5
    assert approx_age_hours("2 日前（編集済み）") == 48
    assert approx_age_hours("3 weeks ago") == 504
    assert approx_age_hours("30 分前") == 0.5
    assert approx_age_hours("たった今") is None


def test_text_of_variants():
    assert text_of({"simpleText": "a"}) == "a"
    assert text_of({"runs": [{"text": "a"}, {"text": "b"}]}) == "ab"
    assert text_of(None) == ""


def test_state_merge_and_prune():
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    a = State(videos={"v1": "2026-11-01T00:00:00+00:00"},
              runners={"primary": {"last_success": now.isoformat()}})
    b = State(videos={"v2": "2026-11-02T00:00:00+00:00"}, posts={"p1": "x"},
              runners={"primary": {"last_success": (now - timedelta(hours=1)).isoformat()},
                       "fallback": {"last_run": now.isoformat()}})
    a.merge_from(b)
    assert set(a.videos) == {"v1", "v2"} and set(a.posts) == {"p1"}
    assert a.runners["primary"]["last_success"] == now.isoformat()
    assert "fallback" in a.runners
    a.prune(limit=1)
    assert list(a.videos) == ["v2"]


def test_parse_posts_post_renderer_shared_member_original_and_locked_teaser():
    from achichi_notifier.youtube import parse_posts_detailed
    data = _tab_data([
        {"backstagePostThreadRenderer": {"post": {"postRenderer": {
            "postId": "UgkxPOSTRENDERER", "contentText": {"runs": [{"text": "x"}]},
            "sponsorsOnlyBadge": {"sponsorsOnlyBadgeRenderer": {"label": {"simpleText": "メンバー限定"}}}}}}},
        {"backstagePostThreadRenderer": {"post": {"sharedPostRenderer": {
            "postId": "UgkxSHAREDMEM", "content": {"runs": [{"text": "共有コメント"}]},
            "originalPost": {"backstagePostRenderer": {
                "postId": "UgkxORIGINALMEM", "contentText": {"runs": [{"text": "ひみつ"}]},
                "sponsorsOnlyBadge": {"sponsorsOnlyBadgeRenderer": {}}}}}}}},
        {"backstageLockedPostViewModel": {
            "publishedTimeText": {"content": "1 時間前"},
            "commentButton": {"buttonViewModel": {"onTap": {"innertubeCommand": {
                "commandMetadata": {"webCommandMetadata": {"url": "/post/UgkxLOCKEDTEASER1"}}}}}}}},
        {"backstageLockedPostViewModel": {"lockedText": {"content": "メンバー限定"}}},
    ])
    posts, locked_without_id = parse_posts_detailed(data)
    by_id = {p.post_id: p for p in posts}
    assert set(by_id) == {"UgkxPOSTRENDERER", "UgkxSHAREDMEM", "UgkxLOCKEDTEASER1"}
    assert all(p.members_only for p in posts)
    assert locked_without_id == 1
    payload = render.post_payload(by_id["UgkxSHAREDMEM"], None, None)
    assert "共有コメント" not in repr(payload) and "ひみつ" not in repr(payload)


def test_membership_status_and_posts_section():
    from achichi_notifier.youtube import membership_status, posts_section_present

    def header(title):
        return {"header": {"pageHeaderRenderer": {"content": {"buttons": [{"buttonViewModel": {
            "title": title, "iconName": "SPONSORSHIP_STAR"}}]}}}}

    assert membership_status(header("メンバー特典")) == "member"
    assert membership_status(header("メンバーになる")) == "not_member"
    assert membership_status({"header": {}}) == "unknown"
    assert posts_section_present(_tab_data([{"continuationItemRenderer": {}}]))
    assert not posts_section_present(_tab_data([{"messageRenderer": {}}]))
