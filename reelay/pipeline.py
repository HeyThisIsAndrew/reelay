"""The content pipeline: every rule for moving videos through
Backlog → Staging → Scheduled → Published, editing posts, scheduling to
YouTube / Bluesky / Threads / Facebook, and syncing.

One place for the rules, used by the desktop Board, the agent API and the
dispatcher. No Qt and no dialogs here: callers confirm with the user first,
then call in. Problems come back as `PipelineError`s with a stable `code`
that the API maps to HTTP statuses.

Scheduling model (per platform, tracked as Dispatch rows):
- YouTube and Facebook schedule natively: Reelay hands the video over right
  away with the publish time, and the platform makes it live on its own.
  YouTube goes through the app-wide UploadManager (one upload at a time).
- Bluesky and Threads have no native scheduling: the dispatcher posts them
  when they're due.
A post's own status and the card's stage summarise its platforms' statuses;
a failure on one platform never changes another platform's status.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from functools import cached_property
from pathlib import Path
from typing import Callable, Protocol

from .accounts import YOUTUBE, AccountService
from .library import Library
from .media import MediaLinks, public_base_url
from .models import (MANUAL_PLATFORMS, TICKET_FIELDS, Account, ContentType, Dispatch,
                     DispatchStatus, ManualPost, Platform, Post, PostStatus, Profile, Stage, Ticket, VideoItem,
                     VideoRow)
from .platform_text import COPY_FIELDS, resolve_text, text_problem
from .columns import is_custom, is_stage, rank
from .readiness import Readiness, evaluate
from .publishers.base import PermanentError, PublishJob
from .publishing import (DESCRIPTION_MAX_BYTES, MIN_SCHEDULE_LEAD, TAGS_MAX_CHARS, TITLE_MAX, build_resource,
                         default_post, humanize_name, parse_utc, reconcile, validate_post)
from .store import Store
from .text import local_text, short_title
from .youtube import YouTubeError, watch_url

log = logging.getLogger("reelay")

LOCKED_MESSAGE = "Scheduled and published videos are managed on their platforms; they can't be moved back."
COPY_TEXT_FIELDS = ("tiktok_text", "instagram_text", "bluesky_text", "threads_text", "facebook_text", "hashtags")
POST_FIELDS = ("title", "description", "tags", "target_platforms", *COPY_TEXT_FIELDS)
# Ticket fields people and agents may edit (provenance included: it can be corrected;
# imported_at is set by Reelay when the video comes in).
EDITABLE_TICKET_FIELDS = tuple(f for f in TICKET_FIELDS if f != "imported_at")
EDITABLE_FIELDS = (*POST_FIELDS, *EDITABLE_TICKET_FIELDS)
TICKET_TEXT_MAX = {"transcript": 200_000, "thumbnail_path": 1024, "topic": 200, "keywords": 1000, "creator": 200,
                   "source_url": 2048, "authorization": 200}
DATE_FIELDS = ("original_publish_date",)
FACEBOOK_MAX_LEAD = timedelta(days=182)
THREADS_MAX_SECONDS = 300
HEARTBEAT_SECONDS = 30
MAX_UPLOAD_ATTEMPTS = 6
INTERRUPTED_MESSAGE = ("Reelay stopped while sending this (it was closed, crashed or lost power), so it may or may "
                       "not have been posted. Check the platform, then choose Try Again, It Was Posted, or Cancel.")
MISSED_MESSAGE = ("Its time passed while Reelay wasn't running, so it would now go out late. Choose Post Now, "
                  "It Was Posted, or Cancel.")
UNSURE_MESSAGE = ("The connection dropped just as the upload finished, so YouTube may already have the video. "
                  "Check YouTube Studio, then choose Try Again, It Was Posted, or Cancel.")
PAUSED_MESSAGE = "Paused when Reelay closed; it continues when Reelay is open again."


class PipelineError(Exception):
    """A request the pipeline refuses. `code` is stable; `message` is for people.

    Codes: not_found, invalid_post, invalid_field, invalid_stage, invalid_time,
    invalid_platform, platform_disabled, locked, needs_upload, already_scheduled,
    already_uploading, ticket_not_ready."""

    def __init__(self, message: str, code: str = "invalid", problems: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.problems = problems or [message]


class UploadQueue(Protocol):
    completion_handler: object

    def enqueue(self, row_id: int, client, path: Path, resource: dict) -> None: ...
    def cancel(self, row_id: int) -> bool: ...
    def progress_of(self, row_id: int) -> float | None: ...
    def is_queued(self, row_id: int) -> bool: ...


@dataclass
class Card:
    """A video as shown on the board."""
    row: VideoRow
    post: Post | None = None
    account: Account | None = None
    progress: float | None = None   # upload percent while uploading
    queued: bool = False
    dispatches: list[Dispatch] = field(default_factory=list)
    readiness: Readiness | None = None
    platform_states: dict = field(default_factory=dict)   # platform → PlatformState
    search_hit: object | None = None   # store.SearchHit when the board is searching

    @property
    def id(self) -> int:
        return self.row.id

    @property
    def title(self) -> str:
        if self.post and self.post.title:
            return self.post.title
        return humanize_name(self.row.item.custom_name) or short_title(self.row.item.hook, 80)

    @property
    def uploading(self) -> bool:
        return bool(self.post and self.post.status == PostStatus.UPLOADING)

    @cached_property
    def coverage(self) -> dict:
        """platform → coverage.Coverage: published / scheduled / ready / needs work / not applicable."""
        from .coverage import compute
        return compute(self.post, self.platform_states, self.readiness, self.row.item.is_downloaded,
                       {d.platform for d in self.dispatches if d.status == DispatchStatus.INTERRUPTED})


@dataclass
class SyncPlan:
    posts: list[Post]
    jobs: list[tuple[object, list[str]]]   # (YouTube client, remote video ids)
    problems: list[str]
    dispatches: dict[str, Dispatch] = field(default_factory=dict)   # remote id → YouTube dispatch


@dataclass
class HistoryEntry:
    """One line of a ticket's publishing history: what went where, when, how."""
    platform: str
    method: str              # "automatic" (Reelay posted it) | "manual" (marked as posted)
    status: str              # dispatch status, or "posted" for manual posts
    at: str | None           # when it went (or goes) live; for pending posts, when it's due
    url: str = ""
    error: str = ""
    note: str = ""
    origin: str = ""
    id: int | None = None    # dispatch id or manual post id


NONE, FAILED, SCHEDULED, SENDING, PUBLISHED = "none", "failed", "scheduled", "sending", "published"
_RANK = {NONE: 0, FAILED: 1, SCHEDULED: 2, SENDING: 3, PUBLISHED: 4}


@dataclass
class PlatformState:
    """One platform's progress on a ticket (the ticket's tag)."""
    platform: str
    target: bool
    state: str = NONE
    at: str | None = None            # when it goes or went live
    automatic: bool = False          # Reelay is posting it
    manual_kind: str | None = None   # "scheduled" | "posted" when marked by hand

    def offer(self, state: str, at: str | None, automatic: bool, manual_kind: str | None = None) -> None:
        if _RANK[state] >= _RANK[self.state]:
            self.state, self.at = state, at
            self.automatic = automatic or (self.automatic and state != PUBLISHED)
            self.manual_kind = manual_kind if not automatic else self.manual_kind


