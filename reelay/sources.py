"""Download sources: where a profile's videos come IN from.

Two kinds, shown together on the Download page and in GET /v1/sources:
- saved sources: a name + profile URL (TikTok, Instagram, YouTube), kept per
  profile; "mine" marks the profile's own accounts;
- account sources: each connected YouTube channel, listed through the
  YouTube Data API (no link needed). These are derived from Accounts and
  never stored twice.

The same URL may be saved more than once; the Download page merges
duplicates by URL when displaying. Nothing here touches publishing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .accounts import YOUTUBE, AccountService
from .handles import clean_handle, clean_text, handle_from_link
from .models import Account, Source, VideoItem
from .platforms import detect_platform, is_supported_url
from .store import Store, utcnow

SOURCE_PLATFORMS = ("tiktok", "instagram", "youtube")
NAME_MAX = 80


class SourceError(ValueError):
    """A source request the user needs to fix (unsupported URL, empty name…)."""


@dataclass
class SourceView:
    """One entry in the Download page's "Download from" list."""
    kind: str                       # "saved" | "account"
    name: str
    platform: str                   # "tiktok" | "instagram" | "youtube"
    mine: bool
    url: str | None = None          # saved sources (and the channel URL for accounts)
    source_id: int | None = None
    account: Account | None = None
    last_fetched_at: str | None = None
    duplicates: int = 0             # other saved entries with the same URL (hidden)

    @property
    def key(self) -> str:
        return f"account:{self.account.id}" if self.account else f"source:{self.source_id}"


def normalize_url(url: str) -> str:
    """Comparable form of a profile URL: scheme/host case, www./m., query,
    fragment and trailing slashes don't matter."""
    url = url.strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    host = re.sub(r"^(www|m|mobile)\.", "", host)
    path = re.sub(r"/+$", "", parts.path or "")
    if host.endswith(("tiktok.com", "instagram.com")) or re.match(r"^/@", path):
        path = path.lower()  # handles are case-insensitive; channel ids (/channel/UC…) are not
    return f"https://{host}{path}"


# A username typed instead of a link, per platform (the @ is optional).
HANDLES = {
    "tiktok": (re.compile(r"^[A-Za-z0-9_.]{2,24}$"), "https://www.tiktok.com/@{}"),
    "instagram": (re.compile(r"^[A-Za-z0-9_.]{1,30}$"), "https://www.instagram.com/{}/"),
    "youtube": (re.compile(r"^[A-Za-z0-9_.\-]{3,30}$"), "https://www.youtube.com/@{}"),
}
YOUTUBE_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_LOOKS_LIKE_LINK = re.compile(r"^(https?://|[\w-]+(\.[\w-]+)+/)", re.IGNORECASE)


def resolve_source_input(platform: str, text: str) -> str:
    """A username (``@khaby.lame``, ``khaby.lame``) or a profile link → the profile link.

    Links are passed through (their own platform wins); a username becomes ``platform``'s profile link."""
    text = clean_text(text)
    if not text:
        raise SourceError("Type a username or paste a profile link.")
    if _LOOKS_LIKE_LINK.match(text):
        return text if "://" in text else "https://" + text
    label = {"tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube"}.get(platform)
    if label is None:
        raise SourceError("Snapchat doesn't let apps list a profile's videos: paste the Spotlight links instead.")
    handle = clean_handle(text, platform)
    if platform == "youtube" and YOUTUBE_CHANNEL_ID.match(handle):
        return f"https://www.youtube.com/channel/{handle}"
    pattern, template = HANDLES[platform]
    if not pattern.match(handle):
        raise SourceError(f"“{text}” doesn't look like a {label} username. Check the spelling, or paste the "
                          "profile link.")
    return template.format(handle)


def source_platform(url: str) -> str:
    label = detect_platform(url)
    platform = label.lower()
    if platform not in SOURCE_PLATFORMS:
        raise SourceError("Only TikTok, Instagram and YouTube links can be saved as sources.")
    return platform


