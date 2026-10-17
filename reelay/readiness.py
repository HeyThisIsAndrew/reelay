"""Is a ticket actually ready to leave the building?

For each platform a ticket targets, a checklist built from the rules Reelay
already enforces: the platform's own copy is written, it's within that
platform's limits (counted the platform's way), and the video file is here.

- A platform whose automatic posting is off is "not checked": it never blocks,
  because its copy is pasted by hand and a person is looking at it.
- The ticket is READY when at least one of its target platforms is ready.
- The Ready gate (pipeline.schedule) refuses automatic scheduling to any
  platform that isn't ready, unless the caller forces it. Copying text by
  hand is never blocked.

Pure functions, no Qt and no database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .models import Platform, Post
from .platform_text import text_problem
from .publishing import DESCRIPTION_MAX_BYTES, TAGS_MAX_CHARS, TITLE_MAX

MB, GB = 1024 * 1024, 1024 * 1024 * 1024
THREADS_MAX_SECONDS, THREADS_MAX_BYTES = 300, 1 * GB
BLUESKY_MAX_SECONDS, BLUESKY_MAX_BYTES = 600, 300 * MB      # raised from 3 minutes / 100 MB on 26 Aug 2026
FACEBOOK_MAX_BYTES = 1 * GB                                 # Reels, and Page videos sent in one piece
FACEBOOK_REEL_SECONDS = (3, 90)                             # a vertical video this long goes up as a Reel
INSTAGRAM_MAX_BYTES = 300 * MB                              # Instagram's API limit for a Reel
SHORT_FORM_MAX_SECONDS = 180     # YouTube Shorts are at most 3 minutes


def is_short_form(width: int | None, height: int | None, duration: float | None) -> bool:
    """A Short: 3 minutes or less and not landscape (unknown sizes count as vertical)."""
    if duration and duration > SHORT_FORM_MAX_SECONDS:
        return False
    return not (width and height and width > height)


def is_facebook_reel(width: int | None, height: int | None, duration: float | None) -> bool:
    """Facebook Reels are vertical and 3–90 seconds long; anything else goes up as a regular Page video."""
    low, high = FACEBOOK_REEL_SECONDS
    return bool(width and height and height > width and duration and low <= duration <= high)


SHORT_FORM, LONG_FORM = "short", "long"


def video_form(item) -> str:
    """SHORT_FORM or LONG_FORM, by YouTube's rule for Shorts (above). A /shorts/ link is a Short whatever
    its size: the platform has already said so."""
    if "/shorts/" in (getattr(item, "url", "") or ""):
        return SHORT_FORM
    return SHORT_FORM if is_short_form(item.width, item.height, item.duration) else LONG_FORM

PASS, WARN = "pass", "warn"
READY, NOT_READY, NOT_CHECKED = "ready", "not_ready", "not_checked"

# The copy block each platform must have written for itself.
OWN_BLOCK = {Platform.BLUESKY: ("bluesky_text", "Bluesky post"),
             Platform.THREADS: ("threads_text", "Threads post"),
             Platform.FACEBOOK: ("facebook_text", "Facebook post")}


@dataclass(frozen=True)
class Check:
    id: str
    label: str        # what is checked, e.g. "Title"
    state: str        # PASS | WARN
    message: str      # plain words: what's fine, or what to fix

    @property
    def ok(self) -> bool:
        return self.state == PASS


@dataclass
class PlatformReadiness:
    platform: str
    auto_post: bool
    checks: list[Check] = field(default_factory=list)

    @property
    def state(self) -> str:
        if not self.auto_post:
            return NOT_CHECKED
        return READY if all(c.ok for c in self.checks) else NOT_READY

    @property
    def ready(self) -> bool:
        return self.state != NOT_READY

    @property
    def label(self) -> str:
        return Platform.LABELS.get(self.platform, self.platform)

    @property
    def problems(self) -> list[str]:
        return [f"{self.label}: {c.message}" for c in self.checks if not c.ok] if self.auto_post else []

    @property
    def note(self) -> str:
        if not self.auto_post:
            return "Automatic posting is off, so this isn't checked: you post it by hand."
        return "Ready to post." if self.ready else "Needs attention before it can be posted automatically."


@dataclass
class Readiness:
    platforms: dict[str, PlatformReadiness]

    @property
    def ready(self) -> bool:
        """READY when at least one target platform is ready."""
        return any(p.ready for p in self.platforms.values())

    @property
    def blocking(self) -> list[str]:
        return [name for name, p in self.platforms.items() if not p.ready]

    def problems(self, platforms: list[str] | None = None) -> list[str]:
        return [msg for name, p in self.platforms.items() if platforms is None or name in platforms
                for msg in p.problems]


def _check(cid: str, label: str, problem: str | None, fine: str) -> Check:
    return Check(cid, label, WARN if problem else PASS, problem or fine)


def _title_problem(title: str) -> str | None:
    title = title.strip()
    if not title:
        return "Add a title."
    if len(title) > TITLE_MAX:
        return f"The title is {len(title)} characters; YouTube allows {TITLE_MAX}."
    if "<" in title or ">" in title:
        return "The title can't contain < or >."
    return None


def _description_problem(description: str) -> str | None:
    if not description.strip():
        return "Add a description."
    if len(description.encode("utf-8")) > DESCRIPTION_MAX_BYTES:
        return f"The description is over {DESCRIPTION_MAX_BYTES:,} bytes."
    if "<" in description or ">" in description:
        return "The description can't contain < or >."
    return None


def _size_check(size: int | None, limit: int, label: str) -> Check | None:
    if size is None or size <= limit:
        return None
    unit = f"{limit // GB} GB" if limit >= GB else f"{limit // MB} MB"
    return Check("size", "File size", WARN, f"{label} videos can be at most {unit}; this one is "
                                            f"{size / MB:,.0f} MB.")


def platform_checks(post: Post, platform: str, video_path: Path | None, duration: float | None,
                    media=None) -> list[Check]:
    """`media` is what the file to be posted really is (probe.MediaInfo); when it's known, its length, size
    and shape are checked rather than what the platform reported when the video was listed."""
    checks: list[Check] = []
    if media is not None and media.duration:
        duration = media.duration
    size = media.size if media is not None else None
    if size is None and video_path is not None:
        try:
            size = Path(video_path).stat().st_size
        except OSError:
            size = None
    if platform == Platform.YOUTUBE:
        checks.append(_check("title", "Title", _title_problem(post.title), "Written, within 100 characters."))
        checks.append(_check("description", "Description", _description_problem(post.description),
                             "Written, within 5,000 bytes."))
        tags_len = sum(len(t) + 1 for t in post.tags)
        checks.append(_check("tags", "Tags", f"Tags are {tags_len} characters in total; the limit is "
                                             f"{TAGS_MAX_CHARS}." if tags_len > TAGS_MAX_CHARS else None,
                             "Within 500 characters." if post.tags else "None (optional)."))
        if media is not None and media.width and media.height:
            short = is_short_form(media.width, media.height, duration)
            checks.append(Check("format", "Format", PASS, "Posts as a Short (square or vertical, 3 minutes or "
                                                          "less)." if short else "Posts as a regular YouTube "
                                                          "video."))
        elif duration and duration > SHORT_FORM_MAX_SECONDS:
            checks.append(Check("format", "Format", PASS, "Posts as a regular YouTube video (longer than 3 "
                                                          "minutes, so not a Short)."))
    elif platform in OWN_BLOCK:
        key, label = OWN_BLOCK[platform]
        text = (getattr(post, key) or "").strip()
        problem = (f"Write the {label} (otherwise Reelay would fall back to the title)." if not text
                   else text_problem(platform, text))
        checks.append(_check(key, label, problem, "Written, within the limit."))
        if platform == Platform.THREADS:
            if duration and duration > THREADS_MAX_SECONDS:
                checks.append(Check("length", "Video length", WARN, "Threads videos can be at most 5 minutes long."))
            checks.append(_size_check(size, THREADS_MAX_BYTES, "Threads"))
        if platform == Platform.BLUESKY:
            if duration and duration > BLUESKY_MAX_SECONDS:
                checks.append(Check("length", "Video length", WARN, "Bluesky videos can be at most 10 minutes "
                                                                     "long."))
            checks.append(_size_check(size, BLUESKY_MAX_BYTES, "Bluesky"))
        if platform == Platform.FACEBOOK:
            checks.append(_size_check(size, FACEBOOK_MAX_BYTES, "Facebook"))
            if media is not None and media.width and media.height:
                reel = is_facebook_reel(media.width, media.height, duration)
                checks.append(Check("format", "Format", PASS, "Posts as a Reel (vertical, 3–90 seconds)." if reel
                                    else "Posts as a regular Page video."))
    exists = video_path is not None and Path(video_path).is_file()
    checks.append(_check("video", "Video file", None if exists else "The video file is missing. Download it first.",
                         "Downloaded."))
    return [c for c in checks if c is not None]


def evaluate(post: Post, enabled: frozenset[str] | set[str], video_path: Path | None,
             duration: float | None = None, targets: list[str] | None = None, media=None) -> Readiness:
    """Readiness for the post's target platforms (or `targets`)."""
    targets = list(dict.fromkeys(targets or post.target_platforms or [Platform.YOUTUBE]))
    platforms = {}
    for platform in targets:
        if platform not in Platform.LABELS:
            continue
        auto = platform in enabled
        platforms[platform] = PlatformReadiness(platform, auto,
                                                platform_checks(post, platform, video_path, duration, media)
                                                if auto else [])
    return Readiness(platforms)