@dataclass
class ScheduleResult:
    post: Post
    dispatches: list[Dispatch]
    manual: list[str] = field(default_factory=list)   # targets left for copy-paste (posting turned off)


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Pipeline:
    def __init__(self, store: Store, accounts: AccountService, uploads: UploadQueue,
                 instance_id: str | None = None, media: MediaLinks | None = None,
                 enabled: frozenset[str] | None = None):
        """`enabled`: platforms automatic posting may use (YouTube is always on).
        Services passes the set it built the dispatcher with; when omitted it is
        read from the flags (env / settings) on each call."""
        self.store = store
        self._enabled = frozenset(enabled) | {Platform.YOUTUBE} if enabled is not None else None
        self.accounts = accounts
        self.uploads = uploads
        self.media = media or MediaLinks()
        self.instance_id = instance_id or uuid.uuid4().hex
        self.on_scheduled: Callable[[], None] | None = None   # e.g. wake the dispatcher
        self._last_heartbeat: dict[int, float] = {}
        uploads.completion_handler = self._on_upload_finished
        uploads.progress_handler = self._on_upload_progress
        uploads.pause_handler = self.pause_upload
        try:     # the first time this computer opens this database, its folders are the ones it may post from
            from .trusted_folders import first_open
            first_open(store)
        except Exception:  # noqa: BLE001 — never block opening the app; untrusted folders are just refused
            log.exception("Couldn't record this database's folders as trusted")

    # ================================================================ reads
    def _row(self, row_id: int) -> VideoRow:
        row = self.store.get_video(row_id)
        if row is None:
            raise PipelineError("That video no longer exists.", "not_found")
        return row

    def _profile(self, profile_id: int) -> Profile:
        profile = self.store.get_profile(profile_id)
        if profile is None:
            raise PipelineError("That profile no longer exists.", "not_found")
        return profile

    def enabled_platforms(self) -> frozenset[str]:
        from .flags import enabled_platforms
        return self._enabled if self._enabled is not None else enabled_platforms(self.store)

    def auto_targets(self, post: Post) -> tuple[list[str], list[str]]:
        """(platforms Reelay will post to, platforms left for copy-paste) for a post."""
        enabled = self.enabled_platforms()
        targets = list(dict.fromkeys(post.target_platforms or [Platform.YOUTUBE]))
        return [p for p in targets if p in enabled], [p for p in targets if p not in enabled]

    def youtube_accounts(self, profile_id: int) -> list[Account]:
        return self.store.list_accounts(profile_id, YOUTUBE)

    def add_shorts_default(self) -> bool:
        return self.store.get_setting("add_shorts", "1") == "1"

    def add_shorts_default_for(self, row: VideoRow) -> bool:
        """The #Shorts default for one video: only for Shorts (vertical, 3 minutes or less)."""
        from .readiness import is_short_form
        item = row.item
        return self.add_shorts_default() and is_short_form(item.width, item.height, item.duration)

    def video_path(self, row: VideoRow) -> Path | None:
        """The file to post: the edited version from the ticket's Video tools, else the download.

        Whatever the database says, only a real video inside this video's folders (the profile's folder,
        or the folder it was downloaded into) is ever posted or served; anything else counts as missing."""
        from .urlsafety import media_file_problem
        from .video_tools import edited_path
        roots = self.media_roots(row)
        edited = edited_path(self.store, row.id)
        if edited is not None:
            problem = media_file_problem(edited, roots)
            if problem is None:
                return edited
            log.warning("Ignoring the edited version of video %s: %s", row.id, problem)
        original = self.original_path(row)
        if original is not None and os.path.lexists(original):
            problem = media_file_problem(original, roots)
            if problem:
                log.warning("Not using the file recorded for video %s: %s", row.id, problem)
                return None
        return original

    def media_roots(self, row: VideoRow) -> list[str]:
        """The folders a video's files may live in: the profile's folder and the one it was downloaded to,
        but only those this computer trusts (trusted_folders.py; the database alone can't add one)."""
        from .trusted_folders import is_trusted
        return [f for f in self._candidate_roots(row) if is_trusted(f)]

    def _candidate_roots(self, row: VideoRow) -> list[str]:
        return [f for f in (self._profile(row.profile_id).output_folder, row.item.folder) if f]

    def file_problem(self, row: VideoRow) -> str:
        """Why there's no file to post for this video, in plain words."""
        from .trusted_folders import is_trusted
        untrusted = [f for f in self._candidate_roots(row) if not is_trusted(f)]
        original = self.original_path(row)
        if untrusted and original is not None and os.path.lexists(original):
            return (f"Reelay won't post from {untrusted[0]}: that folder wasn't chosen on this computer. Choose it "
                    "in the Library's Download Settings (Where videos go) if it's right.")
        return "The video file is missing. Download it again first."

    def original_path(self, row: VideoRow) -> Path | None:
        return row.item.path(self._profile(row.profile_id).output_folder)

    def post_or_default(self, row: VideoRow) -> tuple[Post, bool]:
        """(post, saved): the stored post, or the pre-filled draft it would get."""
        post = self.store.get_post(row.id)
        if post is not None:
            return post, True
        accounts = self.youtube_accounts(row.profile_id)
        return default_post(row.id, row.item, accounts[0].id if accounts else None), False

    def board(self, profile_id: int) -> dict[str, list[Card]]:
        """Cards per stage, in board order. Fetched-but-not-downloaded videos are
        left out of the Backlog (they live on the Download page)."""
        profile = self._profile(profile_id)
        accounts = {a.id: a for a in self.store.list_accounts(profile_id)}
        columns: dict[str, list[Card]] = defaultdict(list, {stage: [] for stage in Stage.ALL})
        for row in self.store.load_videos(profile_id):
            row.item.refresh_download_status(profile.output_folder)
            if row.stage == Stage.BACKLOG and not row.item.is_downloaded:
                continue
            columns[row.stage].append(self._card(row, accounts))
        return columns

    def card(self, row_id: int) -> Card:
        row = self._row(row_id)
        row.item.refresh_download_status(self._profile(row.profile_id).output_folder)
        return self._card(row, {a.id: a for a in self.store.list_accounts(row.profile_id)})

    def _card(self, row: VideoRow, accounts: dict[int, Account]) -> Card:
        post = self.store.get_post(row.id)
        return Card(row=row, post=post, account=accounts.get(post.account_id) if post else None,
                    progress=self.uploads.progress_of(row.id), queued=self.uploads.is_queued(row.id),
                    dispatches=self.store.dispatches_for(row.id), readiness=self._readiness(row, post),
                    platform_states=self.platform_states(row.id))

    # ================================================================ readiness
    def _readiness(self, row: VideoRow, post: Post | None = None, targets: list[str] | None = None) -> Readiness:
        post = post or self.post_or_default(row)[0]
        return evaluate(post, self.enabled_platforms(), self.video_path(row), row.item.duration, targets)

    def readiness(self, row_id: int, post: Post | None = None) -> Readiness:
        """Per-platform checklist for the ticket (as saved, or as `post` is edited)."""
        return self._readiness(self._row(row_id), post)

    # ================================================================ moving cards
    def ensure_post(self, row_id: int) -> Post:
        """The video's post, creating a pre-filled draft if it has none."""
        post, saved = self.post_or_default(self._row(row_id))
        return post if saved else self.store.save_post(post)

    def move(self, row_id: int, target: str, position: int | None = None) -> None:
        """Move a card to another column, or reorder it within its column.

        Moving to Scheduled isn't a move: it's scheduling (see schedule()).
        Moving to Published means "posted outside Reelay"; confirm first."""
        if not is_stage(target):
            raise PipelineError(f"Unknown stage “{target}”.", "invalid_stage")
        row = self._row(row_id)
        if row.stage == target:
            if position is not None:
                self._reorder(row, position)
            return
        post = self.store.get_post(row_id)
        if row.stage in (Stage.SCHEDULED, Stage.PUBLISHED) or (post and post.status == PostStatus.UPLOADING):
            raise PipelineError(LOCKED_MESSAGE, "locked")
        if target == Stage.SCHEDULED:
            raise PipelineError("Schedule the video to move it to Scheduled.", "needs_upload")
        if target == Stage.STAGING or is_custom(target):
            self.ensure_post(row_id)
        self.store.set_stage([row_id], target)
        if position is not None:
            self._reorder(self._row(row_id), position)

    def _reorder(self, row: VideoRow, position: int) -> None:
        order = [c.id for c in self.board(row.profile_id)[row.stage] if c.id != row.id]
        order.insert(max(0, min(position, len(order))), row.id)
        self.store.reorder(order)

    def prepare_upload(self, row_id: int) -> Post:
        """Get a card ready to schedule: make sure it has a draft and is in Staging."""
        post = self.ensure_post(row_id)
        if self._row(row_id).stage == Stage.BACKLOG:
            self.store.set_stage([row_id], Stage.STAGING)
        return post

    # ================================================================ editing posts
    def save_post(self, post: Post) -> Post:
        """Persist edits without changing status or stage."""
        self._row(post.video_row)
        return self.store.save_post(post)

    def save_draft(self, post: Post) -> Post:
        """Validate every copy block, then save as a draft; a Backlog card moves to
        Staging. Raises PipelineError(invalid_post) and saves nothing if any block
        is over its platform's limit (text is never shortened)."""
        problems = metadata_problems({"title": post.title, "description": post.description, "tags": post.tags,
                                      **{f: getattr(post, f) for f in COPY_TEXT_FIELDS}})
        if problems:
            raise PipelineError("Some copy is over its platform's limit; nothing was saved.", "invalid_post",
                                problems)
        post = self.store.save_post(replace(post, status=PostStatus.DRAFT, error=""))
        if self._row(post.video_row).stage == Stage.BACKLOG:
            self.store.set_stage([post.video_row], Stage.STAGING)
        return post

    def update_metadata(self, row_id: int, changes: dict) -> Post:
        """Apply a partial edit: post copy (title, description, tags,
        target_platforms, per-platform text, hashtags) and/or ticket fields
        (transcript, topic, provenance…). Everything is validated before anything
        is written; one transaction per call, logged as revisions. Raises
        PipelineError."""
        unknown = sorted(set(changes) - set(EDITABLE_FIELDS))
        if unknown:
            raise PipelineError(f"These fields can't be edited: {', '.join(unknown)}.", "invalid_field")
        row = self._row(row_id)
        post_changes = {k: v for k, v in changes.items() if k in POST_FIELDS}
        ticket_changes = {k: v for k, v in changes.items() if k in EDITABLE_TICKET_FIELDS}
        sent = {d.platform for d in self.store.dispatches_for(row_id)
                if d.status in DispatchStatus.ACTIVE + (DispatchStatus.PUBLISHED,)}
        locked_copy = {k for k in post_changes if k != "target_platforms" or not sent <= set(post_changes[k] or [])}
        if locked_copy and (sent or (row.stage == Stage.PUBLISHED and not self._hand_managed(row_id))):
            raise PipelineError("This video is already scheduled or published; its details were sent to the "
                                "platforms and can't be changed here.", "locked")
        post, _saved = self.post_or_default(row)
        problems = metadata_problems(post_changes) + self.ticket_problems(row, ticket_changes)
        if problems:
            raise PipelineError("Those changes aren't valid.", "invalid_post", problems)
        updates = dict(post_changes)
        if "tags" in updates:
            updates["tags"] = [t.strip().lstrip("#") for t in updates["tags"]]
        if "target_platforms" in updates:
            updates["target_platforms"] = list(dict.fromkeys(updates["target_platforms"]))
        edited = replace(post, **updates) if updates else None
        self.store.update_ticket(row_id, normalize_ticket(ticket_changes), post=edited)
        return self.store.get_post(row_id) or post

    def update_ticket(self, row_id: int, changes: dict) -> Ticket:
        """Edit ticket fields only (allowed at any stage: they aren't sent to platforms)."""
        unknown = sorted(set(changes) - set(EDITABLE_TICKET_FIELDS))
        if unknown:
            raise PipelineError(f"These fields can't be edited: {', '.join(unknown)}.", "invalid_field")
        row = self._row(row_id)
        problems = self.ticket_problems(row, changes)
        if problems:
            raise PipelineError("Those changes aren't valid.", "invalid_post", problems)
        return self.store.update_ticket(row_id, normalize_ticket(changes))

    def ticket_problems(self, row: VideoRow, changes: dict) -> list[str]:
        problems = []
        for key, value in changes.items():
            if value is None:
                continue
            if key == "derived_from":
                if not isinstance(value, int) or isinstance(value, bool):
                    problems.append("derived_from must be a video id or null.")
                elif value == row.id:
                    problems.append("A video can't be derived from itself.")
                else:
                    parent = self.store.get_video(value)
                    if parent is None or parent.profile_id != row.profile_id:
                        problems.append(f"derived_from: no video {value} in this profile.")
                continue
            if not isinstance(value, str):
                problems.append(f"{key} must be text or null.")
            elif key == "content_type" and value not in ContentType.ALL:
                problems.append(f"content_type must be one of: {', '.join(ContentType.ALL)}.")
            elif key in DATE_FIELDS and _parse_aware(value) is None and value.strip():
                problems.append(f"{key} must be an ISO 8601 date and time with a timezone, "
                                "e.g. 2026-10-02T15:00:00Z.")
            elif key in TICKET_TEXT_MAX and len(value) > TICKET_TEXT_MAX[key]:
                problems.append(f"{key} is {len(value):,} characters; the limit is {TICKET_TEXT_MAX[key]:,}.")
        return problems

    # ================================================================ publishing history
    def mark_posted(self, row_id: int, platform: str, posted_at: str | datetime | None = None, url: str = "",
                    note: str = "", now: datetime | None = None) -> ManualPost:
        """Record that the video was posted to `platform` by hand (copy-paste)."""
        now = now or datetime.now(timezone.utc)
        self._row(row_id)
        if platform not in MANUAL_PLATFORMS:
            raise PipelineError(f"Unknown platform “{platform}”. Use one of: {', '.join(MANUAL_PLATFORMS)}.",
                                "invalid_platform")
        when = now if posted_at is None else _parse_aware(posted_at)
        if when is None:
            raise PipelineError("posted_at must be an ISO 8601 time with a timezone.", "invalid_time")
        if when > now + timedelta(minutes=5):
            raise PipelineError("posted_at is in the future; mark it as posted once it's live.", "invalid_time")
        url = (url or "").strip()
        if url and not url.startswith(("http://", "https://")):
            raise PipelineError("url must start with http:// or https://.", "invalid_post")
        return self.store.add_manual_post(ManualPost(video_row=row_id, platform=platform, posted_at=utc_iso(when),
                                                     url=url, note=(note or "").strip()[:500]))

    # ================================================================ archive, bulk, restore
    def _active(self, row_id: int) -> bool:
        post = self.store.get_post(row_id)
        return (any(d.status in DispatchStatus.ACTIVE for d in self.store.dispatches_for(row_id))
                or bool(post and post.status == PostStatus.UPLOADING))

    def archive(self, row_ids: list[int], archived: bool = True) -> None:
        """Hide videos from the board and Download list, or bring them back. Files,
        tickets and history are kept. Videos waiting to post can't be archived."""
        rows = [self._row(r) for r in row_ids]
        busy = [r.id for r in rows if archived and self._active(r.id)]
        if busy:
            raise PipelineError("Videos that are scheduled or uploading can't be archived; cancel them first.",
                                "locked", [f"Video {r}: scheduled or uploading." for r in busy])
        self.store.set_archived([r.id for r in rows], archived)

    def archived(self, profile_id: int) -> list[VideoRow]:
        return self.store.load_videos(profile_id, archived=True)

    def _plan_edit(self, row: VideoRow, changes: dict) -> tuple[dict, Post | None, list[str]]:
        """Validate one ticket's edit: (ticket changes, edited post or None, problems)."""
        unknown = sorted(set(changes) - set(EDITABLE_FIELDS))
        if unknown:
            return {}, None, [f"These fields can't be edited: {', '.join(unknown)}."]
        post_changes = {k: v for k, v in changes.items() if k in POST_FIELDS}
        ticket_changes = {k: v for k, v in changes.items() if k in EDITABLE_TICKET_FIELDS}
        problems = []
        if post_changes and self._copy_locked(row):
            problems.append("Already scheduled or published; its copy can't change.")
        problems += metadata_problems(post_changes) + self.ticket_problems(row, ticket_changes)
        if problems:
            return {}, None, problems
        if "tags" in post_changes:
            post_changes["tags"] = [t.strip().lstrip("#") for t in post_changes["tags"]]
        if "target_platforms" in post_changes:
            post_changes["target_platforms"] = list(dict.fromkeys(post_changes["target_platforms"]))
        post = replace(self.post_or_default(row)[0], **post_changes) if post_changes else None
        return normalize_ticket(ticket_changes), post, []

    def _copy_locked(self, row: VideoRow) -> bool:
        return row.stage == Stage.PUBLISHED or any(
            d.status in DispatchStatus.ACTIVE + (DispatchStatus.PUBLISHED,) for d in self.store.dispatches_for(row.id))

    def bulk_update(self, row_ids: list[int], changes: dict) -> int:
        """The same edit on many tickets, all or nothing, each change logged.
        Returns how many tickets changed."""
        if not row_ids or not changes:
            raise PipelineError("Choose some videos and at least one field.", "invalid_request")
        edits, problems = [], []
        for row_id in dict.fromkeys(row_ids):
            row = self._row(row_id)
            ticket_changes, post, row_problems = self._plan_edit(row, dict(changes))
            problems += [f"{short_title(row.item.hook, 40)}: {p}" for p in row_problems]
            edits.append((row_id, ticket_changes, post))
        if problems:
            raise PipelineError("Nothing was changed.", "invalid_post", problems)
        self.store.update_many(edits)
        return len(edits)

    def fill_from_original(self, row_ids: list[int]) -> dict[int, list[str]]:
        """Fill each ticket's EMPTY copy (and creator) from its original post's
        caption, within every platform's limits. One transaction; logged.
        Copy of scheduled or published videos is left alone. Returns the fields
        filled per video."""
        from .autofill import suggest
        edits, filled = [], {}
        for row_id in dict.fromkeys(row_ids):
            row = self._row(row_id)
            post, _saved = self.post_or_default(row)
            post_changes, ticket_changes = suggest(post, row.item, row.ticket)
            brand = self.store.active_brand_profile(row.profile_id)
            if brand and brand.hashtags.strip() and not (post.hashtags or "").strip() \
                    and not post_changes.get("hashtags"):
                post_changes["hashtags"] = brand.hashtags.strip()        # the brand voice's default hashtags
            if self._copy_locked(row):
                post_changes = {}
            if post_changes or ticket_changes or not _saved:
                edits.append((row_id, ticket_changes, replace(post, **post_changes)))
                filled[row_id] = [*post_changes, *ticket_changes]
        if edits:
            self.store.update_many(edits)
        return filled

    def restore_revision(self, row_id: int, revision_id: int) -> None:
        """Put a field back to the value it had before a logged change (a new,
        logged change itself)."""
        import json as _json
        revision = next((r for r in self.store.revisions(row_id, limit=100_000) if r.id == revision_id), None)
        if revision is None:
            raise PipelineError("That change isn't in this ticket's history.", "not_found")
        value = revision.old_value
        if revision.field == "archived":
            self.archive([row_id], archived=value is not None)
            return
        if revision.field in ("tags", "target_platforms"):
            value = _json.loads(value) if value else []
            if revision.field == "target_platforms" and not value:
                raise PipelineError("A ticket needs at least one platform; nothing was changed.", "invalid_post")
        elif revision.field == "derived_from":
            value = int(value) if value else None
        elif revision.field in ("title", "description"):
            value = value or ""
        self.update_metadata(row_id, {revision.field: value})

    # ================================================================ transcripts & sheets
    def fetch_transcript(self, row_id: int, ydl_opts: dict, replace_existing: bool = False, fetch=None) -> str:
        """Get the transcript from the original post's captions (network) and
        save it. Raises PipelineError. Keeps an existing transcript unless
        replace_existing."""
        from .transcripts import TranscriptError, fetch_transcript
        row = self._row(row_id)
        if row.ticket.transcript and not replace_existing:
            raise PipelineError("This ticket already has a transcript.", "invalid_post")
        url = row.ticket.source_url or row.item.url
        if not url:
            raise PipelineError("This video has no original link to get captions from.", "invalid_post")
        try:
            text, _track = (fetch or fetch_transcript)(url, ydl_opts)
        except TranscriptError as exc:
            raise PipelineError(str(exc), "no_transcript") from None
        self.update_ticket(row_id, {"transcript": text})
        return text

    def plan_sheet(self, profile_id: int, headers: list[str], rows: list[list[str]],
                   mapping: dict[str, str | None], overwrite: bool = False):
        from .sheet_import import plan_import
        self._profile(profile_id)
        videos = self.store.load_videos(profile_id)
        # Unsaved suggestions (from the caption) count as empty: the sheet wins over them.
        posts = {v.id: self.store.get_post(v.id) or Post(video_row=v.id, target_platforms=[]) for v in videos}
        locked = {v.id for v in videos if self._copy_locked(v)}

        from .sheet_import import LABELS

        def validate(video, post_changes, ticket_changes):
            problems = metadata_problems(post_changes) + self.ticket_problems(video, ticket_changes)
            for key, label in LABELS.items():   # people see the sheet's words, not field names
                problems = [p.replace(f"{key}: ", f"{label}: ", 1) if p.startswith(f"{key}: ") else p
                            for p in problems]
            return problems
        return plan_import(headers, rows, mapping, videos, posts, locked, overwrite, validate)

    def apply_sheet(self, plan) -> int:
        """Write a planned import: every changing row in one transaction."""
        edits = []
        for row in plan.changing:
            video = self._row(row.video_id)
            post = replace(self.post_or_default(video)[0], **row.post_changes) if row.post_changes else None
            if post is not None and "tags" in row.post_changes:
                post.tags = [t.strip().lstrip("#") for t in post.tags]
            edits.append((row.video_id, normalize_ticket(row.ticket_changes), post))
        if edits:
            self.store.update_many(edits)
        return len(edits)

    def unmark_posted(self, manual_id: int) -> None:
        video_row = self.store.delete_manual_post(manual_id)     # under the store's lock
        if video_row is not None:
            self.advance(video_row, allow_back=True)

    # ================================================================ per-platform tags & automatic stages
    def platform_states(self, row_id: int, now: datetime | None = None) -> dict[str, PlatformState]:
        """For every platform: is it a target, and how far has it got (none,
        failed, scheduled, sending, published)? Reelay's own posts and the ones
        marked by hand are combined; a hand-scheduled post counts as published
        once its time has passed."""
        now_iso = utc_iso(now or datetime.now(timezone.utc))
        row = self._row(row_id)
        post = self.store.get_post(row_id)
        targets = set(post.target_platforms) if post else set()
        states = {p: PlatformState(p, p in targets) for p in MANUAL_PLATFORMS}
        word = {DispatchStatus.PENDING: SCHEDULED, DispatchStatus.SCHEDULED: SCHEDULED,
                DispatchStatus.DISPATCHING: SENDING, DispatchStatus.PUBLISHED: PUBLISHED, DispatchStatus.FAILED: FAILED,
                DispatchStatus.INTERRUPTED: FAILED}   # needs a person, like a failure (coverage says why)
        for d in self.store.dispatches_for(row_id):
            if d.platform in states:
                states[d.platform].offer(word.get(d.status, NONE), d.publish_at or d.due_at, automatic=True)
        for m in self.store.manual_posts(row_id):
            if m.platform in states:
                live = m.kind == "posted" or m.posted_at <= now_iso
                states[m.platform].offer(PUBLISHED if live else SCHEDULED, m.posted_at, automatic=False,
                                         manual_kind=m.kind)
        del row
        return states

    def _hand_managed(self, row_id: int) -> bool:
        """The card's stage follows hand-made records (no Reelay posts at all)."""
        return not self.store.dispatches_for(row_id) and bool(self.store.manual_posts(row_id))

    def set_targets(self, row_id: int, platforms: list[str]) -> Post:
        """Which platforms this video is going to (any of the six)."""
        post = self.update_metadata(row_id, {"target_platforms": list(platforms)})
        self.advance(row_id, allow_back=True)
        return post

    def confirm_handoff(self, row_id: int, platform: str, url: str = "", when: str | datetime | None = None,
                        scheduled: bool = False, now: datetime | None = None) -> None:
        """Finish a Prepare to Publish hand-off: the person posted (or scheduled) it in the
        platform's own app and pasted the live link. Recorded like the ticket's Posted /
        Scheduled choice, with the link, and the card moves on its own."""
        from .handoff import check_post_url
        problem = check_post_url(platform, url)
        if problem:
            raise PipelineError(problem, "invalid_post")
        self.set_platform_state(row_id, platform, "scheduled" if scheduled else "posted", when, now=now,
                                url=(url or "").strip(), note="Prepare to Publish")

    def set_platform_state(self, row_id: int, platform: str, state: str, when: str | datetime | None = None,
                           now: datetime | None = None, url: str = "", note: str = "") -> None:
        """The ticket's checkboxes. state: "scheduled" (scheduled by hand in the
        platform's app, going live at `when`), "posted" (live, at `when` or now)
        or "none" (clear what was marked by hand). Platforms Reelay is posting
        itself can't be changed here. The card then moves on its own: Scheduled
        when every target is scheduled or live, Published when every one is live."""
        now = now or datetime.now(timezone.utc)
        row = self._row(row_id)
        if platform not in MANUAL_PLATFORMS:
            raise PipelineError(f"Unknown platform “{platform}”.", "invalid_platform")
        if state not in ("scheduled", "posted", "none"):
            raise PipelineError("state must be scheduled, posted or none.", "invalid_request")
        if any(d.platform == platform for d in self.store.dispatches_for(row_id)):
            raise PipelineError(f"Reelay is posting this to {Platform.LABELS[platform]} itself; its status "
                                "updates on its own.", "locked")
        if state == "none":
            self.store.replace_manual_posts(row_id, platform, ("scheduled", "posted"))
            self.advance(row_id, allow_back=True)
            return
        when_dt = now if when is None else _parse_aware(when)
        if when_dt is None:
            raise PipelineError("The time must be an ISO 8601 time with a timezone.", "invalid_time")
        if state == "posted" and when_dt > now + timedelta(minutes=5):
            raise PipelineError("That time is in the future: mark it as scheduled instead.", "invalid_time")
        post, saved = self.post_or_default(row)
        if platform not in post.target_platforms or not saved:   # ticking a platform makes it a target
            targets = list(dict.fromkeys([*post.target_platforms, platform])) if saved else [platform]
            self.update_metadata(row_id, {"target_platforms": targets})
        self.store.replace_manual_posts(
            row_id, platform, ("scheduled", "posted"),
            ManualPost(video_row=row_id, platform=platform, posted_at=utc_iso(when_dt), kind=state, url=url,
                       note=note))
        self.advance(row_id, allow_back=True, now=now)   # a new target can mean "not everywhere yet"

    def _stage_for(self, row: VideoRow, now: datetime) -> str | None:
        """Where the card belongs by its platforms' progress, or None to leave it."""
        post = self.store.get_post(row.id)
        if post is None or not post.target_platforms:
            return None
        dispatches = self.store.dispatches_for(row.id)
        manual = self.store.manual_posts(row.id)
        if not dispatches and not manual:
            return None                       # nothing marked yet: leave it where it is
        states = self.platform_states(row.id, now)
        targets = [states[p] for p in post.target_platforms if p in states]
        if all(s.state == PUBLISHED for s in targets):
            return Stage.PUBLISHED
        if all(s.state in (SCHEDULED, SENDING, PUBLISHED) for s in targets):
            return Stage.SCHEDULED
        if dispatches:
            return None                       # Reelay's own posts keep their stage rules (refresh_post)
        return Stage.STAGING

    def advance(self, row_id: int, allow_back: bool = False, now: datetime | None = None) -> str | None:
        """Move the card to the column its platforms say. Forward only, unless
        allow_back (a checkbox was cleared). Returns the new stage if it moved."""
        row = self._row(row_id)
        if self._active_upload(row_id):
            return None
        target = self._stage_for(row, now or datetime.now(timezone.utc))
        if target is None or target == row.stage or (is_custom(row.stage) and target == Stage.STAGING):
            return None                   # your own working columns count as Staging
        if rank(target) < rank(row.stage) and not allow_back:
            return None
        self.store.set_stage([row_id], target)
        return target

    def _active_upload(self, row_id: int) -> bool:
        post = self.store.get_post(row_id)
        return bool(post and post.status == PostStatus.UPLOADING)

    def settle_due(self, now: datetime | None = None) -> int:
        """Called by the dispatcher every minute, app open or headless:
        - Facebook schedules natively, so once a scheduled Facebook post's time
          has passed it's live; mark it published (YouTube is checked with
          YouTube instead, because unaudited projects stay private).
        - Cards whose hand-scheduled posts have gone live move on.
        Returns how many cards moved."""
        now = now or datetime.now(timezone.utc)
        now_iso = utc_iso(now)
        for profile in self.store.list_profiles():
            for d in self.store.dispatches_with_status(profile.id, Platform.FACEBOOK, [DispatchStatus.SCHEDULED]):
                if d.publish_at and d.publish_at <= now_iso:
                    self.store.finish_dispatch(d.id, DispatchStatus.PUBLISHED)
                    self.refresh_post(d.video_row)
        return sum(len(self.advance_stages(p.id, now)) for p in self.store.list_profiles())

    def advance_stages(self, profile_id: int, now: datetime | None = None) -> list[int]:
        """Time passes: hand-scheduled posts whose time has come count as live.
        Moves every card that's due; returns the ids moved."""
        moved = []
        for row in self.store.load_videos(profile_id):
            if row.stage != Stage.PUBLISHED and self.advance(row.id, now=now):
                moved.append(row.id)
        return moved

    def publishing_history(self, row_id: int) -> dict[str, list[HistoryEntry]]:
        """Every platform's publishing history for one ticket, automatic and
        manual together, oldest first. Platforms with nothing yet are omitted."""
        self._row(row_id)
        history: dict[str, list[HistoryEntry]] = {}
        for d in self.store.dispatches_for(row_id):
            at = d.publish_at or (d.updated_at if d.status == DispatchStatus.PUBLISHED else d.due_at)
            history.setdefault(d.platform, []).append(HistoryEntry(
                platform=d.platform, method="automatic", status=d.status, at=at, url=d.remote_url, error=d.error,
                id=d.id))
        now = utc_iso(datetime.now(timezone.utc))
        for m in self.store.manual_posts(row_id):
            status = "scheduled" if m.kind == "scheduled" and m.posted_at > now else "posted"
            history.setdefault(m.platform, []).append(HistoryEntry(
                platform=m.platform, method="manual", status=status, at=m.posted_at, url=m.url, note=m.note,
                origin=m.origin, id=m.id))
        for entries in history.values():
            entries.sort(key=lambda e: e.at or "")
        return {p: history[p] for p in MANUAL_PLATFORMS if p in history}

    # ================================================================ scheduling
    def upload_problems(self, post: Post) -> list[str]:
        """Everything stopping this post from being scheduled to its auto-post platforms."""
        row = self._row(post.video_row)
        targets, _manual = self.auto_targets(post)
        if not targets:
            return ["Choose a platform to post to automatically."]
        problems = []
        busy = self._busy_platforms(row, targets)
        if busy:
            problems.append(f"Already scheduled or published on {', '.join(Platform.LABELS[p] for p in busy)}.")
        return problems + self.schedule_problems(post, targets, row)

    def schedule_problems(self, post: Post, platforms: list[str], row: VideoRow | None = None,
                          now: datetime | None = None) -> list[str]:
        now = now or datetime.now(timezone.utc)
        row = row or self._row(post.video_row)
        path = self.video_path(row)
        problems: list[str] = []

        def add(message: str) -> None:
            if message not in problems:
                problems.append(message)

        if not platforms:
            add("Choose at least one platform.")
        for platform in platforms:
            if platform not in Platform.ALL:
                add(f"Unknown platform “{platform}”.")
        if path is None or not path.is_file():
            add(self.file_problem(row))
        when = parse_utc(post.publish_at)
        if post.publish_at and when is None:
            add("The scheduled time isn't valid.")
        elif when is not None and when < now + MIN_SCHEDULE_LEAD:
            add("Schedule at least 10 minutes from now.")

        for platform in platforms:
            label = Platform.LABELS.get(platform, platform)
            if platform == Platform.YOUTUBE:
                for problem in validate_post(post, path, now):
                    add(problem)
                if post.account_id is not None:
                    account = self.store.get_account(post.account_id)
                    if account is None:
                        add("That YouTube channel is no longer connected.")
                    else:
                        try:
                            self.accounts.client_for(account)
                        except YouTubeError as exc:
                            add(str(exc))
                continue
            if platform not in Platform.ALL:
                continue
            account = self.accounts.account_for(row.profile_id, platform)
            if account is None:
                add(f"Connect a {label} account in Accounts first.")
            elif not self.accounts.has_credentials(account):
                add(f"{label}: the sign-in is missing from the keychain. Reconnect it in Accounts.")
            if platform in (Platform.BLUESKY, Platform.THREADS):
                try:
                    resolve_text(post, platform)
                except ValueError as exc:
                    add(str(exc))
            if platform == Platform.THREADS:
                if not public_base_url(self.store):
                    add("Threads needs a public link to the video: set REELAY_PUBLIC_URL (or Settings → Public "
                        "URL) to your Cloudflare Tunnel address.")
                if row.item.duration and row.item.duration > THREADS_MAX_SECONDS:
                    add("Threads videos can be at most 5 minutes long.")
            if platform == Platform.FACEBOOK:
                if not (post.title.strip() or post.description.strip() or (post.facebook_text or "").strip()):
                    add("Facebook needs a title or description.")
                if when is not None and when > now + FACEBOOK_MAX_LEAD:
                    add("Facebook can only schedule up to 6 months ahead.")
        return problems

    def _busy_platforms(self, row: VideoRow, platforms: list[str]) -> list[str]:
        existing = {d.platform: d for d in self.store.dispatches_for(row.id)}
        return [p for p in platforms if p in existing and
                existing[p].status in DispatchStatus.ACTIVE + (DispatchStatus.PUBLISHED,)]

    def describe_upload(self, post: Post) -> str:
        """One line for a confirmation prompt."""
        names = []
        targets, _manual = self.auto_targets(post)
        for platform in targets:
            if platform == Platform.YOUTUBE:
                account = self.store.get_account(post.account_id) if post.account_id else None
                names.append(f"YouTube ({account.display_name})" if account else "YouTube")
            else:
                names.append(Platform.LABELS.get(platform, platform))
        when = (f"goes live {local_text(post.publish_at)}" if post.publish_at
                else f"is published immediately{' as ' + post.privacy.capitalize() if Platform.YOUTUBE in post.target_platforms else ''}")
        return f"Post “{post.title}” to {', '.join(names)}? It {when}."

    def schedule(self, row_id: int, scheduled_at: str | datetime | None = None, platforms: list[str] | None = None,
                 add_shorts: bool | None = None, post: Post | None = None,
                 now: datetime | None = None, force: bool = False) -> ScheduleResult:
        """Schedule (or publish now) a video to its target platforms.

        scheduled_at: timezone-aware time; omitted → the card's saved time if it
        has one, else publish as soon as possible. platforms: overrides the
        card's target_platforms for this request.

        The Ready gate: if any platform being scheduled isn't ready (see
        readiness.py), nothing is scheduled and PipelineError(ticket_not_ready)
        lists the problems, unless force=True. Cards that were already Scheduled
        or Published before the gate existed are exempt. Raises PipelineError."""
        now = now or datetime.now(timezone.utc)
        if self.store.current_origin().split(":")[0] == "automation":
            # The approval boundary for Automations: copy that came from an AI agent (directly, or
            # through a rule's template) is only posted once a person approves it.
            from .rules import agent_written_copy
            if agent_written_copy(self.store, row_id):
                raise PipelineError("This copy came from an AI agent, so an automation can't schedule it "
                                    "without a person's approval.", "needs_approval")
        row, base, targets, manual = self.schedule_plan(row_id, scheduled_at, platforms, post, now, force)
        return self._schedule_write(row, base, targets, manual, add_shorts, now)

    def schedule_plan(self, row_id: int, scheduled_at: str | datetime | None = None,
                      platforms: list[str] | None = None, post: Post | None = None,
                      now: datetime | None = None, force: bool = False) -> tuple[VideoRow, Post, list[str], list[str]]:
        """Every check schedule() makes (times, platforms, the Ready gate), without scheduling anything.
        Raises the same PipelineErrors. Writes nothing unless `post` (typed but unsaved text) is given."""
        now = now or datetime.now(timezone.utc)
        row = self._row(row_id)
        base, _saved = (post, True) if post is not None else self.post_or_default(row)
        if scheduled_at is not None:
            when = _parse_aware(scheduled_at)
            if when is None:
                raise PipelineError("scheduled_at must be an ISO 8601 time with a timezone.", "invalid_time")
            if when <= now:
                raise PipelineError("scheduled_at is in the past.", "invalid_time")
            base = replace(base, publish_at=utc_iso(when))
        enabled = self.enabled_platforms()
        manual: list[str] = []
        if platforms:
            targets = list(dict.fromkeys(platforms))
            unknown = [p for p in targets if p not in MANUAL_PLATFORMS]
            if unknown:
                raise PipelineError(f"Unknown platform(s): {', '.join(unknown)}.", "invalid_platform")
            off = [p for p in targets if p not in enabled]
            if off:
                names = ", ".join(Platform.LABELS[p] for p in off)
                raise PipelineError(f"Automatic posting to {names} is not enabled (platform not enabled). "
                                    "Post it manually from the card's copy, or turn it on with "
                                    + " / ".join(f"REELAY_PUBLISH_{p.upper()}=1" for p in off) + ".",
                                    "platform_disabled")
        else:
            targets, manual = self.auto_targets(base)
            if not targets:
                names = ", ".join(Platform.LABELS[p] for p in manual) or "none"
                raise PipelineError(f"None of this video's platforms are enabled for automatic posting "
                                    f"(targets: {names}). Post them manually from the card's copy.",
                                    "platform_disabled")

        if row.stage == Stage.PUBLISHED and not self.store.dispatches_for(row_id):
            raise PipelineError("This video is already marked as published.", "already_scheduled")
        busy = self._busy_platforms(row, targets)
        if busy:
            raise PipelineError(f"Already scheduled or published on "
                                f"{', '.join(Platform.LABELS[p] for p in busy)}.", "already_scheduled")
        if self.uploads.progress_of(row_id) is not None:
            raise PipelineError("This video is already uploading.", "already_uploading")

        gated = not force and not row.ready_gate_exempt
        readiness = self._readiness(row, base, targets)
        not_ready = readiness.problems(targets) if gated else []
        problems = self.schedule_problems(base, targets, row, now)
        if problems or not_ready:
            current = self.store.get_post(row_id)
            if post is not None and (current is None or current.status in (PostStatus.DRAFT, PostStatus.FAILED)):
                self.store.save_post(replace(post, status=PostStatus.DRAFT))  # keep what was typed
        if problems:   # force can't help with these: report everything at once
            raise PipelineError("This post can't be scheduled yet.", "invalid_post",
                                list(dict.fromkeys(problems + not_ready)))
        if not_ready:
            names = ", ".join(Platform.LABELS[p] for p in readiness.blocking)
            raise PipelineError(f"This ticket isn't ready for {names}. Fix the problems listed, or schedule "
                                "anyway with force.", "ticket_not_ready", not_ready)
        return row, base, targets, manual

    def _schedule_write(self, row: VideoRow, base: Post, targets: list[str], manual: list[str],
                        add_shorts: bool | None, now: datetime) -> ScheduleResult:
        row_id = row.id
        now_iso = utc_iso(now)
        add_shorts = self.add_shorts_default_for(row) if add_shorts is None else add_shorts
        dispatches = []
        for platform in targets:
            account = (self.store.get_account(base.account_id) if platform == Platform.YOUTUBE
                       else self.accounts.account_for(row.profile_id, platform))
            native = platform in Platform.NATIVE_SCHEDULING
            dispatches.append(Dispatch(
                video_row=row_id, platform=platform, account_id=account.id if account else None,
                due_at=now_iso if (native or not base.publish_at) else base.publish_at,
                publish_at=base.publish_at,
                options={"add_shorts": add_shorts} if platform == Platform.YOUTUBE else {}))
        status = PostStatus.UPLOADING if Platform.YOUTUBE in targets else PostStatus.SCHEDULED
        saved = replace(base, status=status, error="")
        rows = self.store.schedule_post(saved, dispatches, Stage.SCHEDULED,
                                        claim=[Platform.YOUTUBE] if Platform.YOUTUBE in targets else [],
                                        claimed_by=self.instance_id)
        youtube = next((d for d in rows if d.platform == Platform.YOUTUBE), None)
        if youtube is not None:
            try:
                self.enqueue_youtube(youtube)
            except PermanentError as exc:
                self.store.finish_dispatch(youtube.id, DispatchStatus.FAILED, error=str(exc))
                self.refresh_post(row_id)
        if self.on_scheduled:
            self.on_scheduled()
        return ScheduleResult(self.store.get_post(row_id), self.store.dispatches_for(row_id), manual)

    def start_upload(self, post: Post, add_shorts: bool | None = None, force: bool = False) -> Post:
        """The desktop Board's Schedule button: schedule the post as edited to its
        enabled platforms (the rest stay copy-paste tickets)."""
        return self.schedule(post.video_row, add_shorts=add_shorts, post=post, force=force).post

    def queue_position(self, dispatch: Dispatch) -> int | None:
        """YouTube: place in the upload queue (0 = uploading now). Others: place
        among waiting dispatches by due time (0 = next). None when not queued."""
        if dispatch.platform == Platform.YOUTUBE:
            position = getattr(self.uploads, "queue_position", None)
            return position(dispatch.video_row) if position else None
        if dispatch.status not in (DispatchStatus.PENDING, DispatchStatus.DISPATCHING):
            return None
        waiting = [d for d in self.store.pending_dispatches() if d.platform != Platform.YOUTUBE]
        return next((n for n, d in enumerate(waiting) if d.id == dispatch.id), None)

    def cancel_upload(self, row_id: int) -> bool:
        return self.uploads.cancel(row_id)

    # ================================================================ YouTube via UploadManager
    def enqueue_youtube(self, dispatch: Dispatch) -> None:
        """Hand a claimed YouTube dispatch to the app-wide upload queue."""
        row = self.store.get_video(dispatch.video_row)
        post = self.store.get_post(dispatch.video_row)
        if row is None or post is None:
            raise PermanentError("The video or its post was deleted.")
        account = (self.store.get_account(dispatch.account_id) if dispatch.account_id else None) or \
            self.accounts.account_for(row.profile_id, Platform.YOUTUBE, post.account_id)
        if account is None:
            raise PermanentError("No YouTube channel is connected for this post.")
        path = self.video_path(row)
        if path is None or not path.is_file():
            raise PermanentError(self.file_problem(row))
        try:
            client = self.accounts.client_for(account)
        except YouTubeError as exc:
            raise PermanentError(str(exc)) from None
        resource = build_resource(replace(post, publish_at=dispatch.publish_at),
                                  dispatch.options.get("add_shorts", self.add_shorts_default_for(row)))
        cover = row.ticket.thumbnail_path if row.ticket else None      # the ticket's cover → YouTube thumbnail
        try:
            if cover:
                self.uploads.enqueue(row.id, client, path, resource, thumbnail=Path(cover))
            else:
                self.uploads.enqueue(row.id, client, path, resource)
        except ValueError:
            pass  # already in the queue (e.g. resumed after a restart)

    def complete_upload(self, row_id: int, resource: dict) -> Post | None:
        dispatch = self._youtube_dispatch(row_id)
        remote_id = resource.get("id", "")
        if dispatch is None:  # uploaded outside the dispatch model; nothing to track
            return self.store.get_post(row_id)
        when = parse_utc(dispatch.publish_at)
        live_later = when is not None and when > datetime.now(timezone.utc)
        self.store.finish_dispatch(dispatch.id, DispatchStatus.SCHEDULED if live_later else DispatchStatus.PUBLISHED,
                                   remote_id=remote_id, remote_url=watch_url(remote_id) if remote_id else "")
        if dispatch.account_id:
            self.store.mark_verified(dispatch.account_id)
        return self.refresh_post(row_id)

    def fail_upload(self, row_id: int, reason: str) -> Post | None:
        dispatch = self._youtube_dispatch(row_id)
        if dispatch is not None:
            self.store.finish_dispatch(dispatch.id, DispatchStatus.FAILED, error=reason)
        return self.refresh_post(row_id)

    def _youtube_dispatch(self, row_id: int) -> Dispatch | None:
        return next((d for d in self.store.dispatches_for(row_id) if d.platform == Platform.YOUTUBE), None)

    def _on_upload_finished(self, row_id: int, ok: bool, payload) -> str:
        self._last_heartbeat.pop(row_id, None)
        if ok:
            post = self.complete_upload(row_id, payload)
            if post is None:
                return "Upload finished."
            when = f" It goes live {local_text(post.publish_at)}." if post.publish_at else ""
            note = payload.get("cover_note") if isinstance(payload, dict) else None
            cover = "" if note is None else (" Its cover is the thumbnail." if not note else f" {note}")
            return f"Uploaded “{post.title}” to YouTube.{when}{cover}"
        dispatch = self._youtube_dispatch(row_id)
        if dispatch is not None and getattr(payload, "uncertain", False):
            self.store.interrupt_dispatch(dispatch.id, UNSURE_MESSAGE)    # never re-upload blindly
            self.refresh_post(row_id)
            return "YouTube upload interrupted at the very end. Open the ticket to say whether it's on YouTube."
        if dispatch is not None and getattr(payload, "transient", False) and dispatch.attempts + 1 < MAX_UPLOAD_ATTEMPTS:
            wait = min(60 * 2 ** dispatch.attempts, 3600)   # nothing reached YouTube in full: safe to retry
            self.store.retry_dispatch(dispatch.id, f"{payload} Retrying.",
                                      utc_iso(datetime.now(timezone.utc) + timedelta(seconds=wait)))
            self.refresh_post(row_id)
            return f"YouTube upload stopped ({payload}). Reelay tries again in {wait // 60} min."
        self.fail_upload(row_id, str(payload))
        return f"YouTube upload failed: {payload}"

    def pause_upload(self, row_id: int, uncertain: bool = False) -> None:
        """Reelay is closing mid-upload: record it BEFORE the upload is stopped. A clean stop
        can't have posted anything, so it continues next time; if the last piece was already
        on its way, YouTube may have the video, so a person decides."""
        dispatch = self._youtube_dispatch(row_id)
        if dispatch is None or dispatch.status != DispatchStatus.DISPATCHING:
            return
        if uncertain:
            self.store.interrupt_dispatch(dispatch.id, UNSURE_MESSAGE)
        else:
            self.store.pause_dispatch(dispatch.id, PAUSED_MESSAGE)
        self.refresh_post(row_id)

    # ================================================================ interrupted sends
    def interrupted(self, profile_id: int | None = None) -> list[Dispatch]:
        """Sends waiting for a person: cut off mid-send, or missed while Reelay was closed."""
        return self.store.interrupted_dispatches(profile_id)

    def resolve_interrupted(self, dispatch_id: int, action: str, url: str = "",
                            now: datetime | None = None) -> Dispatch:
        """What the person decided. action: "retry" (send it now; any time already passed is
        dropped so it goes live right away), "posted" (it's there: record it, with its link
        if given), or "cancel" (don't send it)."""
        now = now or datetime.now(timezone.utc)
        dispatch = self.store.get_dispatch(dispatch_id)
        if dispatch is None:
            raise PipelineError("That send no longer exists.", "not_found")
        if dispatch.status != DispatchStatus.INTERRUPTED:
            raise PipelineError("That send isn't waiting for a decision any more.", "conflict")
        label = Platform.LABELS.get(dispatch.platform, dispatch.platform)
        if action == "retry":
            publish = parse_utc(dispatch.publish_at)
            keep = dispatch.publish_at if publish and publish > now + timedelta(minutes=15) else None
            done = self.store.requeue_dispatch(dispatch_id, utc_iso(now), keep)
        elif action == "posted":
            from .handoff import check_post_url
            url = (url or "").strip()
            problem = check_post_url(dispatch.platform, url)
            if problem:
                raise PipelineError(problem, "invalid_post")
            publish = parse_utc(dispatch.publish_at)
            held_by_platform = dispatch.platform in Platform.NATIVE_SCHEDULING and publish and publish > now
            status = DispatchStatus.SCHEDULED if held_by_platform else DispatchStatus.PUBLISHED
            done = self.store.finish_dispatch(dispatch_id, status, remote_url=url,
                                              remote_id=url.rstrip("/").rsplit("/", 1)[-1] if url else "")
        elif action == "cancel":
            done = self.store.finish_dispatch(dispatch_id, DispatchStatus.FAILED,
                                              error=f"Cancelled after an interruption; nothing more was sent to "
                                                    f"{label}.")
        else:
            raise PipelineError("action must be retry, posted or cancel.", "invalid_request")
        self.refresh_post(dispatch.video_row)
        if action == "retry" and self.on_scheduled:
            self.on_scheduled()
        return done

    def _on_upload_progress(self, row_id: int, _pct: float) -> None:
        """Keep the YouTube claim fresh during long uploads (stale-claim recovery)."""
        now = time.monotonic()
        if now - self._last_heartbeat.get(row_id, 0.0) < HEARTBEAT_SECONDS:
            return
        self._last_heartbeat[row_id] = now
        dispatch = self._youtube_dispatch(row_id)
        if dispatch is not None:
            self.store.heartbeat(dispatch.id)

    # ================================================================ status summary
    def refresh_post(self, row_id: int) -> Post | None:
        """Recompute the post status and card stage from its platforms' statuses."""
        post = self.store.get_post(row_id)
        dispatches = self.store.dispatches_for(row_id)
        if post is None or not dispatches:
            return post
        statuses = [d.status for d in dispatches]
        youtube = next((d for d in dispatches if d.platform == Platform.YOUTUBE), None)
        if DispatchStatus.INTERRUPTED in statuses:
            status, stage = PostStatus.SCHEDULED, Stage.SCHEDULED   # held: waiting for a person's decision
        elif youtube and youtube.status in (DispatchStatus.PENDING, DispatchStatus.DISPATCHING):
            status, stage = PostStatus.UPLOADING, Stage.SCHEDULED
        elif all(s == DispatchStatus.FAILED for s in statuses):
            status, stage = PostStatus.FAILED, Stage.STAGING
        elif all(s in (DispatchStatus.PUBLISHED, DispatchStatus.FAILED) for s in statuses):
            status, stage = PostStatus.PUBLISHED, Stage.PUBLISHED
            enabled = self.enabled_platforms()
            hand = [p for p in post.target_platforms if p not in {d.platform for d in dispatches} and p not in enabled]
            states = self.platform_states(row_id) if hand else {}
            if any(states[p].state != PUBLISHED for p in hand if p in states):
                stage = Stage.SCHEDULED       # still waiting on platforms posted by hand
        else:
            status, stage = PostStatus.SCHEDULED, Stage.SCHEDULED
        error = "; ".join(f"{Platform.LABELS.get(d.platform, d.platform)}: {d.error}"
                          for d in dispatches if d.error)
        main = youtube if (youtube and youtube.remote_id) else next(
            (d for d in dispatches if d.remote_id), None)
        updated = replace(post, status=status, error=error,
                          remote_id=main.remote_id if main else post.remote_id,
                          remote_url=main.remote_url if main else post.remote_url)
        if updated != post:
            self.store.save_post(updated)
        if self._row(row_id).stage != stage:
            self.store.set_stage([row_id], stage)
        return updated

    # ================================================================ dispatcher support
    def build_job(self, dispatch: Dispatch) -> PublishJob:
        """Everything a publisher needs for one claimed dispatch. Raises PermanentError."""
        row = self.store.get_video(dispatch.video_row)
        post = self.store.get_post(dispatch.video_row)
        if row is None or post is None:
            raise PermanentError("The video or its post was deleted.")
        label = Platform.LABELS.get(dispatch.platform, dispatch.platform)
        account = self.accounts.account_for(row.profile_id, dispatch.platform, dispatch.account_id)
        if account is None:
            raise PermanentError(f"No {label} account is connected for this profile.")
        credentials = self.accounts.credentials(account)
        if credentials is None:
            raise PermanentError(f"{label}: the sign-in is missing from the keychain. Reconnect it in Accounts.")
        path = self.video_path(row)
        if path is None or not path.is_file():
            raise PermanentError(self.file_problem(row))
        text = ""
        if dispatch.platform in (Platform.BLUESKY, Platform.THREADS):
            try:
                text = resolve_text(post, dispatch.platform)
            except ValueError as exc:
                raise PermanentError(str(exc)) from None
        media_url = None
        base = public_base_url(self.store)
        if dispatch.platform == Platform.THREADS and base:
            media_url = lambda: self.media.url(self.media.issue(path), base)  # noqa: E731
        return PublishJob(dispatch=dispatch, post=replace(post, publish_at=dispatch.publish_at), item=row.item,
                          video_path=path, account=account, credentials=credentials, text=text,
                          media_url=media_url, checkpoint=lambda c: self.store.set_checkpoint(dispatch.id, c))

    # ================================================================ YouTube sync
    def sync_plan(self, profile_id: int) -> SyncPlan:
        """What to ask YouTube about: uploads waiting to go live, grouped per channel."""
        waiting = [d for d in self.store.dispatches_with_status(profile_id, Platform.YOUTUBE,
                                                                [DispatchStatus.SCHEDULED]) if d.remote_id]
        posts = [p for p in (self.store.get_post(d.video_row) for d in waiting) if p]
        jobs, problems = [], []
        for account_id in sorted({d.account_id for d in waiting if d.account_id}):
            account = self.store.get_account(account_id)
            if account is None:
                continue
            try:
                client = self.accounts.client_for(account)
            except YouTubeError as exc:
                problems.append(str(exc))
                continue
            jobs.append((client, [d.remote_id for d in waiting if d.account_id == account_id]))
        return SyncPlan(posts, jobs, problems, {d.remote_id: d for d in waiting})

    @staticmethod
    def fetch_remote(plan: SyncPlan) -> dict[str, dict | None]:
        """Network part of a sync. Safe to run on a background thread."""
        out: dict[str, dict | None] = {}
        for client, ids in plan.jobs:
            statuses = client.video_statuses(ids)
            out.update({vid: statuses.get(vid) for vid in ids})
        return out

    def apply_sync(self, plan: SyncPlan, remote: dict[str, dict | None], now: datetime | None = None) -> int:
        """Apply YouTube's answers. Returns how many videos went live."""
        now = now or datetime.now(timezone.utc)
        published = 0
        for remote_id, dispatch in plan.dispatches.items():
            if remote_id not in remote:
                continue  # that channel wasn't checked
            probe = Post(video_row=dispatch.video_row, status=PostStatus.SCHEDULED, publish_at=dispatch.publish_at)
            status, note = reconcile(probe, remote[remote_id], now)
            if status == PostStatus.PUBLISHED:
                self.store.finish_dispatch(dispatch.id, DispatchStatus.PUBLISHED)
                published += 1
            elif status == PostStatus.FAILED:
                self.store.finish_dispatch(dispatch.id, DispatchStatus.FAILED, error=note)
            elif note != dispatch.error:
                self.store.finish_dispatch(dispatch.id, DispatchStatus.SCHEDULED, error=note)
            self.refresh_post(dispatch.video_row)
        return published

    # ================================================================ video edits
    def set_custom_name(self, row_id: int, name: str) -> VideoItem:
        """Rename a video's custom name (the file is renamed later via Rename Files).
        Keeps the folder's metadata sidecar and manifest.md in step."""
        row = self._row(row_id)
        row.item.custom_name = name.strip()
        self.store.save_video(row.profile_id, row.item)
        profile = self._profile(row.profile_id)
        row.item.refresh_download_status(profile.output_folder)
        if row.item.is_downloaded:
            self._update_library(profile, row.item)
        return row.item

    def set_notes(self, row_id: int, notes: str) -> None:
        self._row(row_id)
        self.store.set_notes(row_id, notes)

    def _update_library(self, profile: Profile, item: VideoItem) -> None:
        folder = Path(item.folder or profile.output_folder)
        library = Library(folder)
        try:
            library.save(item)
            items = []
            for row in self.store.load_videos(profile.id, archived=None):   # the manifest lists every file
                row.item.refresh_download_status(profile.output_folder)
                if Path(row.item.folder or profile.output_folder) == folder:
                    items.append(row.item)
            library.write_manifest(items, [profile.last_source])
        except OSError as exc:
            log.warning("Couldn't update library files in %s: %s", folder, exc)


