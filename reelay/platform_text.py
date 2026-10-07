"""Per-platform post text: length rules, defaults, and Bluesky rich-text facets.

Limits follow each platform's own counting:
- Bluesky: 300 graphemes (user-perceived characters) and 3,000 UTF-8 bytes
  (app.bsky.feed.post lexicon).
- Threads: 500 characters, where an emoji counts as its UTF-8 byte length
  (Threads API docs).

When a card has no override, the default text is the title, plus the
description when both fit. Nothing is ever truncated: if even the title is
too long, the caller gets an error asking for an override.
"""

from __future__ import annotations

import re
import unicodedata

from .models import Platform, Post

BLUESKY_MAX_GRAPHEMES = 300
BLUESKY_MAX_BYTES = 3000
THREADS_MAX_CHARS = 500
TIKTOK_MAX_CHARS = 4000
INSTAGRAM_MAX_CHARS = 2200
INSTAGRAM_MAX_HASHTAGS = 30
FACEBOOK_MAX_CHARS = 63206

# Copy blocks on a card: post field → (label, platform rule key).
COPY_FIELDS: dict[str, tuple[str, str]] = {
    "tiktok_text": ("TikTok caption", "tiktok"),
    "instagram_text": ("Instagram caption", "instagram"),
    "bluesky_text": ("Bluesky text", "bluesky"),
    "threads_text": ("Threads text", "threads"),
    "facebook_text": ("Facebook text", "facebook"),
    "hashtags": ("Hashtags", "hashtags"),
}

_ZWJ = 0x200D


def _is_extender(cp: int, ch: str) -> bool:
    """Code points that attach to the previous character in the same grapheme."""
    return (unicodedata.combining(ch) > 0
            or unicodedata.category(ch) in ("Mn", "Me", "Mc")
            or 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF      # variation selectors
            or 0x1F3FB <= cp <= 0x1F3FF                                # skin tones
            or 0xE0020 <= cp <= 0xE007F)                               # tag sequences (flags)


def grapheme_count(text: str) -> int:
    """Count user-perceived characters (extended grapheme clusters).

    Covers what social captions contain: combining accents, emoji with skin
    tones / variation selectors, ZWJ sequences (👩‍👩‍👧 = 1), flags (🇺🇸 = 1)
    and CRLF. Python has no built-in grapheme segmentation; this follows the
    Unicode rules for those cases."""
    count, prev_zwj, prev_ri, prev_cr = 0, False, False, False
    for ch in text:
        cp = ord(ch)
        is_ri = 0x1F1E6 <= cp <= 0x1F1FF
        if prev_zwj or (count and (_is_extender(cp, ch) or cp == _ZWJ)) or (prev_cr and ch == "\n"):
            pass                                   # continues the current cluster
        elif is_ri and prev_ri:
            is_ri = False                          # second half of a flag pair
        else:
            count += 1
        prev_zwj = cp == _ZWJ
        prev_ri = is_ri
        prev_cr = ch == "\r"
    return count


def _is_emoji(cp: int) -> bool:
    return (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x2300 <= cp <= 0x23FF
            or 0x2B00 <= cp <= 0x2BFF or 0xFE00 <= cp <= 0xFE0F or cp == _ZWJ
            or 0xE0020 <= cp <= 0xE007F or cp in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139,
                                                    0x3030, 0x303D, 0x3297, 0x3299))


def threads_length(text: str) -> int:
    """Threads' count: 1 per character, but emoji count as their UTF-8 bytes."""
    return sum(len(ch.encode("utf-8")) if _is_emoji(ord(ch)) else 1 for ch in text)


def hashtag_count(text: str) -> int:
    return len(_TAG.findall(text or ""))


def text_problem(platform: str, text: str) -> str | None:
    """Why `text` can't be posted to `platform` (bluesky, threads, tiktok,
    instagram, facebook, hashtags), or None. Never shortens anything."""
    if platform == "tiktok":
        if len(text) > TIKTOK_MAX_CHARS:
            return f"TikTok caption is {len(text)} characters; the limit is {TIKTOK_MAX_CHARS}."
        return None
    if platform == "instagram":
        if len(text) > INSTAGRAM_MAX_CHARS:
            return f"Instagram caption is {len(text)} characters; the limit is {INSTAGRAM_MAX_CHARS}."
        if hashtag_count(text) > INSTAGRAM_MAX_HASHTAGS:
            return f"Instagram caption has {hashtag_count(text)} hashtags; the limit is {INSTAGRAM_MAX_HASHTAGS}."
        return None
    if platform == Platform.FACEBOOK:
        if len(text) > FACEBOOK_MAX_CHARS:
            return f"Facebook text is {len(text)} characters; the limit is {FACEBOOK_MAX_CHARS}."
        return None
    if platform == "hashtags":
        return None
    if platform == Platform.BLUESKY:
        n = grapheme_count(text)
        if n > BLUESKY_MAX_GRAPHEMES:
            return f"Bluesky text is {n} characters; the limit is {BLUESKY_MAX_GRAPHEMES}."
        if len(text.encode("utf-8")) > BLUESKY_MAX_BYTES:
            return f"Bluesky text is over {BLUESKY_MAX_BYTES} bytes."
    elif platform == Platform.THREADS:
        n = threads_length(text)
        if n > THREADS_MAX_CHARS:
            return f"Threads text is {n} characters (emoji count as several); the limit is {THREADS_MAX_CHARS}."
    return None


def override_for(post: Post, platform: str) -> str | None:
    return {Platform.BLUESKY: post.bluesky_text, Platform.THREADS: post.threads_text}.get(platform)


def resolve_text(post: Post, platform: str) -> str:
    """The text to post. Raises ValueError (never truncates)."""
    override = override_for(post, platform)
    if override is not None:
        problem = text_problem(platform, override)
        if problem:
            raise ValueError(problem)
        return override
    title, description = post.title.strip(), post.description.strip()
    combined = f"{title}\n\n{description}".strip() if description else title
    if text_problem(platform, combined) is None:
        return combined
    if text_problem(platform, title) is None:
        return title
    field = "bluesky_text" if platform == Platform.BLUESKY else "threads_text"
    raise ValueError(f"The title is too long for {Platform.LABELS[platform]}; set {field} to a shorter version.")


# ---------------------------------------------------------------- Bluesky facets
_TAG = re.compile(r"(?:^|(?<=\s))#([^\d\s\W][\w]{0,63})", re.UNICODE)
_LINK = re.compile(r"https?://[^\s<>\"')\]]+[^\s<>\"')\].,;:!?]")


def bluesky_facets(text: str) -> list[dict]:
    """Rich-text facets so hashtags and links are clickable (byte offsets, UTF-8)."""
    facets: list[dict] = []

    def span(start: int, end: int) -> dict:
        return {"byteStart": len(text[:start].encode("utf-8")), "byteEnd": len(text[:end].encode("utf-8"))}

    for m in _LINK.finditer(text):
        facets.append({"index": span(m.start(), m.end()),
                       "features": [{"$type": "app.bsky.richtext.facet#link", "uri": m.group(0)}]})
    for m in _TAG.finditer(text):
        start = m.start(1) - 1
        facets.append({"index": span(start, m.end()),
                       "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": m.group(1)}]})
    return sorted(facets, key=lambda f: f["index"]["byteStart"])
