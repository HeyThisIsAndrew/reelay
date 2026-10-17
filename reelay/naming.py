"""Filename templates, sanitising, and collision-free batch naming.

A template is plain text with {tokens}, e.g. "{platform}_{date}_{id}".
Unknown tokens are left as typed, so a typo never raises an error.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

from .models import VideoItem

DEFAULT_TEMPLATE = "{platform}_{date}_{id}"

# (plain-language label shown in the GUI, template)
PRESETS: list[tuple[str, str]] = [
    ("Platform, date and video ID", DEFAULT_TEMPLATE),
    ("Date and file label", "{date}_{custom}"),
    ("File label only", "{custom}"),
    ("Date and first words of the caption", "{date}_{title}"),
    ("Creator, date and file label", "{uploader}_{date}_{custom}"),
]

TOKENS: dict[str, str] = {
    "platform": "TikTok / Instagram / YouTube",
    "date": "Upload date, YYYYMMDD",
    "ymd": "Upload date, YYYY-MM-DD",
    "id": "The platform's video ID",
    "custom": "The video's file label (its video ID if it has none)",
    "title": "First words of the caption, as a slug",
    "uploader": "Creator handle",
    "n": "Position in the list: 001, 002…",
}

MISSING_DATE = "00000000"
MAX_STEM_LEN = 120
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TOKEN = re.compile(r"\{(\w+)\}")
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                     *(f"LPT{i}" for i in range(1, 10))}


def slugify(text: str, max_words: int = 8, max_len: int = 60) -> str:
    """'The loneliness in the middle…' -> 'the-loneliness-in-the-middle'."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"#\w+", " ", text)            # hashtags aren't part of the hook
    text = text.replace("'", "").replace("’", "")
    words = re.findall(r"[a-z0-9]+", text.lower())[:max_words]
    slug = "-".join(words)
    return slug[:max_len].rstrip("-")


def sanitize_stem(text: str) -> str:
    """Make user text safe as a filename on both macOS and Windows."""
    text = _ILLEGAL.sub("", text or "")
    text = re.sub(r"\s+", " ", text).strip().strip(".").strip()
    text = text[:MAX_STEM_LEN].rstrip(" .")
    if text.upper().split(".")[0] in _WINDOWS_RESERVED:
        text = "_" + text
    return text


def render_stem(template: str, item: VideoItem, index: int | None = None) -> str:
    """Fill a template for one video. Always returns a usable filename stem."""
    date = item.upload_date or MISSING_DATE
    values = {
        "platform": item.platform,
        "date": date,
        "ymd": f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) == 8 else date,
        "id": item.video_id,
        "custom": sanitize_stem(item.custom_name) or item.video_id,
        "title": slugify(item.hook) or item.video_id,
        "uploader": sanitize_stem(item.uploader) or item.platform,
        "n": f"{index:03d}" if index is not None else "",
    }
    filled = _TOKEN.sub(lambda m: values.get(m.group(1).lower(), m.group(0)), template or "")
    stem = sanitize_stem(filled)
    stem = re.sub(r"[_\- ]{2,}", lambda m: m.group(0)[0], stem).strip("_- ")
    return stem or render_stem(DEFAULT_TEMPLATE, item)


def assign_filenames(items: Iterable[VideoItem], template: str, ext: str = "mp4") -> dict[str, str]:
    """Name every item, adding _2, _3… so no two videos share a filename.

    Comparison is case-insensitive because macOS and Windows filesystems are.
    """
    names: dict[str, str] = {}
    used: set[str] = set()
    for index, item in enumerate(items, 1):
        stem = render_stem(template, item, index)
        candidate, n = stem, 2
        while candidate.lower() in used:
            candidate = f"{stem}_{n}"
            n += 1
        used.add(candidate.lower())
        names[item.key] = f"{candidate}.{ext}"
    return names
