"""Hashtags as categories: what a video is tagged with, and how many videos
carry each tag. Hashtags come from the original caption and the ticket's own
hashtag block, compared without the # and without case."""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

_TAG = re.compile(r"#(\w+)", re.UNICODE)


def parse(text: str | None) -> list[str]:
    return sorted({t.lower() for t in _TAG.findall(text or "")})


def video_tags(item, post=None) -> list[str]:
    """Hashtags of a video: its caption, plus the ticket's hashtag block if any."""
    tags = set(item.hashtags)
    if post is not None:
        tags.update(parse(post.hashtags))
    return sorted(tags)


def counts(tag_lists: Iterable[Iterable[str]]) -> list[tuple[str, int]]:
    """(tag, how many videos have it), most common first, then A–Z."""
    c = Counter(t for tags in tag_lists for t in set(tags))
    return sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))
