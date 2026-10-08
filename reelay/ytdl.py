"""yt-dlp options shared by every call, plus ffmpeg discovery."""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

log = logging.getLogger("reelay")

# Browsers yt-dlp can read login cookies from (needed for most Instagram use).
COOKIE_BROWSERS = ["chrome", "safari", "firefox", "edge", "brave", "chromium", "opera", "vivaldi"]


class YtDlpLogger:
    """Route yt-dlp output into logging.

    yt-dlp errors go to DEBUG because every failure is re-raised as an
    exception and reported once, in plain language, by the caller.
    """

    def debug(self, msg: str) -> None:
        log.debug(msg)

    def info(self, msg: str) -> None:
        log.debug(msg)

    def warning(self, msg: str) -> None:
        log.warning(msg)

    def error(self, msg: str) -> None:
        log.debug(msg)


def find_ffmpeg() -> str | None:
    """Prefer a system ffmpeg (it ships ffprobe), else the bundled one.

    Apps launched from Finder don't inherit the shell PATH, so the usual
    Homebrew locations are checked explicitly.
    """
    if not os.environ.get("REELAY_BUNDLED_FFMPEG"):  # set to test the bundled copy
        found = shutil.which("ffmpeg")
        if found:
            return found
        for folder in ("/opt/homebrew/bin", "/usr/local/bin"):
            candidate = Path(folder) / "ffmpeg"
            if candidate.is_file():
                return str(candidate)
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 — not installed / no binary for this platform
        return None


def base_options(cookies_browser: str | None = None, cookies_file: str | None = None) -> dict:
    opts: dict = {
        "logger": YtDlpLogger(),
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "sleep_interval_requests": 0.5,
    }
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    if cookies_file:
        opts["cookiefile"] = str(cookies_file)
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    return opts


def friendly_error(exc: BaseException, url: str = "") -> str:
    """Turn yt-dlp's verbose error text into one plain sentence."""
    msg = re.sub(r"\s+", " ", str(exc).replace("ERROR: ", "")).strip()
    low = msg.lower()
    if "requested format is not available" in low:
        return "No watermark-free / downloadable version is available for this video."
    if "unsupported url" in low:
        return "That link isn't a supported TikTok, Instagram or YouTube URL."
    if "instagram" in (url + low) and any(s in low for s in ("login", "cookies", "rate-limit", "empty media")):
        return ("Instagram requires you to be logged in. Choose your browser under "
                "'Use browser sign-in' (and be signed in to Instagram there), then try again.")
    if "private" in low:
        return ("This video or account is private. For your own private videos, set “Use browser sign-in” to a browser "
                "signed into that account.")
    if "ip address is blocked" in low or "http error 403" in low:
        return "The platform blocked the request (the video may be deleted, private, or region-locked)."
    if "http error 429" in low or "too many requests" in low:
        return "Rate-limited by the platform. Wait a few minutes and try again."
    if "could not copy" in low and "cookie" in low:
        return "Couldn't read browser cookies. Quit that browser completely and try again."
    return msg[:300]
