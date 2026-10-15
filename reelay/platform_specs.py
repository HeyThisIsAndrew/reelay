"""One registry of the publishing destinations: what each one is called, which
Post fields hold its copy, how long that copy may be, and how it gets posted.

Why a module of its own: ``platforms.py`` answers a different question (which
site a *source* link came from, as the "TikTok" / "YouTube" label used in file
names), and four modules import it for exactly that. This module is about
*destinations*, keyed by ``models.Platform`` ("tiktok", "youtube").

Nothing here re-declares a fact that already has a home. Labels come from
``models.Platform.LABELS``; which platforms are on a ticket from
``models.MANUAL_PLATFORMS``; limits from ``platform_text`` and ``publishing``;
whether Reelay posts on its own from ``flags`` (the same check Settings and the
dispatcher use). No Qt here: the GUI draws each platform's logo with
``gui.logos.platform_pixmap(spec.key)``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from . import platform_text as T
from .flags import FLAG_PLATFORMS, flag_source
from .models import MANUAL_PLATFORMS, Platform
from .publishing import DESCRIPTION_MAX_BYTES, TAGS_MAX_CHARS, TITLE_MAX

# ---------------------------------------------------------------- how a platform gets posted
ALWAYS = "always"          # Reelay posts it itself (YouTube, once a channel is connected)
WHEN_ON = "when_on"        # Reelay can post it once turned on in Settings → Automatic posting
BY_HAND = "by_hand"        # always posted by hand; Reelay prepares it (Prepare to Publish) or you copy the caption
PLANNED = "planned"        # named so data can refer to it; no posting yet


def _posting(key: str) -> str:
    """Derived from the checks flags.py and models.Platform already make, never a separate list."""
    if flag_source(key)[1] == "always":
        return ALWAYS
    if key in FLAG_PLATFORMS:
        return WHEN_ON
    if key in Platform.HAND_ONLY:
        return BY_HAND
    return PLANNED


# ---------------------------------------------------------------- copy fields and their limits
def _tags_length(text: str) -> int:
    """YouTube counts each tag plus a separator."""
    return sum(len(t.strip().lstrip("#")) + 1 for t in text.split(",") if t.strip())


@dataclass(frozen=True)
class FieldLimit:
    limit: int | None                    # None: no limit (the shared hashtags)
    measure: Callable[[str], int]        # how the platform counts
    unit: str = ""                       # shown after the count (" bytes")


FIELD_LIMITS: dict[str, FieldLimit] = {
    "title": FieldLimit(TITLE_MAX, lambda t: len(t.strip())),
    "description": FieldLimit(DESCRIPTION_MAX_BYTES, lambda t: len(t.encode("utf-8")), " bytes"),
    "tags": FieldLimit(TAGS_MAX_CHARS, _tags_length),
    "tiktok_text": FieldLimit(T.TIKTOK_MAX_CHARS, len),
    "instagram_text": FieldLimit(T.INSTAGRAM_MAX_CHARS, len),
    "bluesky_text": FieldLimit(T.BLUESKY_MAX_GRAPHEMES, T.grapheme_count),
    "threads_text": FieldLimit(T.THREADS_MAX_CHARS, T.threads_length),
    "facebook_text": FieldLimit(T.FACEBOOK_MAX_CHARS, len),
    "hashtags": FieldLimit(None, len),
}
SHARED_FIELDS = ("hashtags",)            # one block for every platform


# ---------------------------------------------------------------- the registry
@dataclass(frozen=True)
class PlatformSpec:
    key: str                              # models.Platform value
    copy_fields: tuple[str, ...] = ()     # Post fields holding this platform's text, in editor order

    @property
    def label(self) -> str:
        return Platform.LABELS[self.key]

    @property
    def posting(self) -> str:
        return _posting(self.key)

    @property
    def on_ticket(self) -> bool:
        """Has a place on the ticket (target, Preparing / Scheduled / Posted)."""
        return self.key in MANUAL_PLATFORMS

    @property
    def caption_field(self) -> str:
        """The field a person pastes as the post's text (YouTube: the description)."""
        return self.copy_fields[-1] if self.key != Platform.YOUTUBE else "description"


SPECS: dict[str, PlatformSpec] = {s.key: s for s in (
    PlatformSpec(Platform.YOUTUBE, ("title", "description", "tags")),
    PlatformSpec(Platform.TIKTOK, ("tiktok_text",)),
    PlatformSpec(Platform.INSTAGRAM, ("instagram_text",)),
    PlatformSpec(Platform.BLUESKY, ("bluesky_text",)),
    PlatformSpec(Platform.THREADS, ("threads_text",)),
    PlatformSpec(Platform.FACEBOOK, ("facebook_text",)),
    PlatformSpec(Platform.SNAPCHAT),      # posted by hand; the ticket has no Snapchat box yet
    PlatformSpec(Platform.PINTEREST),
    PlatformSpec(Platform.LINKEDIN),
)}

# The ticket's platforms, in the ticket's order (models.MANUAL_PLATFORMS is the source).
TICKET: tuple[PlatformSpec, ...] = tuple(SPECS[p] for p in MANUAL_PLATFORMS)
# Every copy field the ticket edits, platform fields first, then the shared ones.
COPY_FIELDS: tuple[str, ...] = tuple(f for s in TICKET for f in s.copy_fields) + SHARED_FIELDS


def spec(key: str) -> PlatformSpec:
    return SPECS[key]


def label(key: str) -> str:
    """Display name for a platform key, or the key itself if it's not one of ours."""
    return Platform.LABELS.get(key, key)


def by_label(name: str) -> PlatformSpec | None:
    """A source label ("TikTok", as platforms.detect_platform writes it) to its spec."""
    return SPECS.get((name or "").strip().lower())


def platform_of_field(field: str) -> str | None:
    return next((s.key for s in TICKET if field in s.copy_fields), None)


def count(field: str, text: str) -> tuple[str, bool]:
    """'12/4,000' (with the field's unit) and whether it's over the limit. Never shortens anything."""
    lim = FIELD_LIMITS[field]
    used = lim.measure(text)
    if lim.limit is None:
        return f"{used:,}", False
    return f"{used:,}/{lim.limit:,}{lim.unit}", used > lim.limit
