"""Map URLs / extractor names to the platform label used in filenames."""

from __future__ import annotations

import re
from urllib.parse import urlparse

PLATFORM_HOSTS = {
    "tiktok.com": "TikTok",
    "instagram.com": "Instagram",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
}

PLATFORM_EXTRACTOR_PREFIXES = {
    "TikTok": "TikTok",
    "Instagram": "Instagram",
    "Youtube": "YouTube",
}


def detect_platform(url: str, extractor_key: str | None = None) -> str:
    """Return 'TikTok' | 'Instagram' | 'YouTube' (or a sanitized extractor name)."""
    host = (urlparse(url).hostname or "").lower()
    for fragment, label in PLATFORM_HOSTS.items():
        if host == fragment or host.endswith("." + fragment):
            return label
    if extractor_key:
        for prefix, label in PLATFORM_EXTRACTOR_PREFIXES.items():
            if extractor_key.startswith(prefix):
                return label
        return re.sub(r"[^A-Za-z0-9]", "", extractor_key) or "Unknown"
    return "Unknown"


def is_supported_url(url: str) -> bool:
    return detect_platform(url) in PLATFORM_HOSTS.values()
