"""The authorization boundary for external clients (the agent API now, MCP later).

The desktop app uses `Pipeline` directly: a person clicking is the authority.
Anything coming from outside goes through `ScopedPipeline`, which sorts every
operation into one of three classes:

* **Read** (board, card, readiness, history…): passed straight through.
* **Draft edit** (ticket copy and fields, restoring a revision, filling from
  the original post, fetching a transcript, saving or renaming a source):
  allowed, but rate limited per profile and capped per call.
* **Needs approval** (schedule, start an upload, archive or unarchive, bulk
  edits, applying a sheet import, marking a platform posted or scheduled,
  recording a manual post, resolving an interrupted send, deleting a source):
  **never executed here.** The arguments are validated, a hash of the target
  videos' current state is taken, an `approval_requests` row is written, and
  `ApprovalPending` is returned. Only a person approves (approvals.py), and the
  running app carries it out after checking the hash again: if the ticket
  changed while the request waited, nothing happens (status "stale").

Every video id is checked (a real positive integer, an existing video, inside
this wrapper's profiles) before anything reaches the pipeline. The `store`
attribute is a read-only view, so code holding a ScopedPipeline can't write to
the database around it.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .models import MANUAL_PLATFORMS, ApprovalRequest, DispatchStatus, Platform, Post
from .pipeline import Pipeline, PipelineError, _parse_aware, utc_iso
from .urlsafety import image_path_problem, media_url_problem, restrict_ydl

MAX_ID = 2 ** 63 - 1
MAX_IDS_PER_CALL = 200
MAX_PENDING_PER_PROFILE = 100
DEFAULT_TTL_HOURS = 24.0
SCHEDULE_MARGIN = timedelta(minutes=10)     # a scheduled post's request expires this long before its time
RATE_CAPACITY = 120                         # draft writes (per video) in a burst, per profile
RATE_PER_SECOND = 2.0                       # …refilled at this rate
COPY_FIELDS = ("title", "description", "tags", "target_platforms", "tiktok_text", "instagram_text",
               "bluesky_text", "threads_text", "facebook_text", "hashtags")


@dataclass
class ApprovalPending:
    """Returned instead of a result: a person has to approve this first."""
    approval_id: int
    operation: str
    status: str
    expires_at: str
    target_ids: list[int]
    reused: bool = False          # an identical request was already waiting


# ---------------------------------------------------------------------- the target hash
def snapshot(store, row_id: int) -> dict:
    """Everything about a video that an approval depends on: its copy, ticket fields, column,
    archived flag, publishing state and the file that would be posted."""
    from .video_tools import edited_path
    row = store.get_video(row_id)
    if row is None:
        return {"id": row_id, "missing": True}
    post = store.get_post(row_id)
    profile = store.get_profile(row.profile_id)
    path = edited_path(store, row_id) or (row.item.path(profile.output_folder) if profile else None)
    try:
        st = Path(path).stat() if path else None
        file = [str(path), st.st_size, st.st_mtime_ns] if st else None
    except OSError:
        file = None
    return {
        "id": row_id, "stage": row.stage, "archived": row.archived_at, "ticket": asdict(row.ticket),
        "post": {k: v for k, v in asdict(post).items() if k not in ("id", "updated_at")} if post else None,
        "dispatches": sorted([d.platform, d.status, d.publish_at or "", d.remote_id] for d in store.dispatches_for(row_id)),
        "manual": sorted([m.platform, m.kind, m.posted_at, m.url] for m in store.manual_posts(row_id)),
        "file": file,
    }


def target_hash(store, row_ids: list[int], extra: dict | None = None) -> str:
    data = {"videos": [snapshot(store, r) for r in sorted(set(row_ids))], "extra": extra or {}}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _source_state(store, source_id: int) -> dict:
    src = store.get_source(source_id)
    return {"source": None if src is None else {k: v for k, v in asdict(src).items() if k != "last_fetched_at"}}


def _dispatch_state(store, dispatch_id: int) -> dict:
    d = store.get_dispatch(dispatch_id)
    return {"dispatch": None if d is None else [d.id, d.platform, d.status, d.error]}


def hash_for(store, req: ApprovalRequest) -> str:
    """The hash of what `req` targets, as it is now (to compare with the one taken at request time)."""
    return target_hash(store, req.target_ids, _extra(store, req.operation, req.payload))


def _extra(store, operation: str, payload: dict) -> dict:
    if operation == "delete_source":
        return _source_state(store, payload["source_id"])
    if operation == "resolve_interrupted":
        return _dispatch_state(store, payload["dispatch_id"])
    return {}


# ---------------------------------------------------------------------- creating requests
def ttl() -> timedelta:
    try:
        return timedelta(hours=float(os.environ.get("REELAY_APPROVAL_TTL_HOURS", DEFAULT_TTL_HOURS)))
    except ValueError:
        return timedelta(hours=DEFAULT_TTL_HOURS)


def request_approval(store, profile_id: int, operation: str, payload: dict, target_ids: list[int],
                     requested_by: str = "api", now: datetime | None = None,
                     expires: datetime | None = None) -> ApprovalPending:
    """Write (or reuse) a pending approval request. Executes nothing."""
    now = now or datetime.now(timezone.utc)
    now_iso = utc_iso(now)
    store.expire_approvals(now_iso)
    payload = json.loads(json.dumps(payload, default=str))           # plain JSON: what will be executed
    target_ids = sorted(set(target_ids))
    existing = store.find_pending_approval(profile_id, operation, payload, target_ids, now_iso)
    if existing is not None:
        return ApprovalPending(existing.id, operation, existing.status, existing.expires_at, target_ids, reused=True)
    if store.count_pending_approvals(profile_id, now_iso) >= MAX_PENDING_PER_PROFILE:
        raise PipelineError(f"{MAX_PENDING_PER_PROFILE} requests are already waiting for approval for this "
                            "profile. Ask the person to review them first.", "too_many_pending")
    expires_at = min(expires or now + ttl(), now + ttl())
    if expires_at <= now + timedelta(minutes=5):
        raise PipelineError("That's too soon to wait for a person's approval. Pick a later time.", "invalid_time")
    req = store.create_approval(ApprovalRequest(
        profile_id=profile_id, operation=operation, payload=payload, target_ids=target_ids,
        target_hash=target_hash(store, target_ids, _extra(store, operation, payload)),
        requested_by=requested_by, created_at=now_iso, expires_at=utc_iso(expires_at)))
    return ApprovalPending(req.id, operation, req.status, req.expires_at, target_ids)


# ---------------------------------------------------------------------- executing (only after approval)
def _ids(payload: dict, key: str = "video_ids") -> list[int]:
    ids = payload.get(key)
    if not isinstance(ids, list) or not ids or not all(type(i) is int and 0 < i <= MAX_ID for i in ids):
        raise PipelineError("The stored request is damaged (video ids).", "invalid_request")
    return ids


def _exec_schedule(p: Pipeline, a: dict) -> dict:
    result = p.schedule(_ids({"v": [a["video_id"]]}, "v")[0], scheduled_at=a.get("scheduled_at"),
                        platforms=a.get("platforms"), add_shorts=a.get("add_shorts"), force=bool(a.get("force")))
    return {"dispatches": [{"platform": d.platform, "id": d.id, "status": d.status} for d in result.dispatches],
            "manual_platforms": result.manual}


def _exec_archive(p: Pipeline, a: dict) -> dict:
    p.archive(_ids(a), bool(a["archived"]))
    return {"archived": bool(a["archived"]), "videos": len(a["video_ids"])}


def _exec_bulk(p: Pipeline, a: dict) -> dict:
    return {"changed": p.bulk_update(_ids(a), dict(a["changes"]))}


def _exec_import(p: Pipeline, a: dict) -> dict:
    plan = p.plan_sheet(int(a["profile_id"]), list(a["headers"]), [list(r) for r in a["rows"]],
                        dict(a["mapping"]), bool(a["overwrite"]))
    expected = sorted(a.get("expected_ids") or [])
    now_ids = sorted(r.video_id for r in plan.changing if r.video_id)
    if now_ids != expected:
        raise PipelineError("The sheet would now change different videos than when it was requested.", "conflict")
    return {"changed": p.apply_sheet(plan)}


def _exec_platform(p: Pipeline, a: dict) -> dict:
    vid = _ids({"v": [a["video_id"]]}, "v")[0]
    if a.get("url") and a["state"] != "none":
        p.confirm_handoff(vid, a["platform"], a["url"], a.get("when"), scheduled=a["state"] == "scheduled")
    else:
        p.set_platform_state(vid, a["platform"], a["state"], a.get("when"))
    return {"platform": a["platform"], "state": a["state"]}


def _exec_manual(p: Pipeline, a: dict) -> dict:
    vid = _ids({"v": [a["video_id"]]}, "v")[0]
    m = p.mark_posted(vid, a["platform"], a.get("posted_at"), a.get("url") or "", a.get("note") or "")
    return {"manual_post_id": m.id}


def _exec_resolve(p: Pipeline, a: dict) -> dict:
    d = p.resolve_interrupted(int(a["dispatch_id"]), a["action"], a.get("url") or "")
    return {"dispatch_id": d.id, "status": d.status}


def _exec_delete_source(p: Pipeline, a: dict) -> dict:
    from .sources import SourceService
    SourceService(p.store, p.accounts).remove(int(a["source_id"]))
    return {"deleted_source": int(a["source_id"])}


# The only things an approved request can do. A payload naming anything else is refused.
OPERATIONS: dict[str, Callable[[Pipeline, dict], dict]] = {
    "schedule": _exec_schedule, "archive": _exec_archive, "bulk_update": _exec_bulk, "import_sheet": _exec_import,
    "set_platform_state": _exec_platform, "mark_posted": _exec_manual, "resolve_interrupted": _exec_resolve,
    "delete_source": _exec_delete_source,
}
OPERATION_LABELS = {
    "schedule": "Schedule", "archive": "Archive / unarchive", "bulk_update": "Bulk edit",
    "import_sheet": "Apply sheet import", "set_platform_state": "Mark platform", "mark_posted": "Record manual post",
    "resolve_interrupted": "Resolve interrupted send", "delete_source": "Delete source",
}


# ---------------------------------------------------------------------- rate limiting
class RateLimiter:
    """A token bucket per profile for draft writes (cost = videos touched)."""

    def __init__(self, capacity: float = RATE_CAPACITY, per_second: float = RATE_PER_SECOND,
                 clock=time.monotonic):
        self.capacity, self.per_second, self.clock = capacity, per_second, clock
        self._buckets: dict[int, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def take(self, profile_id: int, cost: int = 1) -> None:
        with self._lock:
            now = self.clock()
            tokens, last = self._buckets.get(profile_id, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.per_second)
            if tokens < cost:
                self._buckets[profile_id] = (tokens, now)
                wait = int((cost - tokens) / self.per_second) + 1
                raise PipelineError(f"Too many changes too quickly. Try again in about {wait} s.", "rate_limited")
            self._buckets[profile_id] = (tokens - cost, now)


# ---------------------------------------------------------------------- read-only store view
class ReadOnlyStore:
    """The Store's read methods (and `origin` for tagging), nothing that writes."""

    READS = frozenset({
        "get_video", "get_post", "get_profile", "list_profiles", "list_accounts", "get_account", "search",
        "revisions", "dispatches_for", "get_dispatch", "manual_posts", "get_source", "list_sources",
        "load_videos", "get_setting", "list_approvals", "get_approval", "interrupted_dispatches",
        "active_brand_profile", "list_brand_profiles", "origin", "subscribe",
    })

    def __init__(self, store):
        object.__setattr__(self, "_store", store)

    def __getattr__(self, name):
        if name in self.READS:
            return getattr(self._store, name)
        raise AttributeError(f"{name} isn't available to external clients (read-only store view)")

    def __setattr__(self, name, value):
        raise AttributeError("The store view is read-only.")

    @property
    def path(self) -> str:
        return self._store.path


