"""List a profile's videos without downloading anything."""

from __future__ import annotations

import re

import yt_dlp

from .models import VideoItem
from .platforms import detect_platform
from .urlsafety import app_ydl, check_fetch_url, fetch_url_problem
from .ytdl import guarded_ydl


def _flatten(info: dict, default_platform: str) -> list[VideoItem]:
    """Walk a (possibly nested) playlist and return every leaf video.

    YouTube channel URLs return nested playlists (Videos tab, Shorts tab…),
    so this recurses into any entry that has entries of its own.
    """
    items: list[VideoItem] = []
    for entry in info.get("entries") or []:
        if not entry:  # yt-dlp yields None for entries it couldn't resolve
            continue
        if entry.get("entries") is not None:
            items.extend(_flatten(entry, default_platform))
            continue
        url = entry.get("webpage_url") or entry.get("url")
        if not url or not entry.get("id") or fetch_url_problem(url):
            continue                    # no link, or one Reelay wouldn't fetch later (not a platform link)
        platform = detect_platform(url, entry.get("ie_key") or entry.get("extractor_key"))
        if platform == "Unknown":
            platform = default_platform
        items.append(VideoItem.from_info(entry, platform, url))
    return items


INSTAGRAM_PROFILE_UNAVAILABLE = (
    "Instagram only lets apps list a whole profile through its official API. To list every Reel on your own "
    "profile, connect it under Accounts → Instagram → Advanced Setup (it needs a Professional account and a "
    "token from your own Meta app; the guide walks you through it). For any other account, open each reel on "
    "Instagram, copy its link (Share, then Copy link), and paste the links here, several at once separated by "
    "spaces. If Instagram asks you to sign in, set “Use browser sign-in” to a browser signed in to Instagram.")
_INSTAGRAM_POST = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:p|reels?|tv|stories)/", re.IGNORECASE)


class ListingUnavailable(Exception):
    """A plain-language reason a link can't be listed, found before any network request."""
    plain = True


def instagram_profile_listing_works() -> bool:
    """Whether yt-dlp's own Instagram profile extractor is marked working in this copy of yt-dlp."""
    try:
        from yt_dlp.extractor.instagram import InstagramUserIE
    except Exception:  # noqa: BLE001
        return False
    return bool(getattr(InstagramUserIE, "_WORKING", True))


def listing_problem(source_url: str) -> str | None:
    """Why this link can't be listed with this copy of yt-dlp (None when it can be tried)."""
    if "instagram.com/" in source_url.lower() and not _INSTAGRAM_POST.search(source_url) \
            and not instagram_profile_listing_works():
        return INSTAGRAM_PROFILE_UNAVAILABLE
    return None


def list_videos(source_url: str, ydl_opts: dict, limit: int | None = None) -> tuple[str, list[VideoItem]]:
    """Resolve a profile / playlist / single-video URL.

    Returns (display name of the source, videos newest-first). Uses flat
    extraction, so listing a 500-video profile is a handful of requests.
    """
    source_url = check_fetch_url(source_url)          # before any network activity
    problem = listing_problem(source_url)
    if problem:
        raise ListingUnavailable(problem)
    opts = {**ydl_opts, "extract_flat": "in_playlist"}
    if limit:
        opts["playlistend"] = limit
    with guarded_ydl(app_ydl(opts, source_url), yt_dlp.YoutubeDL) as ydl:
        info = ydl.extract_info(source_url, download=False)
    if not info:
        return source_url, []

    default_platform = detect_platform(source_url, info.get("extractor_key"))
    source_name = info.get("uploader") or info.get("channel") or info.get("title") or source_url
    if info.get("entries") is not None:
        items = _flatten(info, default_platform)
    else:
        items = [VideoItem.from_info(info, default_platform, source_url)]

    seen: set[str] = set()
    unique = [i for i in items if not (i.key in seen or seen.add(i.key))]
    return source_name, unique[:limit] if limit else unique


def load_details(item: VideoItem, ydl_opts: dict) -> VideoItem:
    """Fetch full metadata for one video (date, caption…). Returns a new item."""
    check_fetch_url(item.url)
    with guarded_ydl(app_ydl({**ydl_opts, "noplaylist": True}, item.url), yt_dlp.YoutubeDL) as ydl:
        info = ydl.extract_info(item.url, download=False, process=False)
    fresh = VideoItem(platform=item.platform, video_id=item.video_id, url=item.url)
    if info:
        fresh.update_from_info(info)
    return fresh