def normalize_ticket(changes: dict) -> dict:
    """Trim text, store blank text as null, and dates as ISO 8601 UTC."""
    out = {}
    for key, value in changes.items():
        if isinstance(value, str):
            value = value if key == "transcript" else value.strip()
            if not value.strip():
                value = None
            elif key in DATE_FIELDS:
                value = utc_iso(_parse_aware(value))
        out[key] = value
    return out


def _parse_aware(value: str | datetime) -> datetime | None:
    """ISO 8601 with a timezone (or an aware datetime) → aware datetime; else None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else None


def metadata_problems(changes: dict) -> list[str]:
    """Validate edited fields by their own rules (YouTube's for title/description/
    tags, each platform's for its override). Never truncates."""
    problems: list[str] = []
    if "title" in changes:
        title = changes["title"]
        if not isinstance(title, str) or not title.strip():
            problems.append("title must be non-empty text.")
        elif len(title.strip()) > TITLE_MAX:
            problems.append(f"title is {len(title.strip())} characters; the limit is {TITLE_MAX}.")
        elif "<" in title or ">" in title:
            problems.append("title can't contain < or >.")
    if "description" in changes:
        desc = changes["description"]
        if not isinstance(desc, str):
            problems.append("description must be text.")
        elif len(desc.encode("utf-8")) > DESCRIPTION_MAX_BYTES:
            problems.append(f"description is over {DESCRIPTION_MAX_BYTES} bytes.")
        elif "<" in desc or ">" in desc:
            problems.append("description can't contain < or >.")
    if "tags" in changes:
        tags = changes["tags"]
        if not isinstance(tags, list) or not all(isinstance(t, str) and t.strip().lstrip("#") for t in tags):
            problems.append("tags must be a list of non-empty strings.")
        elif sum(len(t.strip().lstrip("#")) + 1 for t in tags) > TAGS_MAX_CHARS:
            problems.append(f"tags are over {TAGS_MAX_CHARS} characters in total.")
    if "target_platforms" in changes:
        targets = changes["target_platforms"]
        if not isinstance(targets, list) or not targets:
            problems.append("target_platforms must list at least one platform.")
        else:
            unknown = [p for p in targets if p not in MANUAL_PLATFORMS]
            if unknown:
                problems.append(f"Unknown platform(s): {', '.join(map(str, unknown))}.")
    for key, (_label, rule) in COPY_FIELDS.items():
        if key in changes and changes[key] is not None:
            value = changes[key]
            if not isinstance(value, str):
                problems.append(f"{key} must be text or null.")
            else:
                problem = text_problem(rule, value)
                if problem:
                    problems.append(f"{key}: {problem}")
    return problems
