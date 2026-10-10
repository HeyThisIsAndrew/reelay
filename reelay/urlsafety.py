"""Which links Reelay will fetch on an agent's behalf, and which files it will accept.

Anything an agent can make Reelay fetch (transcripts today; more through the
MCP server later) is limited to the three platforms Reelay imports from, over
http(s) only. yt-dlp's "generic" extractor would otherwise fetch any address,
including ones on the local network, and `file://` must never be reachable.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

MEDIA_HOSTS = ("tiktok.com", "instagram.com", "youtube.com", "youtu.be")
# yt-dlp extractor names (regular expressions) that may run for agent-triggered fetches
ALLOWED_EXTRACTORS = [r"tiktok(:.*)?", r"vm\.tiktok", r"instagram(:.*)?", r"youtube(:.*)?"]
IMAGE_TYPES = (".jpg", ".jpeg", ".png", ".webp")


def media_url_problem(url: str) -> str | None:
    """Why `url` may not be fetched for an agent, or None if it's a TikTok, Instagram or YouTube link."""
    url = (url or "").strip()
    try:
        parts = urlparse(url)
    except ValueError:
        return "That link can't be read."
    if parts.scheme.lower() not in ("http", "https"):
        return "Only http(s) links can be fetched."
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or not any(host == h or host.endswith("." + h) for h in MEDIA_HOSTS):
        return "Only TikTok, Instagram and YouTube links can be fetched."
    if parts.username or parts.password:
        return "Links with a user name or password can't be fetched."
    return None


def restrict_ydl(opts: dict) -> dict:
    """yt-dlp options for an agent-triggered fetch: only the platform extractors, never file://."""
    return {**opts, "allowed_extractors": list(ALLOWED_EXTRACTORS), "enable_file_urls": False}


def image_path_problem(path: str, library: Path | str) -> str | None:
    """A cover image path set by an agent must be an image inside the profile's library folder."""
    if not path:
        return None
    p = Path(path).expanduser()
    if p.suffix.lower() not in IMAGE_TYPES:
        return "The cover must be a .jpg, .png or .webp image."
    try:
        resolved, root = p.resolve(strict=False), Path(library).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return "That cover path can't be used."
    if root != resolved and root not in resolved.parents:
        return "The cover image must be inside this profile's video folder."
    return None


def inside(path: Path | str, folder: Path | str) -> bool:
    """True if `path` (after resolving links and ..) is inside `folder`."""
    try:
        p, root = Path(path).resolve(strict=False), Path(folder).resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return root in p.parents