def default_name(url: str) -> str:
    """'https://www.tiktok.com/@livingood.lifestyle' → '@livingood.lifestyle on TikTok'."""
    url = url if "://" in url else "https://" + url.strip()
    label = detect_platform(url)
    path = [p for p in urlparse(url).path.split("/") if p]
    handle = next((p for p in path if p.startswith("@")), None)
    if not handle and path and label == "Instagram":
        handle = "@" + path[0]
    if not handle and len(path) >= 2 and path[0] in ("channel", "c", "user"):
        handle = path[1]
    return f"{handle} on {label}" if handle else f"{label} profile"


def channel_url(account: Account) -> str:
    return f"https://www.youtube.com/channel/{account.external_id}"


class SourceService:
    def __init__(self, store: Store, accounts: AccountService):
        self.store, self.accounts = store, accounts

    # ---------------------------------------------------------------- saved sources
    def add(self, profile_id: int, url: str, name: str | None = None, mine: bool = False,
            platform: str | None = None) -> Source:
        """Save a source. With ``platform``, ``url`` may also be a username on that platform."""
        url = (url or "").strip()
        if platform:
            url = resolve_source_input(platform, url)
        if not url:
            raise SourceError("Add a profile link.")
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        if not is_supported_url(url):
            raise SourceError("That isn't a TikTok, Instagram or YouTube link.")
        platform = source_platform(url)
        name = (name or "").strip() or default_name(url)
        if len(name) > NAME_MAX:
            raise SourceError(f"Names can be at most {NAME_MAX} characters.")
        if self.store.get_profile(profile_id) is None:
            raise SourceError("That profile no longer exists.")
        return self.store.add_source(Source(profile_id=profile_id, name=name, platform=platform, url=url,
                                            mine=bool(mine)))

    def rename(self, source_id: int, name: str) -> Source:
        source = self._get(source_id)
        name = (name or "").strip()
        if not name:
            raise SourceError("Names can't be empty.")
        if len(name) > NAME_MAX:
            raise SourceError(f"Names can be at most {NAME_MAX} characters.")
        source.name = name
        self.store.update_source(source)
        return source

    def set_mine(self, source_id: int, mine: bool) -> Source:
        source = self._get(source_id)
        source.mine = bool(mine)
        self.store.update_source(source)
        return source

    def remove(self, source_id: int) -> None:
        self._get(source_id)
        self.store.delete_source(source_id)

    def mark_fetched(self, source_id: int) -> None:
        source = self.store.get_source(source_id)
        if source is not None:
            source.last_fetched_at = utcnow()
            self.store.update_source(source)

    def _get(self, source_id: int) -> Source:
        source = self.store.get_source(source_id)
        if source is None:
            raise SourceError("That source no longer exists.")
        return source

    # ---------------------------------------------------------------- listing
    def all_sources(self, profile_id: int) -> list[SourceView]:
        """Every source, undeduplicated: connected YouTube channels, then saved ones."""
        views = [SourceView(kind="account", name=a.display_name, platform="youtube", mine=True, url=channel_url(a),
                            account=a, last_fetched_at=a.last_verified_at)
                 for a in self.store.list_accounts(profile_id, YOUTUBE)]
        views += [SourceView(kind="saved", name=s.name, platform=s.platform, mine=s.mine, url=s.url, source_id=s.id,
                             last_fetched_at=s.last_fetched_at) for s in self.store.list_sources(profile_id)]
        return views

    def display_sources(self, profile_id: int) -> list[SourceView]:
        """For the Download page: duplicates (same URL, or a saved link to a
        connected channel) merged into the first entry; own accounts first."""
        shown: list[SourceView] = []
        by_url: dict[str, SourceView] = {}
        # Accounts first, then saved sources oldest first: the first-saved name wins a duplicate.
        ordered = sorted(self.all_sources(profile_id),
                         key=lambda v: (v.kind != "account", v.source_id or 0))
        for view in ordered:
            keys = {normalize_url(view.url)} if view.url else set()
            if view.account and view.account.handle:
                keys.add(normalize_url(f"https://www.youtube.com/{view.account.handle}"))
            existing = next((by_url[k] for k in keys if k in by_url), None)
            if existing is not None:
                existing.duplicates += 1
                existing.mine = existing.mine or view.mine
                continue
            for k in keys:
                by_url[k] = view
            shown.append(view)
        return sorted(shown, key=lambda v: (not v.mine, v.kind != "account", v.name.casefold()))


