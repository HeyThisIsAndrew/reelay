"""The VideoItem record shared by the fetcher, downloader, library and GUI."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path


class Stage:
    """Columns of the content board."""
    BACKLOG = "backlog"
    STAGING = "staging"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    ALL = (BACKLOG, STAGING, SCHEDULED, PUBLISHED)
    LABELS = {BACKLOG: "Backlog", STAGING: "Staging", SCHEDULED: "Scheduled", PUBLISHED: "Published"}


class PostStatus:
    DRAFT = "draft"
    UPLOADING = "uploading"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    FAILED = "failed"


class Platform:
    """Publishing destinations."""
    YOUTUBE = "youtube"
    BLUESKY = "bluesky"
    THREADS = "threads"
    FACEBOOK = "facebook"
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    ALL = (YOUTUBE, BLUESKY, THREADS, FACEBOOK)       # can be posted automatically (when turned on)
    HAND_ONLY = (TIKTOK, INSTAGRAM)                   # always posted by hand (copy and paste)
    LABELS = {YOUTUBE: "YouTube", TIKTOK: "TikTok", INSTAGRAM: "Instagram", BLUESKY: "Bluesky", THREADS: "Threads",
              FACEBOOK: "Facebook"}
    # Platforms that hold a scheduled post on their own servers (Reelay hands the
    # video over right away). The rest are posted by Reelay at the due time.
    NATIVE_SCHEDULING = (YOUTUBE, FACEBOOK)


class DispatchStatus:
    """Per-platform delivery state of a scheduled post."""
    PENDING = "pending"          # waiting for its due time (or a retry)
    DISPATCHING = "dispatching"  # claimed; platform calls in progress
    SCHEDULED = "scheduled"      # accepted by the platform; it goes live at the set time
    PUBLISHED = "published"      # live
    FAILED = "failed"            # gave up; see error
    ACTIVE = (PENDING, DISPATCHING, SCHEDULED)
    ALL = (PENDING, DISPATCHING, SCHEDULED, PUBLISHED, FAILED)


class Privacy:
    PUBLIC = "public"
    UNLISTED = "unlisted"
    PRIVATE = "private"
    ALL = (PUBLIC, UNLISTED, PRIVATE)


class Status:
    NEW = "new"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    FAILED = "failed"


# Fields that only matter while the app is running; never written to disk.
RUNTIME_FIELDS = {"checked", "status", "error", "progress"}

GENERIC_TITLE = re.compile(r"^(video|reel|post) by ", re.IGNORECASE)


@dataclass
class VideoItem:
    platform: str
    video_id: str
    url: str
    title: str = ""
    description: str = ""
    upload_date: str = ""          # YYYYMMDD, "" when not known yet
    duration: float | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    tags: list[str] = field(default_factory=list)
    uploader: str = ""
    thumbnail_url: str = ""
    width: int | None = None
    height: int | None = None
    custom_name: str = ""
    filename: str = ""             # name of the file on disk once downloaded
    folder: str = ""               # folder the file was downloaded into
    format_id: str = ""
    watermark_free: bool | None = None
    downloaded_at: str = ""
    availability: str = ""         # YouTube listing via the Data API: public | unlisted | private | scheduled
    products: list | None = None   # products tagged on the post (TikTok Shop); None = not looked up yet
    # runtime-only
    checked: bool = True
    status: str = Status.NEW
    error: str = ""
    progress: float = 0.0

    # ------------------------------------------------------------------ #
    @property
    def key(self) -> str:
        return f"{self.platform}_{self.video_id}"

    @property
    def hashtags(self) -> list[str]:
        return sorted({t.lower() for t in re.findall(r"#(\w+)", self.description or "")})

    @property
    def hook(self) -> str:
        """The video's opening line / title as a viewer would recognise it.

        Instagram titles are generic ("Video by username") and TikTok titles
        are a truncated caption, so fall back to the caption's first line.
        """
        title = (self.title or "").strip()
        if not title or title == self.video_id or GENERIC_TITLE.match(title) or title.endswith("..."):
            first = next((ln.strip() for ln in (self.description or "").splitlines() if ln.strip()), "")
            if first:
                return first
        return title or self.video_id

    @property
    def has_details(self) -> bool:
        """Flat profile listings (e.g. YouTube) omit the date and caption."""
        return bool(self.upload_date)

    @property
    def is_downloaded(self) -> bool:
        return self.status == Status.DOWNLOADED and bool(self.filename)

    def path(self, default_folder: Path | str | None = None) -> Path | None:
        """Absolute path of the downloaded file (whether or not it exists)."""
        folder = self.folder or default_folder
        return Path(folder) / self.filename if (folder and self.filename) else None

    def refresh_download_status(self, default_folder: Path | str | None = None) -> None:
        """Mark as downloaded only if the file is really on disk."""
        path = self.path(default_folder)
        if path is not None and path.is_file():
            self.status, self.checked = Status.DOWNLOADED, False
        elif self.status == Status.DOWNLOADED:
            self.status = Status.NEW

    # ------------------------------------------------------------------ #
    def update_from_info(self, info: dict) -> None:
        """Merge fields from a yt-dlp info dict, never blanking known values."""
        def put(attr: str, value) -> None:
            if value not in (None, "", []):
                setattr(self, attr, value)

        put("title", info.get("title"))
        put("description", info.get("description"))
        put("duration", info.get("duration"))
        put("view_count", info.get("view_count"))
        put("like_count", info.get("like_count"))
        put("comment_count", info.get("comment_count"))
        put("tags", info.get("tags"))
        put("uploader", info.get("uploader") or info.get("channel"))
        put("width", info.get("width"))
        put("height", info.get("height"))
        put("thumbnail_url", pick_thumbnail(info))
        put("upload_date", info.get("upload_date") or _date_from_timestamp(info.get("timestamp")))
        if info.get("webpage_url"):
            self.url = info["webpage_url"]

    def merge_metadata(self, other: VideoItem) -> None:
        """Copy descriptive metadata (not file/runtime state) from another item."""
        for name in ("title", "description", "upload_date", "duration", "view_count",
                     "like_count", "comment_count", "tags", "uploader", "thumbnail_url",
                     "width", "height", "url"):
            value = getattr(other, name)
            if value not in (None, "", []):
                setattr(self, name, value)
        if other.products is not None:
            self.products = other.products

    def to_record(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k not in RUNTIME_FIELDS}

    @classmethod
    def from_record(cls, record: dict) -> VideoItem:
        known = {f.name for f in fields(cls)} - RUNTIME_FIELDS
        data = {k: v for k, v in record.items() if k in known}
        # Records written by the first CLI version used different keys.
        data.setdefault("url", record.get("original_url", ""))
        data.setdefault("duration", record.get("duration_seconds"))
        return cls(**data)

    @classmethod
    def from_info(cls, info: dict, platform: str, fallback_url: str = "") -> VideoItem:
        item = cls(platform=platform, video_id=str(info.get("id") or ""),
                   url=info.get("webpage_url") or info.get("url") or fallback_url)
        item.update_from_info(info)
        return item


def _date_from_timestamp(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y%m%d")
    except (OverflowError, OSError, ValueError):
        return ""


def pick_thumbnail(info: dict) -> str:
    """Choose a static, reasonably sized cover image URL."""
    thumbs = [t for t in info.get("thumbnails") or []
              if t.get("url") and "dynamic" not in str(t.get("id", "")).lower()]
    sized = [t for t in thumbs if t.get("width")]
    if sized:
        big_enough = [t for t in sized if t["width"] >= 360]
        choice = min(big_enough, key=lambda t: t["width"]) if big_enough else max(sized, key=lambda t: t["width"])
        return choice["url"]
    if thumbs:  # yt-dlp orders thumbnails worst -> best
        return thumbs[-1]["url"]
    return info.get("thumbnail") or ""


@dataclass
class Account:
    """A connected platform account (e.g. a YouTube channel) inside a profile."""
    profile_id: int
    platform: str
    external_id: str
    display_name: str
    handle: str = ""
    avatar_url: str = ""
    id: int | None = None
    connected_at: str = ""
    last_verified_at: str | None = None

    @property
    def token_key(self) -> str:
        return f"{self.platform.lower()}:{self.external_id}"

    @property
    def label(self) -> str:
        return f"{self.display_name} ({self.handle})" if self.handle else self.display_name


@dataclass
class Profile:
    """A creator workspace: its own library folder, accounts and board."""
    name: str
    output_folder: str
    color: str = "#8a8984"
    template: str = "{platform}_{date}_{id}"
    quality: str = "best"
    cookies_browser: str = ""
    last_source: str = ""
    id: int | None = None
    created_at: str = ""

    @property
    def initials(self) -> str:
        parts = [p for p in re.split(r"\s+", self.name.strip()) if p]
        return ("".join(p[0] for p in parts[:2]) or "?").upper()


@dataclass
class Post:
    """What to publish for one video: target account, metadata, schedule."""
    video_row: int
    account_id: int | None = None
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    privacy: str = Privacy.PUBLIC
    publish_at: str | None = None   # UTC, "YYYY-MM-DDTHH:MM:SSZ"; None = publish on upload
    made_for_kids: bool = False
    status: str = PostStatus.DRAFT
    remote_id: str = ""
    remote_url: str = ""
    error: str = ""
    id: int | None = None
    updated_at: str = ""
    target_platforms: list[str] = field(default_factory=lambda: [Platform.YOUTUBE])
    bluesky_text: str | None = None   # override for Bluesky (≤300 graphemes)
    threads_text: str | None = None   # override for Threads (≤500 characters)
    tiktok_text: str | None = None    # TikTok caption, for copy-paste (≤4,000 characters)
    instagram_text: str | None = None # Instagram caption, for copy-paste (≤2,200 characters, ≤30 hashtags)
    facebook_text: str | None = None  # Facebook post text (≤63,206); used as the Facebook description when posting
    hashtags: str | None = None       # one shared hashtag block, e.g. "#growth #healing"


@dataclass
class Dispatch:
    """One platform's delivery of one post."""
    video_row: int
    platform: str
    due_at: str                       # UTC; when Reelay should send it
    status: str = DispatchStatus.PENDING
    publish_at: str | None = None     # UTC; when it goes live (None = immediately)
    account_id: int | None = None
    attempts: int = 0
    next_attempt_at: str | None = None
    claimed_at: str | None = None
    claimed_by: str | None = None
    remote_id: str = ""
    remote_url: str = ""
    checkpoint: str = ""              # platform-specific resume marker (idempotency)
    options: dict = field(default_factory=dict)
    error: str = ""
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class Source:
    """A saved place to download from (a profile URL), kept per profile."""
    profile_id: int
    name: str
    platform: str                  # "tiktok" | "instagram" | "youtube"
    url: str
    mine: bool = False             # one of this profile's own accounts
    id: int | None = None
    last_fetched_at: str | None = None
    created_at: str = ""


