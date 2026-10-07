"""List a profile's videos without downloading anything."""

from __future__ import annotations

import yt_dlp

from .models import VideoItem
from .platforms import detect_platform


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
        if not url or not entry.get("id"):
            continue
        platform = detect_platform(url, entry.get("ie_key") or entry.get("extractor_key"))
        if platform == "Unknown":
            platform = default_platform
        items.append(VideoItem.from_info(entry, platform, url))
    return items


def list_videos(source_url: str, ydl_opts: dict, limit: int | None = None) -> tuple[str, list[VideoItem]]:
    """Resolve a profile / playlist / single-video URL.

    Returns (display name of the source, videos newest-first). Uses flat
    extraction, so listing a 500-video profile is a handful of requests.
    """
    opts = {**ydl_opts, "extract_flat": "in_playlist"}
    if limit:
        opts["playlistend"] = limit
    with yt_dlp.YoutubeDL(opts) as ydl:
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
    with yt_dlp.YoutubeDL({**ydl_opts, "noplaylist": True}) as ydl:
        info = ydl.extract_info(item.url, download=False, process=False)
    fresh = VideoItem(platform=item.platform, video_id=item.video_id, url=item.url)
    if info:
        fresh.update_from_info(info)
    return fresh