# ---------------------------------------------------------------- YouTube Data API → VideoItems
_ISO_DURATION = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def iso_duration_seconds(value: str | None) -> float | None:
    """'PT1M3S' → 63.0."""
    m = _ISO_DURATION.fullmatch(value or "")
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    total = d * 86400 + h * 3600 + mi * 60 + s
    return float(total) if total else None  # P0D = live / not processed yet


# The VideoItem fields youtube_items() fills from the Data API. They're stored YouTube API data: refreshed or
# cleared within 30 days (youtube_retention.py). Everything else on a video is the creator's or Reelay's own.
API_VIDEO_FIELDS = ("title", "description", "upload_date", "duration", "view_count", "like_count",
                    "comment_count", "tags", "uploader", "thumbnail_url", "availability")


def youtube_items(videos: list[dict], fetched_at: str | None = None) -> list[VideoItem]:
    """Data API video resources (snippet, contentDetails, statistics, status) → VideoItems,
    stamped with when they were fetched and which fields came from the API."""
    fetched_at = fetched_at or utcnow()
    items = []
    for v in videos:
        sn, st, stats = v.get("snippet", {}), v.get("status", {}), v.get("statistics", {})
        thumbs = sn.get("thumbnails", {})
        thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
        privacy = st.get("privacyStatus", "")
        availability = "scheduled" if privacy == "private" and st.get("publishAt") else privacy
        published = (sn.get("publishedAt") or "")[:10].replace("-", "")

        def count(key):
            return int(stats[key]) if str(stats.get(key, "")).isdigit() else None

        items.append(VideoItem(
            platform="YouTube", video_id=v["id"], url=f"https://www.youtube.com/watch?v={v['id']}",
            title=sn.get("title", ""), description=sn.get("description", ""), upload_date=published,
            duration=iso_duration_seconds(v.get("contentDetails", {}).get("duration")),
            view_count=count("viewCount"), like_count=count("likeCount"), comment_count=count("commentCount"),
            tags=sn.get("tags") or [], uploader=sn.get("channelTitle", ""), thumbnail_url=thumb,
            availability=availability))
        item = items[-1]
        item.api_fields = [f for f in API_VIDEO_FIELDS if getattr(item, f) not in (None, "", [])]
        item.api_fetched_at = fetched_at if item.api_fields else ""
        item.api_channel_id = sn.get("channelId", "") if item.api_fields else ""
    return items


def downloadable_by_default(item: VideoItem) -> bool:
    """Private and scheduled YouTube videos need the owner's login cookies to download."""
    return item.availability not in ("private", "scheduled")


def is_bare_handle(text: str) -> bool:
    """'@name' (or 'name') rather than a link: something with no slash and no web address in it."""
    text = clean_text(text)
    if not text or "/" in text:
        return False
    if text[0] in "@＠":
        return True
    return not is_supported_url("https://" + text)


def sources_for_handle(views: list[SourceView], text: str) -> list[SourceView]:
    """The saved sources whose profile link has this username, in the order they're listed."""
    wanted = clean_handle(text).lower()
    if not wanted:
        return []
    return [v for v in views if v.url and handle_from_link(v.url, v.platform).lower() == wanted]
