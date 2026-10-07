"""Turning a downloaded video into a YouTube post: defaults, validation,
the API resource, and reconciling scheduled posts with YouTube."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Post, PostStatus, Privacy, Stage, VideoItem
from .text import short_title

TITLE_MAX = 100
DESCRIPTION_MAX_BYTES = 5000
TAGS_MAX_CHARS = 500
MIN_SCHEDULE_LEAD = timedelta(minutes=10)
CATEGORY_PEOPLE_BLOGS = "22"


def humanize_name(custom_name: str) -> str:
    """'letting-go-of-who-you-were' -> 'Letting go of who you were'."""
    text = re.sub(r"[_\-]+", " ", custom_name or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:1].upper() + text[1:] if text else ""


def default_post(video_row: int, item: VideoItem, account_id: int | None = None) -> Post:
    """Pre-fill a post from the original video's metadata."""
    title = humanize_name(item.custom_name) or short_title(item.hook, TITLE_MAX)
    title = re.sub(r"\s*#\w+", "", title).strip() or title  # hashtags belong in tags, not the title
    title = title[:1].upper() + title[1:]
    tags = item.hashtags or [t for t in item.tags if t]
    return Post(video_row=video_row, account_id=account_id, title=clean_text(title)[:TITLE_MAX],
                description=clean_text(item.description).strip(), tags=tags[:30])


def clean_text(text: str) -> str:
    """YouTube rejects '<' and '>' in titles and descriptions."""
    return (text or "").replace("<", "‹").replace(">", "›")


def to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive = local time
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value.replace(".000Z", "Z"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def validate_post(post: Post, video_path: Path | None, now: datetime | None = None) -> list[str]:
    """Everything that would make YouTube reject the upload, in plain words."""
    now = now or datetime.now(timezone.utc)
    errors: list[str] = []
    if post.account_id is None:
        errors.append("Choose a YouTube channel to post to.")
    title = post.title.strip()
    if not title:
        errors.append("Add a title.")
    elif len(title) > TITLE_MAX:
        errors.append(f"Title is {len(title)} characters; YouTube allows {TITLE_MAX}.")
    if "<" in post.title + post.description or ">" in post.title + post.description:
        errors.append("Titles and descriptions can't contain < or >.")
    if len(post.description.encode("utf-8")) > DESCRIPTION_MAX_BYTES:
        errors.append("Description is too long for YouTube (5,000 bytes max).")
    if sum(len(t) + 1 for t in post.tags) > TAGS_MAX_CHARS:
        errors.append("Tags are too long in total (500 characters max).")
    if post.privacy not in Privacy.ALL:
        errors.append("Choose a visibility.")
    when = parse_utc(post.publish_at)
    if post.publish_at and when is None:
        errors.append("The scheduled time isn't valid.")
    elif when is not None and when < now + MIN_SCHEDULE_LEAD:
        errors.append("Schedule at least 10 minutes from now.")
    if video_path is None or not Path(video_path).is_file():
        errors.append("The video file is missing. Download it again first.")
    return errors


def build_resource(post: Post, add_shorts_tag: bool = False) -> dict:
    """The JSON body for videos.insert."""
    description = post.description
    if add_shorts_tag and "#shorts" not in (post.title + description).lower():
        description = (description.rstrip() + "\n\n#Shorts").strip()
    status: dict = {"selfDeclaredMadeForKids": post.made_for_kids}
    if post.publish_at:
        # YouTube only honours publishAt on private videos; it flips them public at that time.
        status.update(privacyStatus=Privacy.PRIVATE, publishAt=post.publish_at)
    else:
        status["privacyStatus"] = post.privacy
    return {
        "snippet": {
            "title": post.title.strip()[:TITLE_MAX],
            "description": description,
            "tags": post.tags,
            "categoryId": CATEGORY_PEOPLE_BLOGS,
        },
        "status": status,
    }


def stage_after_upload(post: Post) -> str:
    return Stage.SCHEDULED if post.publish_at else Stage.PUBLISHED


def reconcile(post: Post, remote: dict | None, now: datetime | None = None) -> tuple[str, str]:
    """Compare a scheduled post with YouTube's view of the video.

    Returns (new post status, note for the user)."""
    now = now or datetime.now(timezone.utc)
    if remote is None:
        return PostStatus.FAILED, "This video is no longer on YouTube (deleted from YouTube Studio?)."
    if remote.get("privacy") in (Privacy.PUBLIC, Privacy.UNLISTED):
        return PostStatus.PUBLISHED, ""
    when = parse_utc(post.publish_at)
    if remote.get("privacy") == Privacy.PRIVATE and when and when + timedelta(minutes=30) < now:
        return PostStatus.SCHEDULED, (
            "YouTube kept this video private after its scheduled time. Videos uploaded through an "
            "unaudited Google API project stay private until the project passes YouTube's API audit.")
    return post.status, ""
