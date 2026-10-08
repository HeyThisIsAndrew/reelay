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

THREADS_MAX_SECONDS = 300

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


def platform_checks(post: Post, platform: str, video_path: Path | None, duration: float | None) -> list[Check]:
    checks: list[Check] = []
    if platform == Platform.YOUTUBE:
        checks.append(_check("title", "Title", _title_problem(post.title), "Written, within 100 characters."))
        checks.append(_check("description", "Description", _description_problem(post.description),
                             "Written, within 5,000 bytes."))
        tags_len = sum(len(t) + 1 for t in post.tags)
        checks.append(_check("tags", "Tags", f"Tags are {tags_len} characters in total; the limit is "
                                             f"{TAGS_MAX_CHARS}." if tags_len > TAGS_MAX_CHARS else None,
                             "Within 500 characters." if post.tags else "None (optional)."))
    elif platform in OWN_BLOCK:
        key, label = OWN_BLOCK[platform]
        text = (getattr(post, key) or "").strip()
        problem = (f"Write the {label} (otherwise Reelay would fall back to the title)." if not text
                   else text_problem(platform, text))
        checks.append(_check(key, label, problem, "Written, within the limit."))
        if platform == Platform.THREADS and duration and duration > THREADS_MAX_SECONDS:
            checks.append(Check("length", "Video length", WARN, "Threads videos can be at most 5 minutes long."))
    exists = video_path is not None and Path(video_path).is_file()
    checks.append(_check("video", "Video file", None if exists else "The video file is missing. Download it first.",
                         "Downloaded."))
    return checks


def evaluate(post: Post, enabled: frozenset[str] | set[str], video_path: Path | None,
             duration: float | None = None, targets: list[str] | None = None) -> Readiness:
    """Readiness for the post's target platforms (or `targets`)."""
    targets = list(dict.fromkeys(targets or post.target_platforms or [Platform.YOUTUBE]))
    platforms = {}
    for platform in targets:
        if platform not in Platform.ALL:
            continue
        auto = platform in enabled
        platforms[platform] = PlatformReadiness(platform, auto,
                                                platform_checks(post, platform, video_path, duration) if auto else [])
    return Readiness(platforms)