# ---------------------------------------------------------------------- the wrapper
class ScopedPipeline:
    """See the module docstring. `profile_ids=None` allows every profile; a set limits the client."""

    def __init__(self, pipeline: Pipeline, profile_ids: set[int] | None = None, requested_by: str = "api",
                 limiter: RateLimiter | None = None, clock: Callable[[], datetime] | None = None):
        self._pipeline = pipeline
        self._store = pipeline.store
        self.store = ReadOnlyStore(pipeline.store)
        self.profile_ids = set(profile_ids) if profile_ids is not None else None
        self.requested_by = requested_by
        self.limiter = limiter or RateLimiter()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # ---------------------------------------------------------------- validation
    def _allowed(self, profile_id: int) -> bool:
        return self.profile_ids is None or profile_id in self.profile_ids

    def check_profile(self, profile_id: int):
        profile = self._store.get_profile(profile_id) if type(profile_id) is int else None
        if profile is None or not self._allowed(profile.id):
            raise PipelineError("Unknown profile.", "not_found")
        return profile

    def video(self, video_id) -> object:
        """A validated video row: a real positive integer id of a video this client may see."""
        if type(video_id) is not int or not 0 < video_id <= MAX_ID:
            raise PipelineError("Video ids are positive whole numbers.", "invalid_id")
        row = self._store.get_video(video_id)
        if row is None or not self._allowed(row.profile_id):
            raise PipelineError(f"No video with id {video_id}.", "not_found")
        return row

    def videos(self, video_ids) -> list:
        """Validated rows for a list of ids: no duplicates beyond the cap, one profile only."""
        if not isinstance(video_ids, (list, tuple)) or not video_ids:
            raise PipelineError("Send at least one video id.", "invalid_request")
        for v in video_ids:   # check types BEFORE de-duplicating: True == 1.0 == 1 in Python
            if type(v) is not int or not 0 < v <= MAX_ID:
                raise PipelineError("Video ids are positive whole numbers.", "invalid_id")
        ids = list(dict.fromkeys(video_ids))
        if len(ids) > MAX_IDS_PER_CALL:
            raise PipelineError(f"At most {MAX_IDS_PER_CALL} videos per request.", "invalid_request")
        rows = [self.video(i) for i in ids]
        if len({r.profile_id for r in rows}) > 1:
            raise PipelineError("All videos in one request must belong to the same profile.", "invalid_request")
        return rows

    def _check_ticket_values(self, row, changes: dict) -> None:
        if "thumbnail_path" in changes and changes["thumbnail_path"]:
            profile = self._store.get_profile(row.profile_id)
            problem = image_path_problem(str(changes["thumbnail_path"]), profile.output_folder)
            if problem:
                raise PipelineError(problem, "invalid_field", [problem])

    def _approval(self, profile_id: int, operation: str, payload: dict, target_ids: list[int],
                  expires: datetime | None = None) -> ApprovalPending:
        return request_approval(self._store, profile_id, operation, payload, target_ids, self.requested_by,
                                self.clock(), expires)

    # ---------------------------------------------------------------- reads (pass through)
    def board(self, profile_id: int):
        self.check_profile(profile_id)
        return self._pipeline.board(profile_id)

    def card(self, video_id: int):
        self.video(video_id)
        return self._pipeline.card(video_id)

    def readiness(self, video_id: int, post: Post | None = None):
        self.video(video_id)
        return self._pipeline.readiness(video_id, post)

    def publishing_history(self, video_id: int):
        self.video(video_id)
        return self._pipeline.publishing_history(video_id)

    def platform_states(self, video_id: int):
        self.video(video_id)
        return self._pipeline.platform_states(video_id)

    def archived(self, profile_id: int):
        self.check_profile(profile_id)
        return self._pipeline.archived(profile_id)

    def post_or_default(self, row):
        self.video(row.id)
        return self._pipeline.post_or_default(row)

    def queue_position(self, dispatch):
        return self._pipeline.queue_position(dispatch)

    def enabled_platforms(self):
        return self._pipeline.enabled_platforms()

    def plan_sheet(self, profile_id: int, headers, rows, mapping, overwrite: bool = False):
        """A dry run: what an import would change (writes nothing)."""
        self.check_profile(profile_id)
        return self._pipeline.plan_sheet(profile_id, headers, rows, mapping, overwrite)

    @property
    def media(self):
        return self._pipeline.media

    def sources(self, profile_id: int):
        from .sources import SourceService
        self.check_profile(profile_id)
        return SourceService(self._store, self._pipeline.accounts).all_sources(profile_id)

    # ---------------------------------------------------------------- draft edits (rate limited)
    def save_draft(self, post: Post) -> Post:
        row = self.video(post.video_row)
        self.limiter.take(row.profile_id)
        return self._pipeline.save_draft(post)

    def update_metadata(self, video_id: int, changes: dict) -> Post:
        row = self.video(video_id)
        self._check_ticket_values(row, changes)
        self.limiter.take(row.profile_id)
        return self._pipeline.update_metadata(video_id, changes)

    def restore_revision(self, video_id: int, revision_id: int) -> None:
        row = self.video(video_id)
        if type(revision_id) is not int or revision_id <= 0:
            raise PipelineError("Revision ids are positive whole numbers.", "invalid_id")
        self.limiter.take(row.profile_id)
        self._pipeline.restore_revision(video_id, revision_id)

    def fill_from_original(self, video_ids: list[int]) -> dict:
        rows = self.videos(video_ids)
        self.limiter.take(rows[0].profile_id, len(rows))
        return self._pipeline.fill_from_original([r.id for r in rows])

    def fetch_transcript(self, video_id: int, ydl_opts: dict, replace_existing: bool = False) -> str:
        row = self.video(video_id)
        # the link the pipeline will actually fetch (an agent can edit source_url, so check that one)
        problem = media_url_problem(row.ticket.source_url or row.item.url)
        if problem:
            raise PipelineError(f"Can't fetch this video's transcript: {problem}", "invalid_request")
        self.limiter.take(row.profile_id, 5)                              # it goes over the network
        return self._pipeline.fetch_transcript(video_id, restrict_ydl(ydl_opts), replace_existing=replace_existing)

    def add_source(self, profile_id: int, url: str, name: str | None = None, mine: bool = False):
        from .sources import SourceError, SourceService
        self.check_profile(profile_id)
        problem = media_url_problem(url if "://" in (url or "") else "https://" + (url or ""))
        if problem:
            raise PipelineError(problem, "invalid_source")
        self.limiter.take(profile_id)
        try:
            return SourceService(self._store, self._pipeline.accounts).add(profile_id, url, name, mine)
        except SourceError as exc:
            raise PipelineError(str(exc), "invalid_source") from None

    def update_source(self, source_id: int, name: str | None = None, mine: bool | None = None):
        from .sources import SourceError, SourceService
        src = self._source(source_id)
        self.limiter.take(src.profile_id)
        service = SourceService(self._store, self._pipeline.accounts)
        try:
            if name is not None:
                service.rename(source_id, name)
            if mine is not None:
                service.set_mine(source_id, mine)
        except SourceError as exc:
            raise PipelineError(str(exc), "invalid_source") from None

    def _source(self, source_id: int):
        if type(source_id) is not int or source_id <= 0:
            raise PipelineError("Source ids are positive whole numbers.", "invalid_id")
        src = self._store.get_source(source_id)
        if src is None or not self._allowed(src.profile_id):
            raise PipelineError(f"No saved source with id {source_id}.", "not_found")
        return src

    # ---------------------------------------------------------------- needs approval (never executed here)
    def schedule(self, video_id: int, scheduled_at: str | datetime | None = None, platforms: list[str] | None = None,
                 add_shorts: bool | None = None, force: bool = False) -> ApprovalPending:
        row = self.video(video_id)
        now = self.clock()
        # The same checks scheduling makes, now, so a request that can't work fails straight away.
        _row, base, targets, _manual = self._pipeline.schedule_plan(video_id, scheduled_at, platforms, None, now,
                                                                     bool(force))
        when = _parse_aware(scheduled_at) if scheduled_at is not None else None
        expires = (when - SCHEDULE_MARGIN) if when else None
        payload = {"video_id": video_id, "scheduled_at": utc_iso(when) if when else None,
                   "platforms": list(dict.fromkeys(platforms)) if platforms else None,
                   "add_shorts": add_shorts, "force": bool(force)}
        return self._approval(row.profile_id, "schedule", payload, [video_id], expires)

    def start_upload(self, post: Post, add_shorts: bool | None = None, force: bool = False) -> ApprovalPending:
        """Save the typed copy as a draft (allowed), then ask to schedule it (needs approval)."""
        self.save_draft(post)
        return self.schedule(post.video_row, add_shorts=add_shorts, force=force)

    def archive(self, video_ids: list[int], archived: bool = True) -> ApprovalPending:
        rows = self.videos(video_ids)
        if archived:
            busy = [r.id for r in rows if self._pipeline._active(r.id)]
            if busy:
                raise PipelineError("Videos that are scheduled or uploading can't be archived.", "locked")
        return self._approval(rows[0].profile_id, "archive", {"video_ids": [r.id for r in rows],
                                                              "archived": bool(archived)}, [r.id for r in rows])

    def bulk_update(self, video_ids: list[int], changes: dict) -> ApprovalPending:
        rows = self.videos(video_ids)
        if not isinstance(changes, dict) or not changes:
            raise PipelineError("Send at least one field to change.", "invalid_request")
        for r in rows:
            self._check_ticket_values(r, changes)
        locked = [r.id for r in rows if set(changes) & set(COPY_FIELDS) and self._pipeline._copy_locked(r)]
        if locked:
            raise PipelineError("Copy can't change on videos that are scheduled or published.", "locked")
        return self._approval(rows[0].profile_id, "bulk_update", {"video_ids": [r.id for r in rows],
                                                                  "changes": changes}, [r.id for r in rows])

    def apply_sheet(self, profile_id: int, headers, rows, mapping, overwrite: bool = False) -> ApprovalPending | None:
        """Ask to apply an import. None if it would change nothing."""
        self.check_profile(profile_id)
        plan = self._pipeline.plan_sheet(profile_id, headers, rows, mapping, overwrite)
        ids = sorted(r.video_id for r in plan.changing if r.video_id)
        if not ids:
            return None
        if len(ids) > MAX_IDS_PER_CALL:
            raise PipelineError(f"At most {MAX_IDS_PER_CALL} videos per import.", "invalid_request")
        for i in ids:
            self.video(i)
        payload = {"profile_id": profile_id, "headers": list(headers), "rows": [list(r) for r in rows],
                   "mapping": dict(mapping), "overwrite": bool(overwrite), "expected_ids": ids}
        return self._approval(profile_id, "import_sheet", payload, ids)

    def set_platform_state(self, video_id: int, platform: str, state: str, when=None,
                           url: str = "") -> ApprovalPending:
        from .handoff import check_post_url
        row = self.video(video_id)
        if platform not in MANUAL_PLATFORMS:
            raise PipelineError(f"Unknown platform “{platform}”.", "invalid_platform")
        if state not in ("scheduled", "posted", "none"):
            raise PipelineError("state must be scheduled, posted or none.", "invalid_request")
        if any(d.platform == platform for d in self._store.dispatches_for(video_id)):
            raise PipelineError(f"Reelay is posting this to {Platform.LABELS[platform]} itself; its status "
                                "updates on its own.", "locked")
        problem = check_post_url(platform, url) if url else None
        if problem:
            raise PipelineError(problem, "invalid_post")
        when_dt = _parse_aware(when) if when is not None else None
        if when is not None and when_dt is None:
            raise PipelineError("The time must be an ISO 8601 time with a timezone.", "invalid_time")
        payload = {"video_id": video_id, "platform": platform, "state": state,
                   "when": utc_iso(when_dt) if when_dt else None, "url": url or ""}
        return self._approval(row.profile_id, "set_platform_state", payload, [video_id])

    def mark_posted(self, video_id: int, platform: str, posted_at=None, url: str = "",
                    note: str = "") -> ApprovalPending:
        row = self.video(video_id)
        if platform not in MANUAL_PLATFORMS:
            raise PipelineError(f"Unknown platform “{platform}”.", "invalid_platform")
        when = _parse_aware(posted_at) if posted_at is not None else None
        if posted_at is not None and when is None:
            raise PipelineError("posted_at must be an ISO 8601 time with a timezone.", "invalid_time")
        if when and when > self.clock() + timedelta(minutes=5):
            raise PipelineError("posted_at is in the future; mark it as posted once it's live.", "invalid_time")
        if url and not url.strip().startswith(("http://", "https://")):
            raise PipelineError("url must start with http:// or https://.", "invalid_post")
        payload = {"video_id": video_id, "platform": platform, "posted_at": utc_iso(when) if when else None,
                   "url": (url or "").strip(), "note": (note or "").strip()[:500]}
        return self._approval(row.profile_id, "mark_posted", payload, [video_id])

    def resolve_interrupted(self, dispatch_id: int, action: str, url: str = "") -> ApprovalPending:
        from .handoff import check_post_url
        if type(dispatch_id) is not int or dispatch_id <= 0:
            raise PipelineError("Send ids are positive whole numbers.", "invalid_id")
        d = self._store.get_dispatch(dispatch_id)
        if d is None:
            raise PipelineError("That send no longer exists.", "not_found")
        row = self.video(d.video_row)
        if d.status != DispatchStatus.INTERRUPTED:
            raise PipelineError("That send isn't waiting for a decision.", "conflict")
        if action not in ("retry", "posted", "cancel"):
            raise PipelineError("action must be retry, posted or cancel.", "invalid_request")
        problem = check_post_url(d.platform, url) if (action == "posted" and url) else None
        if problem:
            raise PipelineError(problem, "invalid_post")
        return self._approval(row.profile_id, "resolve_interrupted",
                              {"dispatch_id": dispatch_id, "action": action, "url": url or ""}, [row.id])

    def delete_source(self, source_id: int) -> ApprovalPending:
        src = self._source(source_id)
        return self._approval(src.profile_id, "delete_source", {"source_id": source_id}, [])

    # ---------------------------------------------------------------- requests (read)
    def approvals(self, profile_id: int | None = None, status: str | None = None, limit: int = 100):
        self._store.expire_approvals(utc_iso(self.clock()))
        reqs = self._store.list_approvals(profile_id, status, limit)
        return [r for r in reqs if self._allowed(r.profile_id)]

    def approval(self, approval_id: int) -> ApprovalRequest:
        if type(approval_id) is not int or approval_id <= 0:
            raise PipelineError("Approval ids are positive whole numbers.", "invalid_id")
        self._store.expire_approvals(utc_iso(self.clock()))
        req = self._store.get_approval(approval_id)
        if req is None or not self._allowed(req.profile_id):
            raise PipelineError(f"No approval request with id {approval_id}.", "not_found")
        return req

