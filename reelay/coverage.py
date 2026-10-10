"""The coverage map: where a video stands on every platform, at a glance.

One state per platform, computed from what's already stored (the platform's
progress, the ticket and the readiness checks). It drives the logos on the
front of each Board card and is available to the agent API.

    published       live there                                  green ✓
    scheduled       scheduled there, or Reelay is posting it    blue clock
    ready           going there, and nothing is missing         amber dot
    needs_work      going there, but something's missing/failed red !
    not_applicable  not going there                             faded logo
"""

from __future__ import annotations

from dataclasses import dataclass

from .handoff import CAPTION_FIELD, caption_for
from .models import MANUAL_PLATFORMS, Platform, Post
from .platform_text import text_problem

PUBLISHED, SCHEDULED, READY, NEEDS_WORK, NOT_APPLICABLE = (
    "published", "scheduled", "ready", "needs_work", "not_applicable")
ORDER = (Platform.TIKTOK, Platform.INSTAGRAM, Platform.YOUTUBE, Platform.FACEBOOK, Platform.THREADS,
         Platform.BLUESKY)
LABELS = {PUBLISHED: "Published", SCHEDULED: "Scheduled", READY: "Ready to publish", NEEDS_WORK: "Needs work",
          NOT_APPLICABLE: "Not going there"}


@dataclass(frozen=True)
class Coverage:
    platform: str
    state: str
    reason: str = ""

    @property
    def text(self) -> str:
        label = Platform.LABELS.get(self.platform, self.platform)
        return f"{label}: {LABELS[self.state]}" + (f". {self.reason}" if self.reason else "")


def _hand_problem(post: Post | None, platform: str) -> str | None:
    """What's missing to post by hand (the caption), or None."""
    if post is None:
        return "Write the ticket first."
    caption = caption_for(post, platform)
    if not caption:
        return f"Write the {Platform.LABELS.get(platform, platform)} caption."
    if platform in CAPTION_FIELD:
        return text_problem(platform, caption)
    return None


def compute(post: Post | None, platform_states: dict, readiness=None, downloaded: bool = True,
            interrupted: frozenset[str] | set[str] = frozenset()) -> dict[str, Coverage]:
    """platform → Coverage, for every platform, in ORDER. `interrupted`: platforms whose
    send was cut off or missed and waits for a decision."""
    out: dict[str, Coverage] = {}
    for platform in ORDER:
        if platform not in MANUAL_PLATFORMS:
            continue
        state = platform_states.get(platform)
        planned = state is not None and (state.target or state.state in ("scheduled", "sending", "failed"))
        if not planned:
            if state is not None and state.state == "published":       # posted there without being planned
                out[platform] = Coverage(platform, PUBLISHED)
            else:
                out[platform] = Coverage(platform, NOT_APPLICABLE)
            continue
        if state.state == "published":
            out[platform] = Coverage(platform, PUBLISHED)
        elif state.state in ("scheduled", "sending"):
            out[platform] = Coverage(platform, SCHEDULED, "Reelay is posting it now" if state.state == "sending"
                                     else "")
        elif state.state == "failed" and platform in interrupted:
            out[platform] = Coverage(platform, NEEDS_WORK, "Interrupted: say whether it was posted")
        elif state.state == "failed":
            out[platform] = Coverage(platform, NEEDS_WORK, "Reelay couldn't post it")
        elif not downloaded:
            out[platform] = Coverage(platform, NEEDS_WORK, "The video file is missing")
        else:
            pr = readiness.platforms.get(platform) if readiness is not None else None
            if pr is not None and pr.auto_post:
                problems = [c.message for c in pr.checks if not c.ok]
                problem = problems[0] if problems else None
            else:
                problem = _hand_problem(post, platform)
            out[platform] = Coverage(platform, NEEDS_WORK, problem) if problem else Coverage(platform, READY)
    return out


def summary(coverage: dict[str, Coverage]) -> str:
    """e.g. "2 published · 1 ready · 1 needs work" (only planned platforms count)."""
    counts: dict[str, int] = {}
    for c in coverage.values():
        if c.state != NOT_APPLICABLE:
            counts[c.state] = counts.get(c.state, 0) + 1
    return " · ".join(f"{counts[s]} {LABELS[s].lower()}" for s in (PUBLISHED, SCHEDULED, READY, NEEDS_WORK)
                      if counts.get(s))


SHORT = {PUBLISHED: "live", SCHEDULED: "scheduled", READY: "ready", NEEDS_WORK: "to fix"}


def short_summary(coverage: dict[str, Coverage]) -> str:
    """Card-sized: e.g. "1 live · 1 scheduled · 1 to fix"."""
    counts: dict[str, int] = {}
    for c in coverage.values():
        if c.state != NOT_APPLICABLE:
            counts[c.state] = counts.get(c.state, 0) + 1
    return " · ".join(f"{counts[s]} {SHORT[s]}" for s in (PUBLISHED, SCHEDULED, READY, NEEDS_WORK) if counts.get(s))


def needs_work(coverage: dict[str, Coverage]) -> list[str]:
    return [c.platform for c in coverage.values() if c.state == NEEDS_WORK]