class ContentType:
    SHORT = "short"
    LONG_FORM = "long-form"
    CLIP = "clip"
    OTHER = "other"
    ALL = (SHORT, LONG_FORM, CLIP, OTHER)
    LABELS = {SHORT: "Short", LONG_FORM: "Long-form", CLIP: "Clip", OTHER: "Other"}


# Platforms a ticket can be posted to by hand (copy and paste). Automatic
# posting is only possible to Platform.ALL.
MANUAL_PLATFORMS = ("youtube", "tiktok", "instagram", "bluesky", "threads", "facebook")
MANUAL_LABELS = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram", "bluesky": "Bluesky",
                 "threads": "Threads", "facebook": "Facebook"}


@dataclass
class Ticket:
    """The content ticket's own fields (schema v5). All optional.

    Dates are ISO 8601 UTC strings ("2026-10-02T15:00:00Z"). Provenance
    (source_platform, source_url, imported_at, authorization) is filled in when
    a video is imported; derived_from points at the video this one was cut from
    and becomes None if that video is deleted."""
    transcript: str | None = None
    thumbnail_path: str | None = None
    content_type: str | None = None        # ContentType
    topic: str | None = None
    keywords: str | None = None
    creator: str | None = None
    original_publish_date: str | None = None
    source_platform: str | None = None     # "tiktok" | "instagram" | "youtube"
    source_url: str | None = None
    imported_at: str | None = None
    authorization: str | None = None       # e.g. "Own content", "Managed account"
    derived_from: int | None = None


