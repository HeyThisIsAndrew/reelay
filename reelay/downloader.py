"""Download a single video with quality rules, progress reporting and cancel."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

import yt_dlp
from yt_dlp.utils import DownloadCancelled, DownloadError

from .models import Status, VideoItem
from .urlsafety import app_ydl, fetch_url_problem, inside
from .ytdl import guarded_ydl

QUALITY_BEST = "best"
QUALITY_720 = "720p"
QUALITY_LABELS = {
    QUALITY_BEST: "Highest quality (best video + best audio)",
    QUALITY_720: "Standard 720p (smaller files)",
}

# Formats tagged "watermark" by yt-dlp's TikTok extractor are excluded. The
# `?` keeps formats with no format_note at all (most non-TikTok formats).
_NO_WM = "[format_note!*=?watermark]"

# Separate best video + best audio, merged (YouTube / Instagram serve them apart).
_GENERAL_SELECTOR = f"bv*{_NO_WM}+ba/b{_NO_WM}"

# TikTok always serves video and audio together, but also exposes the post's
# "sound" (music.playUrl) as an audio-only format. Merging that in would
# replace the creator's real audio with the soundtrack, so TikTok only ever
# takes a single pre-muxed, watermark-free stream.
_TIKTOK_SELECTOR = f"b[vcodec!=none][acodec!=none]{_NO_WM}"


class UserCancelled(DownloadCancelled):
    msg = "Cancelled"


@dataclass
class Progress:
    percent: float | None = None   # 0-100 for the current file
    speed: float | None = None     # bytes / second
    eta: int | None = None         # seconds
    stage: str = "Downloading"     # Downloading | Finalizing


def format_options(platform: str, quality: str) -> dict:
    """Format selector + sort order for a platform and quality preset."""
    # At equal resolution prefer H.264/AAC: it opens in every editor and
    # uploads cleanly to YouTube. "res:720" = largest version up to 720p.
    sort = ["res:720" if quality == QUALITY_720 else "res", "fps", "vcodec:h264", "acodec:aac"]
    selector = _TIKTOK_SELECTOR if platform == "TikTok" else _GENERAL_SELECTOR
    return {"format": selector, "format_sort": sort}


def _final_filepath(info: dict) -> Path | None:
    for dl in info.get("requested_downloads") or []:
        if dl.get("filepath"):
            return Path(dl["filepath"])
    return Path(info["filepath"]) if info.get("filepath") else None


def _is_watermark_free(info: dict) -> bool:
    formats = info.get("requested_formats") or [info]
    return not any("watermark" in (f.get("format_note") or "").lower() for f in formats)


def _unique_stem(folder: Path, stem: str, ext: str = "mp4") -> str:
    candidate, n = stem, 2
    while (folder / f"{candidate}.{ext}").exists():
        candidate = f"{stem}_{n}"
        n += 1
    return candidate


def download_item(
    item: VideoItem,
    folder: Path,
    quality: str,
    base_opts: dict,
    name_for: Callable[[VideoItem], str],
    on_progress: Callable[[Progress], None] | None = None,
    cancel: threading.Event | None = None,
) -> VideoItem:
    """Download one video into `folder`. Returns an updated copy of the item.

    `name_for(item)` returns the filename stem; it's called after fresh
    metadata is fetched so templates using {date} etc. are always accurate.
    Raises DownloadError / UserCancelled on failure.
    """
    result = replace(item)
    cancel = cancel or threading.Event()

    def progress_hook(d: dict) -> None:
        if cancel.is_set():
            raise UserCancelled()
        if not on_progress:
            return
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100) if total else None
            on_progress(Progress(pct, d.get("speed"), d.get("eta")))
        elif d.get("status") == "finished":
            on_progress(Progress(100.0, None, 0))

    def pp_hook(d: dict) -> None:
        if on_progress and d.get("status") == "started":
            on_progress(Progress(100.0, None, 0, stage="Finalizing"))

    common = {
        **base_opts,
        **format_options(item.platform, quality),
        "noplaylist": True,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
    }

    # 1) Fresh metadata (signed media URLs expire, and the date may be unknown).
    problem = fetch_url_problem(item.url)              # before any network activity
    if problem:
        raise DownloadError(problem)
    common = app_ydl(common, item.url)
    with guarded_ydl(common, yt_dlp.YoutubeDL) as ydl:
        info = ydl.extract_info(item.url, download=False)
    if not info:
        raise DownloadError("The platform returned no information for this video.")
    if cancel.is_set():
        raise UserCancelled()
    result.update_from_info(info)

    # 2) Download under the chosen name.
    stem = _unique_stem(folder, name_for(result))
    dl_opts = {
        **common,
        "paths": {"home": str(folder), "temp": str(folder / ".partial")},
        "outtmpl": {"default": stem.replace("%", "%%") + ".%(ext)s"},
        "merge_output_format": "mp4",
        "overwrites": False,
        # If the best stream arrives as WebM/MKV, rewrap it as MP4 (no re-encode).
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
    }
    try:
        with guarded_ydl(dl_opts, yt_dlp.YoutubeDL) as ydl:
            info = ydl.process_ie_result(info, download=True)
    finally:
        try:
            (folder / ".partial").rmdir()  # only succeeds once it's empty
        except OSError:
            pass

    filepath = _final_filepath(info or {})
    if not filepath or not filepath.is_file():
        raise DownloadError("Download finished but the video file was not found.")
    if not inside(filepath, folder):
        # The name is built from a sanitised stem, but the final path comes from yt-dlp's metadata:
        # never record (or later upload) a file outside the folder Reelay was told to use.
        raise DownloadError("The download was written outside the chosen folder, so Reelay didn't use it.")

    result.filename = filepath.name
    result.format_id = info.get("format_id") or ""
    result.watermark_free = _is_watermark_free(info)
    result.downloaded_at = datetime.now().isoformat(timespec="seconds")
    result.status = Status.DOWNLOADED
    result.error = ""
    result.progress = 100.0
    return result
