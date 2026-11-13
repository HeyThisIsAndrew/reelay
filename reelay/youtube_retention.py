"""Stored YouTube API data never outlives 30 days without a refresh.

The YouTube API Services Developer Policies (III.E.4.b to d) let an app keep data it got from the YouTube
Data API for at most 30 calendar days; after that it must refresh it or delete it, and statistics kept
longer must be re-checked every 30 days (still authorized, video not deleted). When a channel's access is
revoked, its data must go too (III.D.2.c). docs/youtube-data-retention.md says which stored fields count.

What Reelay stores from the Data API, and what this module does about it:

- A connected channel's name, handle and picture (``accounts`` rows, stamped ``api_fetched_at``).
- The fields of a video listed through the Data API (``sources.API_VIDEO_FIELDS``: title, description,
  publish date, length, view/like/comment counts, tags, channel name, thumbnail link, privacy), stamped
  per video with ``api_fetched_at`` and the fields that still hold API values (``api_fields``). A field
  later filled from somewhere else (yt-dlp, the creator) stops counting. The cover image cached from the
  thumbnail link goes with them.

On launch, after every change to the connected accounts, and once a day while Reelay runs, ``run_once``:

1. refreshes anything fetched ``REFRESH_AFTER`` (27 days) ago or more through the API, with the sign-in of
   the channel it came from, when that channel is still connected;
2. clears it at once when its channel isn't connected any more (disconnected in Reelay, or its sign-in was
   revoked or expired), or when the video is gone from YouTube;
3. when a refresh fails for another reason (offline, quota), keeps it and tries again the next day, unless
   it's ``CLEAR_AFTER`` (29 days) old: then it's cleared, because the next check would be past 30 days.

Clearing removes only the API fields. The creator's own work stays: files, custom names, notes, ticket
fields they changed, and post drafts (copy they write to publish, even when it started from a title).
Nothing here sends anything anywhere except the read-only API calls that refresh the data.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import MISSING, dataclass, field, fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .models import Account, VideoItem, VideoRow
from .paths import cover_cache_file
from .sources import API_VIDEO_FIELDS, youtube_items
from .store import ReadOnlyError, Store
from .youtube import ReauthRequired, YouTubeError

log = logging.getLogger("reelay")

MAX_AGE = timedelta(days=30)            # the policy's limit
CHECK_EVERY = timedelta(days=1)         # while Reelay is running
REFRESH_AFTER = timedelta(days=27)      # a few days early, so a day or two offline doesn't lose anything
CLEAR_AFTER = MAX_AGE - CHECK_EVERY     # still not refreshed: clear now, the next check would be too late
CLEARED_CHANNEL_NAME = "YouTube channel"
_NEVER = timedelta.max

_DEFAULTS = {f.name: f.default_factory() if f.default_factory is not MISSING else f.default
             for f in fields(VideoItem) if f.name in API_VIDEO_FIELDS}


@dataclass
class RetentionReport:
    refreshed_videos: int = 0
    cleared_videos: int = 0
    refreshed_channels: int = 0
    cleared_channels: int = 0
    waiting: int = 0                     # due, couldn't be refreshed yet, not near the limit: tried tomorrow
    keys: list[str] = field(default_factory=list)   # videos whose API data changed (their covers were dropped)

    @property
    def changed(self) -> bool:
        return bool(self.refreshed_videos or self.cleared_videos or self.refreshed_channels
                    or self.cleared_channels)


def age(stamp: str | None, now: datetime) -> timedelta:
    """How old an ``api_fetched_at`` stamp is; unknown or unreadable counts as too old."""
    if not stamp:
        return _NEVER
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return _NEVER
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return now - when


def cleared(item: VideoItem) -> VideoItem:
    """``item`` without any of its YouTube API data."""
    out = replace(item, tags=list(item.tags))
    for name in item.api_fields:
        if name in _DEFAULTS:
            setattr(out, name, _DEFAULTS[name])
    out.api_fields, out.api_fetched_at, out.api_channel_id = [], "", ""
    return out


def refreshed(item: VideoItem, resource: dict, now: str) -> VideoItem:
    """``item`` with its API fields replaced by a fresh Data API video resource. Fields that hold values
    from elsewhere (yt-dlp, the creator) are kept; empty ones are filled from the API."""
    fresh = youtube_items([resource], fetched_at=now)[0]
    out = replace(item, tags=list(item.tags))
    api = set()
    for name in API_VIDEO_FIELDS:
        value = getattr(fresh, name)
        if name in item.api_fields or getattr(item, name) in (None, "", []):
            setattr(out, name, value)
            if value not in (None, "", []):
                api.add(name)
    out.api_fields = sorted(api)
    out.api_fetched_at = now if api else ""
    out.api_channel_id = fresh.api_channel_id if api else ""
    return out


class YouTubeRetention:
    """Runs the 30-day pass at start, when kicked and every ``CHECK_EVERY`` on a background thread."""

    def __init__(self, store: Store, accounts, covers_dir: Path | None = None,
                 on_changed: Callable[[RetentionReport], None] | None = None,
                 update_library: Callable[[int, VideoItem], None] | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.store, self.accounts, self.covers_dir = store, accounts, covers_dir
        self.on_changed = on_changed
        self.update_library = update_library            # rewrite a downloaded video's sidecar and manifest
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._wake, self._stop = threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None
        self._run_lock = threading.Lock()

    # ---------------------------------------------------------------- thread
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="reelay-youtube-retention", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
            self._thread = None

    def kick(self) -> None:
        """Check now (e.g. a channel was just disconnected)."""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 — try again at the next check; never take the app down
                log.exception("YouTube data refresh failed")
            self._wake.wait(CHECK_EVERY.total_seconds())

    # ---------------------------------------------------------------- one pass
    def run_once(self) -> RetentionReport:
        report = RetentionReport()
        if self.store.read_only:
            return report        # the copy of Reelay that has the database for editing does this
        with self._run_lock:
            now = self.clock()
            try:
                clients: dict[str, object] = {}
                self._channels(now, report, clients)
                self._videos(now, report, clients)
            except ReadOnlyError:
                return report
        if report.changed:
            log.info("YouTube data: refreshed %d videos and %d channels, cleared %d videos and %d channels.",
                     report.refreshed_videos, report.refreshed_channels, report.cleared_videos,
                     report.cleared_channels)
            if self.on_changed:
                self.on_changed(report)
        return report

    def _client(self, account: Account, clients: dict):
        """The channel's API client, or the exception that says it can't be used (cached per pass)."""
        key = account.token_key
        if key not in clients:
            try:
                clients[key] = self.accounts.client_for(account)
            except YouTubeError as exc:          # signed out, or its Google app isn't set up any more
                clients[key] = ReauthRequired(str(exc))
            except Exception as exc:  # noqa: BLE001 — a locked keychain: try again later
                clients[key] = exc
        return clients[key]

    # ---------------------------------------------------------------- channels
    def _channels(self, now: datetime, report: RetentionReport, clients: dict) -> None:
        stamp = _iso(now)
        for account in self.store.youtube_accounts():
            account_age = age(account.api_fetched_at, now)
            if account_age < REFRESH_AFTER:
                continue
            already_clear = not account.api_fetched_at and account.display_name == CLEARED_CHANNEL_NAME \
                and not account.handle and not account.avatar_url
            client = self._client(account, clients)
            definitive = isinstance(client, ReauthRequired)
            channel = None
            if not isinstance(client, Exception):
                try:
                    channel = client.my_channel()
                except ReauthRequired:
                    clients[account.token_key] = ReauthRequired("signed out")
                    definitive = True
                except (YouTubeError, requests.RequestException, OSError, KeyError, ValueError) as exc:
                    log.info("Couldn't refresh %s's YouTube details: %s", account.token_key, exc)
            if channel is not None and channel.get("id") == account.external_id:
                self.store.set_account_api_data(account.id, channel.get("title") or account.external_id,
                                                channel.get("handle", ""), channel.get("thumbnail", ""), stamp)
                report.refreshed_channels += 1
            elif channel is not None or definitive or account_age >= CLEAR_AFTER:
                if not already_clear:
                    self.store.set_account_api_data(account.id, CLEARED_CHANNEL_NAME, "", "", None)
                    report.cleared_channels += 1
            else:
                report.waiting += 1

    # ---------------------------------------------------------------- videos
    def _videos(self, now: datetime, report: RetentionReport, clients: dict) -> None:
        rows = self.store.videos_with_youtube_api_data()
        if not rows:
            return
        accounts = self.store.youtube_accounts()
        stamp = _iso(now)
        work: dict[int, tuple[VideoRow, list[Account]]] = {}
        for row in rows:
            candidates = _candidates(row, accounts)
            if not candidates:                                  # its channel isn't connected here any more
                self._clear(row, report)
            elif age(row.item.api_fetched_at, now) >= REFRESH_AFTER:
                work[row.id] = (row, candidates)

        unsure: set[int] = set()                                # a refresh failed for a passing reason
        while work:
            batches: dict[str, tuple[Account, list[VideoRow]]] = {}
            for row, candidates in work.values():
                account = candidates[0]
                batches.setdefault(account.token_key, (account, []))[1].append(row)
            for account, batch in batches.values():
                resources = self._fetch(account, [r.item.video_id for r in batch], clients, unsure, batch)
                for row in batch:
                    resource = (resources or {}).get(row.item.video_id)
                    if resource and resource.get("snippet", {}).get("channelId") == account.external_id:
                        self._refresh(row, resource, stamp, report)
                        work.pop(row.id)
                        continue
                    work[row.id][1].pop(0)                      # try the next connected channel, if any
                    if not work[row.id][1]:
                        work.pop(row.id)
                        if row.id in unsure and age(row.item.api_fetched_at, now) < CLEAR_AFTER:
                            report.waiting += 1
                        else:
                            self._clear(row, report)

    def _fetch(self, account: Account, ids: list[str], clients: dict, unsure: set[int],
               batch: list[VideoRow]) -> dict | None:
        """{video id: resource} from this channel, or None when it couldn't be asked."""
        client = self._client(account, clients)
        if isinstance(client, ReauthRequired):
            return None
        if isinstance(client, Exception):
            unsure.update(r.id for r in batch)
            return None
        try:
            return client.videos_by_id(ids)
        except ReauthRequired:
            clients[account.token_key] = ReauthRequired("signed out")
            return None
        except (YouTubeError, requests.RequestException, OSError, ValueError) as exc:
            log.info("Couldn't refresh YouTube videos for %s: %s", account.token_key, exc)
            clients[account.token_key] = exc
            unsure.update(r.id for r in batch)
            return None

    def _refresh(self, row: VideoRow, resource: dict, stamp: str, report: RetentionReport) -> None:
        def update(current: VideoItem) -> VideoItem | None:
            if current.api_fetched_at != row.item.api_fetched_at:
                return None                                     # listed again meanwhile: already fresh
            return refreshed(current, resource, stamp)
        item = self.store.replace_youtube_api_data(row.id, update)
        if item is not None:
            report.refreshed_videos += 1
            self._after(row, item, report)

    def _clear(self, row: VideoRow, report: RetentionReport) -> None:
        def update(current: VideoItem) -> VideoItem | None:
            if current.api_fetched_at != row.item.api_fetched_at or not current.api_fields:
                return None
            return cleared(current)
        item = self.store.replace_youtube_api_data(row.id, update)
        if item is not None:
            report.cleared_videos += 1
            self._after(row, item, report)

    def _after(self, row: VideoRow, item: VideoItem, report: RetentionReport) -> None:
        report.keys.append(item.key)
        if self.covers_dir is not None:                         # the cover was the API's thumbnail
            try:
                cover_cache_file(self.covers_dir, item.key).unlink(missing_ok=True)
            except OSError as exc:
                log.warning("Couldn't remove the cached cover for %s: %s", item.key, exc)
        if item.filename and self.update_library:
            try:
                self.update_library(row.profile_id, item)
            except Exception:  # noqa: BLE001 — the database is right; the sidecar catches up next save
                log.exception("Couldn't update the library files for %s", item.key)


def _candidates(row: VideoRow, accounts: list[Account]) -> list[Account]:
    """The connected channels that may refresh this video: its own channel in its profile (any of the
    profile's channels when an older record doesn't say which), one per sign-in."""
    channel = row.item.api_channel_id
    mine = [a for a in accounts if a.profile_id == row.profile_id and (not channel or a.external_id == channel)]
    seen, out = set(), []
    for account in mine:
        if account.token_key not in seen:
            seen.add(account.token_key)
            out.append(account)
    return out


def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
