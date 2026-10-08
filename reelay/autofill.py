"""Fill a ticket's empty copy from the original post.

The creator's own caption is usually the right starting point for every
platform, so empty blocks are filled from it, using each platform's own rules.
Nothing is ever shortened: if the caption doesn't fit a platform, that block
stays empty for a person to write.
"""

from __future__ import annotations

from .models import Platform, Post, Ticket, VideoItem
from .platform_text import resolve_text, text_problem
from .publishing import DESCRIPTION_MAX_BYTES, clean_text, default_post

COPY_FROM_CAPTION = {"tiktok_text": "tiktok", "instagram_text": "instagram", "facebook_text": Platform.FACEBOOK}


def _empty(value) -> bool:
    return value is None or value == "" or value == []


def suggest(post: Post, item: VideoItem, ticket: Ticket | None = None) -> tuple[dict, dict]:
    """(post field values, ticket field values) for fields that are empty now
    and can be filled from the original post without breaking a limit."""
    base = default_post(post.video_row, item)
    caption = (item.description or "").strip()
    post_changes: dict = {}
    if _empty(post.title.strip()) and base.title:
        post_changes["title"] = base.title
    if _empty(post.description.strip()) and caption and len(clean_text(caption).encode()) <= DESCRIPTION_MAX_BYTES:
        post_changes["description"] = clean_text(caption)
    if _empty(post.tags) and base.tags:
        post_changes["tags"] = base.tags
    for key, rule in COPY_FROM_CAPTION.items():
        if _empty(getattr(post, key)) and caption and text_problem(rule, caption) is None:
            post_changes[key] = caption
    filled = Post(**{**post.__dict__, **post_changes})
    for platform in (Platform.BLUESKY, Platform.THREADS):
        key = f"{platform}_text"
        if _empty(getattr(post, key)):
            try:
                text = resolve_text(Post(**{**filled.__dict__, key: None}), platform)
            except ValueError:
                continue
            if text:
                post_changes[key] = text
    if _empty(post.hashtags) and item.hashtags:
        post_changes["hashtags"] = " ".join(f"#{t}" for t in item.hashtags)

    ticket_changes: dict = {}
    if ticket is not None and _empty(ticket.creator) and item.uploader:
        ticket_changes["creator"] = item.uploader
    return post_changes, ticket_changes