TICKET_FIELDS = tuple(f.name for f in fields(Ticket))


@dataclass
class Revision:
    """One logged change to a ticket field."""
    id: int
    video_row: int
    at: str
    origin: str          # "local" (the desktop app), "api" (an agent), …
    field: str
    old_value: str | None
    new_value: str | None


@dataclass
class ManualPost:
    """A post made by hand (copy-paste), recorded with "Mark as posted"."""
    video_row: int
    platform: str        # MANUAL_PLATFORMS
    posted_at: str       # UTC: when it went live (posted) or goes live (scheduled)
    url: str = ""
    kind: str = "posted"  # "posted" | "scheduled" (scheduled by hand in the platform's own app)
    note: str = ""
    origin: str = "local"
    id: int | None = None
    created_at: str = ""


@dataclass
class VideoRow:
    """A video as stored for a profile, with its board state."""
    id: int
    item: VideoItem
    stage: str = Stage.BACKLOG
    position: float = 0.0
    notes: str = ""
    profile_id: int = 0
    updated_at: str = ""
    ticket: Ticket = field(default_factory=Ticket)
    ready_gate_exempt: bool = False   # was already Scheduled/Published before the Ready gate existed
    archived_at: str | None = None    # hidden from the board and Download list (files and history kept)


@dataclass
class BrandProfile:
    """How a creator or brand sounds, for the local AI helper (Settings → Brand voice)."""
    profile_id: int
    name: str
    tone: str = ""        # e.g. "warm, encouraging, a little playful"
    avoid: str = ""       # words or phrases never to use, comma separated
    hashtags: str = ""    # default hashtags, e.g. "#livingood #healing"
    guidance: str = ""    # anything else: audience, example captions, emoji habits
    active: bool = False
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""
