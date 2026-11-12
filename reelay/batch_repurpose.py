"""Reelay Batch Repurpose Queue: Multi-ticket proposal generation via Meta Muse Spark.

Post-hackathon product work for Reelay (branch batch-v2).
Founder: Andrew Baxter.
Model: Meta Muse Spark (muse-spark-1.3 via https://api.meta.ai/v1, Standard non-contributor tier).

Design & Guarantees:
- BatchJob: Dataclass tracking the lifecycle of each ticket's repurpose proposal
  across 6 platforms (YouTube, TikTok, Instagram, Bluesky, Threads, Facebook).
- BatchRepurposeQueue / BatchQueueManager: Thread-safe in-memory queue manager supporting:
    * Background execution with small fixed concurrency cap (max_concurrency=1 or 2).
    * Synchronous execution (process_sync) for CLI / testing.
    * Real-time progress callbacks (per ticket and batch total).
    * Fine-grained cancel operations (cancel_batch / cancel_all, cancel_job).
    * Safe retry operation (retry_job for failed or cancelled jobs).
    * Strict failure isolation: An error on one ticket (e.g. rate limit, validation error,
      network timeout) NEVER halts or kills the rest of the batch.
    * Rate limit respect: Detects HTTP 429 errors from RepurposeAPIError and applies
      configurable backoff before taking the next ticket.
    * Privacy gate: Batch execution requires explicit Cloud AI opt-in. Verifies
      require_cloud_ai_opt_in before running or retrying.
    * Credential safety: API keys are NEVER printed, logged, or stored in errors.
    * Local-first safety: Proposals are generated in-memory only. NEVER auto-saves to
      SQLite, NEVER posts, and NEVER auto-publishes.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .privacy import (
    PrivacyOptInRequiredError,
    require_cloud_ai_opt_in,
)
from .repurpose_engine import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    RepurposeAPIError,
    RepurposeConfigError,
    RepurposeError,
    RepurposeNetworkError,
    RepurposeValidationError,
    generate_repurpose_proposal,
)
from .tokens import KeyringTokenStore, TokenStore

log = logging.getLogger("reelay.batch_repurpose")

# ---------------------------------------------------------------------- Job Statuses
JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_READY = "ready"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_APPLIED = "applied"
JOB_STATUS_SAVED = "saved"
JOB_STATUS_CANCELLED = "cancelled"

VALID_JOB_STATUSES = frozenset({
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_READY,
    JOB_STATUS_FAILED,
    JOB_STATUS_APPLIED,
    JOB_STATUS_SAVED,
    JOB_STATUS_CANCELLED,
})

TERMINAL_JOB_STATUSES = frozenset({
    JOB_STATUS_READY,
    JOB_STATUS_FAILED,
    JOB_STATUS_APPLIED,
    JOB_STATUS_SAVED,
    JOB_STATUS_CANCELLED,
})

RETRYABLE_JOB_STATUSES = frozenset({
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
})

DEFAULT_PLATFORMS: list[str] = [
    "youtube",
    "tiktok",
    "instagram",
    "bluesky",
    "threads",
    "facebook",
]

SNAPSHOT_FIELDS = ("title", "description", "notes", "transcript", "brand_voice")

# Regex patterns for stripping sensitive credentials from error messages
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]+", re.IGNORECASE)
_PARAM_KEY_RE = re.compile(r"(api[_-]?key|token|secret)=([A-Za-z0-9_\-\.]+)", re.IGNORECASE)
_JSON_KEY_RE = re.compile(r'("?(?:api[_-]?key|token|secret|password)"?\s*:\s*)"[^"]+"', re.IGNORECASE)
_AUTH_HEADER_RE = re.compile(r"(Authorization|x-api-key):\s*([^\r\n]+)", re.IGNORECASE)


# ---------------------------------------------------------------------- Sanitization
def sanitize_error_message(error: str | None, api_key: str | None = None) -> str | None:
    """Sanitizes an error message so API keys and bearer tokens are NEVER logged or stored.

    Args:
        error: Raw error string or None.
        api_key: Optional explicit API key to redact.

    Returns:
        Redacted error string or None.
    """
    if error is None:
        return None

    cleaned = str(error)

    # 1. Redact explicit API key if provided
    if api_key and api_key.strip():
        cleaned = cleaned.replace(api_key.strip(), "[REDACTED]")

    # 2. Redact META_API_KEY from env if present
    env_key = os.environ.get("META_API_KEY", "").strip()
    if env_key:
        cleaned = cleaned.replace(env_key, "[REDACTED]")

    # 3. Pattern-based redaction for bearer tokens, query params, headers, and JSON fields
    cleaned = _BEARER_RE.sub("Bearer [REDACTED]", cleaned)
    cleaned = _PARAM_KEY_RE.sub(r"\1=[REDACTED]", cleaned)
    cleaned = _JSON_KEY_RE.sub(r'\1"[REDACTED]"', cleaned)
    cleaned = _AUTH_HEADER_RE.sub(r"\1: [REDACTED]", cleaned)

    return cleaned


# ---------------------------------------------------------------------- Metadata Helpers
def normalize_snapshot(snapshot: dict[str, Any] | None) -> dict[str, str]:
    """Ensures source snapshot contains all 5 required string fields."""
    if not snapshot:
        return {f: "" for f in SNAPSHOT_FIELDS}
    return {
        f: str(snapshot.get(f, "") or "").strip()
        for f in SNAPSHOT_FIELDS
    }


def extract_source_snapshot(
    obj: Any,
    *,
    store: Any | None = None,
    brand_voice: str = "",
) -> dict[str, str]:
    """Extracts a normalized 5-field source snapshot from a ticket, card, row, or dict.

    Supported objects:
    - Dict with snapshot keys or a nested 'source_snapshot' key.
    - VideoRow (reelay.models.VideoRow).
    - TicketCard or Card (reelay.gui.ticket_widgets.TicketCard / card).
    - Ticket (reelay.models.Ticket).
    """
    if isinstance(obj, dict):
        if "source_snapshot" in obj and isinstance(obj["source_snapshot"], dict):
            return normalize_snapshot(obj["source_snapshot"])
        return normalize_snapshot(obj)

    # Check for direct snapshot property if already set
    if hasattr(obj, "source_snapshot") and isinstance(obj.source_snapshot, dict):
        return normalize_snapshot(obj.source_snapshot)
    if hasattr(obj, "_source_snapshot") and isinstance(obj._source_snapshot, dict):
        return normalize_snapshot(obj._source_snapshot)

    # Walk common Reelay domain hierarchies
    row = getattr(obj, "row", None) if not hasattr(obj, "item") else obj
    post = getattr(obj, "post", None)
    item = getattr(row, "item", None) if row is not None else getattr(obj, "item", None)
    ticket = getattr(row, "ticket", None) if row is not None else getattr(obj, "ticket", None)
    if ticket is None and hasattr(obj, "transcript"):
        ticket = obj

    source_title = (
        getattr(obj, "title", None)
        or (getattr(post, "title", None) if post else None)
        or (getattr(item, "title", None) if item else None)
        or ""
    )
    source_description = (
        getattr(obj, "description", None)
        or (getattr(post, "description", None) if post else None)
        or (getattr(item, "description", None) if item else None)
        or ""
    )
    source_transcript = getattr(ticket, "transcript", None) or ""

    source_notes = getattr(obj, "notes", None) or (getattr(row, "notes", None) if row else None) or ""
    if not source_notes.strip() and ticket is not None:
        parts = [
            p.strip()
            for p in (
                getattr(ticket, "topic", "") or "",
                getattr(ticket, "keywords", "") or "",
            )
            if p and p.strip()
        ]
        source_notes = " · ".join(parts)

    resolved_brand_voice = brand_voice.strip()
    if not resolved_brand_voice and store is not None and row is not None and hasattr(store, "active_brand_profile"):
        profile_id = getattr(row, "profile_id", 0)
        try:
            from . import assist
            brand = store.active_brand_profile(profile_id)
            if brand:
                resolved_brand_voice = assist.system_prompt(brand)
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not resolve brand voice for profile %s: %s", profile_id, exc)

    return normalize_snapshot({
        "title": source_title,
        "description": source_description,
        "notes": source_notes,
        "transcript": source_transcript,
        "brand_voice": resolved_brand_voice,
    })


def _accepts(fn, name: str) -> bool:
    """Whether a generate function takes a keyword (test doubles may not take `voice`)."""
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


# ---------------------------------------------------------------------- BatchJob Dataclass
@dataclass
class BatchJob:
    """Represents a single ticket's proposal generation job in the batch queue.

    Attributes:
        ticket_id: Unique integer identifier for the ticket / video row.
        source_snapshot: Dict with title, description, notes, transcript, and brand_voice.
        platforms: List of target platform keys (e.g. ['youtube', 'tiktok', ...]).
        status: One of 'queued', 'running', 'ready', 'failed', 'applied', 'saved', 'cancelled'.
        proposal: 9-field proposal dictionary when generated and ready.
        variants: Optional generated copy variants per platform.
        error: Sanitized error description if failed (never contains credentials).
        error_status_code: HTTP status code if failed due to an API error (e.g. 429).
        created_at: Unix timestamp when the job was created.
        updated_at: Unix timestamp when the job was last modified.
        reviewed: True when the creator has reviewed and approved the proposal.
        applied: True when the proposal was applied to the ticket editor.
        saved: True when the proposal was persisted to the ticket.
    """

    ticket_id: int
    source_snapshot: dict[str, str] = field(default_factory=lambda: {f: "" for f in SNAPSHOT_FIELDS})
    platforms: list[str] = field(default_factory=lambda: list(DEFAULT_PLATFORMS))
    status: str = JOB_STATUS_QUEUED
    proposal: dict[str, str] | None = None
    variants: dict[str, list[dict[str, str]]] | None = None
    error: str | None = None
    error_status_code: int | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    reviewed: bool = False
    applied: bool = False
    saved: bool = False

    def __post_init__(self) -> None:
        self.ticket_id = int(self.ticket_id)
        self.source_snapshot = normalize_snapshot(self.source_snapshot)
        self.platforms = list(self.platforms) if self.platforms else list(DEFAULT_PLATFORMS)
        if self.status not in VALID_JOB_STATUSES:
            raise ValueError(f"Invalid job status {self.status!r}. Must be one of {sorted(VALID_JOB_STATUSES)}")
        self.created_at = float(self.created_at)
        self.updated_at = float(self.updated_at)
        self.error = sanitize_error_message(self.error)

    # --------------------------------- Status Properties
    @property
    def is_terminal(self) -> bool:
        """True if the job has reached a final or completed state."""
        return self.status in TERMINAL_JOB_STATUSES

    @property
    def can_retry(self) -> bool:
        """True if the job can be re-queued via retry_job (failed or cancelled)."""
        return self.status in RETRYABLE_JOB_STATUSES

    @property
    def is_pending(self) -> bool:
        """True if the job is awaiting execution or currently executing."""
        return self.status in (JOB_STATUS_QUEUED, JOB_STATUS_RUNNING)

    @property
    def is_ready(self) -> bool:
        """True if the proposal was successfully generated and is awaiting review."""
        return self.status == JOB_STATUS_READY

    @property
    def is_failed(self) -> bool:
        """True if proposal generation failed."""
        return self.status == JOB_STATUS_FAILED

    @property
    def is_cancelled(self) -> bool:
        """True if the job was cancelled by creator."""
        return self.status == JOB_STATUS_CANCELLED

    # --------------------------------- Mutation Helpers
    def mark_running(self) -> None:
        """Transitions job to running status."""
        self.status = JOB_STATUS_RUNNING
        self.updated_at = time.time()

    def mark_ready(
        self,
        proposal: dict[str, str],
        variants: dict[str, list[dict[str, str]]] | None = None,
    ) -> None:
        """Transitions job to ready status with generated proposal."""
        self.status = JOB_STATUS_READY
        self.proposal = dict(proposal)
        self.variants = variants
        self.error = None
        self.error_status_code = None
        self.updated_at = time.time()

    def mark_failed(
        self,
        error_message: str,
        status_code: int | None = None,
        api_key: str | None = None,
    ) -> None:
        """Transitions job to failed status with sanitized error description."""
        self.status = JOB_STATUS_FAILED
        self.error = sanitize_error_message(error_message, api_key=api_key)
        self.error_status_code = status_code
        self.updated_at = time.time()

    def mark_cancelled(self) -> None:
        """Transitions job to cancelled status."""
        self.status = JOB_STATUS_CANCELLED
        self.updated_at = time.time()

    def mark_reviewed(self, reviewed: bool = True) -> None:
        """Records creator review state."""
        self.reviewed = reviewed
        self.updated_at = time.time()

    def mark_applied(self, applied: bool = True) -> None:
        """Records whether copy was applied to ticket draft in editor."""
        self.applied = applied
        if applied:
            self.status = JOB_STATUS_APPLIED
        self.updated_at = time.time()

    def mark_saved(self, saved: bool = True) -> None:
        """Records whether copy was committed to SQLite ticket."""
        self.saved = saved
        if saved:
            self.status = JOB_STATUS_SAVED
        self.updated_at = time.time()

    def add_variant(self, platform: str, variant: dict[str, str]) -> None:
        """Adds an alternative copy variant for a specific platform."""
        if self.variants is None:
            self.variants = {}
        if platform not in self.variants:
            self.variants[platform] = []
        self.variants[platform].append(dict(variant))
        self.updated_at = time.time()

    # --------------------------------- Serialization
    def to_dict(self) -> dict[str, Any]:
        """Serializes the BatchJob to a plain JSON-compatible dictionary."""
        return {
            "ticket_id": self.ticket_id,
            "source_snapshot": dict(self.source_snapshot),
            "platforms": list(self.platforms),
            "status": self.status,
            "proposal": dict(self.proposal) if self.proposal else None,
            "variants": self.variants,
            "error": self.error,
            "error_status_code": self.error_status_code,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reviewed": self.reviewed,
            "applied": self.applied,
            "saved": self.saved,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatchJob:
        """Deserializes a BatchJob from a dictionary."""
        return cls(
            ticket_id=int(data["ticket_id"]),
            source_snapshot=normalize_snapshot(data.get("source_snapshot")),
            platforms=list(data.get("platforms", DEFAULT_PLATFORMS)),
            status=data.get("status", JOB_STATUS_QUEUED),
            proposal=data.get("proposal"),
            variants=data.get("variants"),
            error=sanitize_error_message(data.get("error")),
            error_status_code=data.get("error_status_code"),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            reviewed=bool(data.get("reviewed", False)),
            applied=bool(data.get("applied", False)),
            saved=bool(data.get("saved", False)),
        )

    @classmethod
    def from_ticket(
        cls,
        ticket_id: int,
        ticket_or_row: Any = None,
        *,
        source_snapshot: dict[str, str] | None = None,
        store: Any | None = None,
        brand_voice: str = "",
        platforms: list[str] | None = None,
    ) -> BatchJob:
        """Factory helper creating a BatchJob from a ticket, row, card, or snapshot."""
        if source_snapshot is not None:
            snapshot = normalize_snapshot(source_snapshot)
        elif ticket_or_row is not None:
            snapshot = extract_source_snapshot(ticket_or_row, store=store, brand_voice=brand_voice)
        else:
            snapshot = normalize_snapshot(None)

        return cls(
            ticket_id=ticket_id,
            source_snapshot=snapshot,
            platforms=list(platforms) if platforms else list(DEFAULT_PLATFORMS),
        )


# Callback type aliases
TicketCallback = Callable[[int, str, BatchJob], None]
BatchProgressCallback = Callable[[int, int], None]
BatchFinishedCallback = Callable[[list[BatchJob]], None]


# ---------------------------------------------------------------------- BatchRepurposeQueue
class BatchRepurposeQueue:
    """Robust batch repurpose queue engine for Reelay.

    Safeguards:
    1. Human in the Loop: Generates proposals in-memory only. No auto-save or publishing.
    2. Privacy Gate: Checks require_cloud_ai_opt_in before running or retrying.
    3. Failure Isolation: A failure on one ticket NEVER kills or stops other tickets.
    4. Rate Limit Respect: Detects HTTP 429 and applies backoff before the next request.
    5. Credential Safety: API keys are never stored, logged, or printed.
    6. Concurrency Control: Small fixed worker concurrency cap (default 1, max 2).
    """

    def __init__(
        self,
        jobs: Sequence[BatchJob | Any] | None = None,
        *,
        max_concurrency: int = 1,
        rate_limit_backoff: float = 2.0,
        store: Any | None = None,
        opt_in: bool | None = None,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        allow_refinement: bool = True,
        token_store: TokenStore | None = None,
        generate_fn: Callable[..., dict[str, str]] | None = None,
        variant_generator: Callable[[BatchJob, dict[str, str]], dict[str, list[dict[str, str]]] | None] | None = None,
        on_ticket_status: TicketCallback | None = None,
        on_batch_progress: BatchProgressCallback | None = None,
        on_batch_finished: BatchFinishedCallback | None = None,
        fetch_transcripts: bool | None = None,
    ) -> None:
        """Initializes the BatchRepurposeQueue.

        Args:
            jobs: Optional initial sequence of BatchJob instances or ticket objects.
            max_concurrency: Maximum parallel worker threads (1 or 2 recommended).
            rate_limit_backoff: Seconds to wait after encountering an HTTP 429 rate limit.
            store: Optional Reelay Store checked for cloud AI opt-in setting.
            opt_in: Explicit Cloud AI opt-in override for this batch pass.
            api_key: Optional Meta Model API key (if omitted, resolved from env/keychain).
            model: Model identifier in Meta Model API (default 'muse-spark-1.3').
            base_url: Base URL for Meta Model API (default 'https://api.meta.ai/v1').
            timeout: HTTP request timeout in seconds.
            allow_refinement: Whether over-limit fields attempt targeted rewrites.
            token_store: Optional TokenStore for keychain credential lookup.
            generate_fn: Proposal generator function (defaults to generate_repurpose_proposal).
            variant_generator: Optional callable to generate variants for ready proposals.
            on_ticket_status: Callback invoked on ticket status change: (ticket_id, status, job).
            on_batch_progress: Callback invoked on progress update: (completed, total).
            on_batch_finished: Callback invoked when all queued jobs finish: (jobs_list).
        """
        # Clamp concurrency between 1 and 2 to strictly respect rate limits
        self.max_concurrency = max(1, min(int(max_concurrency), 2))
        self.rate_limit_backoff = max(0.5, float(rate_limit_backoff))

        self.store = store
        self.opt_in = opt_in
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.allow_refinement = allow_refinement
        self.token_store = token_store
        self._generate_fn = generate_fn or generate_repurpose_proposal
        # Get missing transcripts from the original posts' captions before drafting. On by default with the
        # real engine; off with an injected generate_fn (tests), unless asked for.
        self.fetch_transcripts = (generate_fn is None) if fetch_transcripts is None else bool(fetch_transcripts)
        self._variant_generator = variant_generator

        # Callbacks
        self._ticket_callbacks: list[TicketCallback] = []
        self._progress_callbacks: list[BatchProgressCallback] = []
        self._finished_callbacks: list[BatchFinishedCallback] = []
        if on_ticket_status:
            self._ticket_callbacks.append(on_ticket_status)
        if on_batch_progress:
            self._progress_callbacks.append(on_batch_progress)
        if on_batch_finished:
            self._finished_callbacks.append(on_batch_finished)

        # Threading and internal queue structures
        self._lock = threading.RLock()
        self._rate_limit_lock = threading.Lock()
        self._last_rate_limit_time: float = 0.0

        self._jobs: dict[int, BatchJob] = {}
        self._work_queue: queue.Queue[int] = queue.Queue()
        self._enqueued_ids: set[int] = set()
        self._cancelled_job_ids: set[int] = set()

        self._threads: list[threading.Thread] = []
        self._stop_event = threading.Event()
        self._done_event = threading.Event()
        self._done_event.set()
        self._active_workers: int = 0
        self._finished_notified: bool = False

        # Populate initial jobs if provided
        if jobs:
            self.add_jobs(jobs)

    # ------------------------------------------------------------------ Callbacks
    def add_ticket_callback(self, callback: TicketCallback) -> None:
        """Registers a callback receiving (ticket_id, status, job) updates."""
        with self._lock:
            self._ticket_callbacks.append(callback)

    def add_progress_callback(self, callback: BatchProgressCallback) -> None:
        """Registers a callback receiving (completed, total) updates."""
        with self._lock:
            self._progress_callbacks.append(callback)

    def add_finished_callback(self, callback: BatchFinishedCallback) -> None:
        """Registers a callback receiving the final list of jobs when batch completes."""
        with self._lock:
            self._finished_callbacks.append(callback)

    def _notify_ticket_status(self, job: BatchJob) -> None:
        callbacks = list(self._ticket_callbacks)
        for cb in callbacks:
            try:
                cb(job.ticket_id, job.status, job)
            except Exception as exc:  # noqa: BLE001
                log.warning("Exception in ticket status callback for ticket %s: %s", job.ticket_id, exc)

    def _notify_progress(self) -> None:
        completed = self.completed_count
        total = self.total_count
        callbacks = list(self._progress_callbacks)
        for cb in callbacks:
            try:
                cb(completed, total)
            except Exception as exc:  # noqa: BLE001
                log.warning("Exception in batch progress callback: %s", exc)

    def _notify_finished(self) -> None:
        jobs = self.list_jobs()
        callbacks = list(self._finished_callbacks)
        for cb in callbacks:
            try:
                cb(jobs)
            except Exception as exc:  # noqa: BLE001
                log.warning("Exception in batch finished callback: %s", exc)

    # ------------------------------------------------------------------ Job Ingestion
    def add_job(self, job: BatchJob, *, overwrite: bool = False) -> None:
        """Adds a BatchJob to the queue.

        Args:
            job: The BatchJob instance to enqueue.
            overwrite: If True, replaces any existing job with the same ticket_id.

        Raises:
            ValueError: If a job with the same ticket_id exists and overwrite is False.
        """
        with self._lock:
            if job.ticket_id in self._jobs and not overwrite:
                raise ValueError(f"Job with ticket_id {job.ticket_id} already exists in queue")

            self._jobs[job.ticket_id] = job
            self._cancelled_job_ids.discard(job.ticket_id)

            if job.status == JOB_STATUS_QUEUED and job.ticket_id not in self._enqueued_ids:
                self._work_queue.put(job.ticket_id)
                self._enqueued_ids.add(job.ticket_id)
                self._done_event.clear()
                self._finished_notified = False

        self._notify_ticket_status(job)
        self._notify_progress()

    def add_jobs(self, jobs: Sequence[BatchJob | Any]) -> None:
        """Adds multiple jobs or tickets to the queue."""
        for item in jobs:
            if isinstance(item, BatchJob):
                self.add_job(item)
            else:
                self.add_ticket(item)

    def add_ticket(
        self,
        ticket_or_card: Any,
        *,
        ticket_id: int | None = None,
        brand_voice: str = "",
        platforms: list[str] | None = None,
    ) -> BatchJob:
        """Creates a BatchJob from a ticket/card/row/dict and adds it to the queue.

        Args:
            ticket_or_card: Source ticket, VideoRow, card, or dictionary.
            ticket_id: Optional explicit ticket ID. If omitted, resolved from object.
            brand_voice: Optional brand voice profile guidance.
            platforms: Optional target platforms list.

        Returns:
            The newly created and enqueued BatchJob.
        """
        resolved_id = ticket_id
        if resolved_id is None:
            resolved_id = (
                getattr(ticket_or_card, "ticket_id", None)
                or getattr(ticket_or_card, "id", None)
                or getattr(getattr(ticket_or_card, "row", None), "id", None)
                or (ticket_or_card.get("ticket_id") if isinstance(ticket_or_card, dict) else None)
                or (ticket_or_card.get("id") if isinstance(ticket_or_card, dict) else None)
            )
        if resolved_id is None:
            raise ValueError(f"Could not determine ticket_id for object {ticket_or_card!r}")

        job = BatchJob.from_ticket(
            ticket_id=int(resolved_id),
            ticket_or_row=ticket_or_card,
            store=self.store,
            brand_voice=brand_voice,
            platforms=platforms,
        )
        self.add_job(job)
        return job

    def add_tickets(
        self,
        tickets: Sequence[Any],
        *,
        brand_voice: str = "",
        platforms: list[str] | None = None,
    ) -> list[BatchJob]:
        """Adds a list of tickets or cards to the queue."""
        return [
            self.add_ticket(t, brand_voice=brand_voice, platforms=platforms)
            for t in tickets
        ]

    # ------------------------------------------------------------------ Execution
    def start(self) -> None:
        """Starts background worker threads after verifying Cloud AI privacy opt-in.

        Raises:
            PrivacyOptInRequiredError: If cloud AI features are not explicitly enabled.
        """
        # Privacy Gate: must be opted in before starting any batch generation
        require_cloud_ai_opt_in(self.store, opt_in=self.opt_in)

        with self._lock:
            self._stop_event.clear()

            # Ensure any queued jobs are in the work queue
            for tid, job in self._jobs.items():
                if job.status == JOB_STATUS_QUEUED and tid not in self._enqueued_ids:
                    self._work_queue.put(tid)
                    self._enqueued_ids.add(tid)

            # If there is nothing to process, signal done immediately
            if self._work_queue.empty() and self._active_workers == 0:
                self._done_event.set()
                if not self._finished_notified:
                    self._finished_notified = True
                    self._notify_progress()
                    self._notify_finished()
                return

            self._done_event.clear()
            self._finished_notified = False
            self._ensure_workers_started()

    def _ensure_workers_started(self) -> None:
        """Ensures worker threads up to max_concurrency are running."""
        self._threads = [t for t in self._threads if t.is_alive()]
        while len(self._threads) < self.max_concurrency:
            worker_id = len(self._threads) + 1
            t = threading.Thread(
                target=self._worker_loop,
                daemon=True,
                name=f"ReelayBatchWorker-{worker_id}",
            )
            self._threads.append(t)
            t.start()

    def _worker_loop(self) -> None:
        """Background worker thread processing jobs from the work queue."""
        while not self._stop_event.is_set():
            try:
                ticket_id = self._work_queue.get(timeout=0.2)
            except queue.Empty:
                with self._lock:
                    if self._work_queue.empty() and self._active_workers == 0:
                        self._done_event.set()
                        if not self._finished_notified:
                            self._finished_notified = True
                            self._notify_finished()
                continue

            with self._lock:
                self._active_workers += 1
                job = self._jobs.get(ticket_id)

            if job is None:
                with self._lock:
                    self._active_workers -= 1
                    self._enqueued_ids.discard(ticket_id)
                self._work_queue.task_done()
                continue

            try:
                self._process_single_job(job)
            finally:
                with self._lock:
                    self._active_workers -= 1
                    self._enqueued_ids.discard(ticket_id)
                self._work_queue.task_done()

                with self._lock:
                    if self._work_queue.empty() and self._active_workers == 0:
                        self._done_event.set()
                        if not self._finished_notified:
                            self._finished_notified = True
                            self._notify_finished()

    def _process_single_job(self, job: BatchJob) -> None:
        """Executes proposal generation for a single job with failure isolation and backoff."""
        # 1. Check if cancelled before starting
        with self._lock:
            if job.ticket_id in self._cancelled_job_ids or job.status == JOB_STATUS_CANCELLED:
                job.mark_cancelled()
                self._notify_ticket_status(job)
                self._notify_progress()
                return
            job.mark_running()

        self._notify_ticket_status(job)
        self._notify_progress()

        # 2. Rate limit respect: back off if a 429 was recently encountered
        with self._rate_limit_lock:
            elapsed = time.time() - self._last_rate_limit_time
            if elapsed < self.rate_limit_backoff:
                time.sleep(self.rate_limit_backoff - elapsed)

        # 3. Double-check cancellation right before calling the Meta API
        with self._lock:
            if job.ticket_id in self._cancelled_job_ids:
                job.mark_cancelled()
                self._notify_ticket_status(job)
                self._notify_progress()
                return

        # 4. Generate proposal with strict failure isolation
        try:
            # What the video says comes first: fetch captions when the ticket has none and the video is posted
            # somewhere that has them. Then the profile's voice (rules, banned phrases, footer).
            extra: dict[str, Any] = {}
            row = None
            if self.store is not None and hasattr(self.store, "get_video"):
                try:
                    row = self.store.get_video(job.ticket_id)
                except Exception:  # noqa: BLE001
                    row = None
            if row is not None and not job.source_snapshot.get("transcript", "").strip() and self.fetch_transcripts:
                from .transcripts import fetch_and_save
                fetched = fetch_and_save(self.store, job.ticket_id)
                if fetched:
                    job.source_snapshot["transcript"] = fetched
            if row is not None and hasattr(self.store, "active_brand_profile") and _accepts(self._generate_fn,
                                                                                              "voice"):
                voice = self.store.active_brand_profile(row.profile_id)
                if voice is not None:
                    extra["voice"] = voice
            proposal = self._generate_fn(
                title=job.source_snapshot.get("title", ""),
                description=job.source_snapshot.get("description", ""),
                notes=job.source_snapshot.get("notes", ""),
                transcript=job.source_snapshot.get("transcript", ""),
                brand_voice=job.source_snapshot.get("brand_voice", ""),
                model=self.model,
                base_url=self.base_url,
                api_key=self.api_key,
                token_store=self.token_store,
                store=self.store,
                opt_in=self.opt_in,
                timeout=self.timeout,
                allow_refinement=self.allow_refinement,
                **extra,
            )

            # Optional variant generation hook
            variants = None
            if self._variant_generator is not None:
                try:
                    variants = self._variant_generator(job, proposal)
                except Exception as var_exc:  # noqa: BLE001
                    log.warning("Variant generator failed for ticket %s: %s", job.ticket_id, var_exc)

            with self._lock:
                if job.ticket_id in self._cancelled_job_ids:
                    job.mark_cancelled()
                else:
                    job.mark_ready(proposal=proposal, variants=variants)

        except Exception as exc:  # Failure isolation: never stop other tickets!
            status_code = getattr(exc, "status_code", None)
            if status_code == 429:
                with self._rate_limit_lock:
                    self._last_rate_limit_time = time.time()
                error_desc = f"Rate limit exceeded (HTTP 429). {exc}"
            else:
                error_desc = str(exc) or exc.__class__.__name__

            with self._lock:
                if job.ticket_id in self._cancelled_job_ids:
                    job.mark_cancelled()
                else:
                    job.mark_failed(
                        error_message=error_desc,
                        status_code=status_code,
                        api_key=self.api_key,
                    )

        finally:
            self._notify_ticket_status(job)
            self._notify_progress()

    def process_sync(self) -> list[BatchJob]:
        """Processes all queued jobs sequentially in the calling thread.

        Verifies Cloud AI opt-in and processes all queued jobs in order.
        Useful for synchronous testing and deterministic scripting.

        Returns:
            List of all jobs in the queue.
        """
        require_cloud_ai_opt_in(self.store, opt_in=self.opt_in)

        with self._lock:
            self._done_event.clear()
            self._finished_notified = False

        while True:
            job = None
            with self._lock:
                for candidate in self._jobs.values():
                    if candidate.status == JOB_STATUS_QUEUED and candidate.ticket_id not in self._cancelled_job_ids:
                        job = candidate
                        break
            if job is None:
                break
            self._process_single_job(job)

        with self._lock:
            self._done_event.set()
            if not self._finished_notified:
                self._finished_notified = True
                self._notify_finished()

        return self.list_jobs()

    def process_all(self, *, wait: bool = True, timeout: float | None = None) -> list[BatchJob]:
        """Starts batch processing in background and optionally waits for completion.

        Args:
            wait: If True, blocks until all queued jobs complete or timeout expires.
            timeout: Maximum seconds to wait (None waits indefinitely).

        Returns:
            List of all jobs in the queue.
        """
        self.start()
        if wait:
            self.wait(timeout=timeout)
        return self.list_jobs()

    def wait(self, timeout: float | None = None) -> bool:
        """Blocks until all currently queued jobs are finished.

        Returns:
            True if all jobs finished, False if timed out.
        """
        return self._done_event.wait(timeout=timeout)

    @property
    def is_running(self) -> bool:
        """True if workers are active or work remains in the queue."""
        with self._lock:
            return self._active_workers > 0 or not self._work_queue.empty()

    def stop(self, *, cancel_pending: bool = False, wait: bool = True, timeout: float | None = 5.0) -> None:
        """Signals workers to stop."""
        if cancel_pending:
            self.cancel_batch()
        self._stop_event.set()
        if wait:
            for t in list(self._threads):
                t.join(timeout=timeout)
        with self._lock:
            self._threads.clear()

    # ------------------------------------------------------------------ Cancel & Retry
    def cancel_batch(self) -> int:
        """Cancels all pending/queued jobs in the batch.

        Returns:
            The number of jobs transitioned to cancelled.
        """
        cancelled_count = 0
        jobs_to_notify: list[BatchJob] = []

        with self._lock:
            # Drain the work queue
            while True:
                try:
                    tid = self._work_queue.get_nowait()
                    self._work_queue.task_done()
                    self._enqueued_ids.discard(tid)
                except queue.Empty:
                    break

            for job in self._jobs.values():
                if job.status == JOB_STATUS_QUEUED:
                    job.mark_cancelled()
                    self._cancelled_job_ids.add(job.ticket_id)
                    cancelled_count += 1
                    jobs_to_notify.append(job)
                elif job.status == JOB_STATUS_RUNNING:
                    self._cancelled_job_ids.add(job.ticket_id)

            if self._active_workers == 0:
                self._done_event.set()
                if not self._finished_notified:
                    self._finished_notified = True
                    self._notify_finished()

        for j in jobs_to_notify:
            self._notify_ticket_status(j)
        self._notify_progress()
        return cancelled_count

    cancel_all = cancel_batch  # Alias per specification

    def cancel_job(self, ticket_id: int) -> bool:
        """Cancels a specific job by ticket_id.

        Returns:
            True if the job was queued or running and marked cancelled;
            False if the job was not found or already terminal.
        """
        with self._lock:
            job = self._jobs.get(ticket_id)
            if job is None:
                return False

            if job.status == JOB_STATUS_QUEUED:
                job.mark_cancelled()
                self._cancelled_job_ids.add(ticket_id)
            elif job.status == JOB_STATUS_RUNNING:
                self._cancelled_job_ids.add(ticket_id)
            else:
                return False

        self._notify_ticket_status(job)
        self._notify_progress()
        return True

    def retry_job(self, ticket_id: int) -> bool:
        """Re-queues a failed or cancelled job only.

        Safe retry:
        - Only operates on jobs in 'failed' or 'cancelled' status.
        - Verifies Cloud AI privacy opt-in before re-queueing.
        - Resets error and proposal fields while preserving source snapshot and platforms.

        Returns:
            True if the job was re-queued, False if not retryable or not found.

        Raises:
            PrivacyOptInRequiredError: If cloud AI features are not enabled.
        """
        with self._lock:
            job = self._jobs.get(ticket_id)
            if job is None or not job.can_retry:
                return False

            # Verify Privacy Gate before re-queueing
            require_cloud_ai_opt_in(self.store, opt_in=self.opt_in)

            self._cancelled_job_ids.discard(ticket_id)
            job.status = JOB_STATUS_QUEUED
            job.error = None
            job.error_status_code = None
            job.proposal = None
            job.updated_at = time.time()

            self._done_event.clear()
            self._finished_notified = False
            self._work_queue.put(ticket_id)
            self._enqueued_ids.add(ticket_id)

            # Ensure background workers are alive to process the retry
            self._ensure_workers_started()

        self._notify_ticket_status(job)
        self._notify_progress()
        return True

    # ------------------------------------------------------------------ Query & Summary
    def get_job(self, ticket_id: int) -> BatchJob | None:
        """Retrieves a BatchJob by ticket_id or None if not found."""
        with self._lock:
            return self._jobs.get(ticket_id)

    def list_jobs(self) -> list[BatchJob]:
        """Returns a list of all jobs currently in the queue in insertion order."""
        with self._lock:
            return list(self._jobs.values())

    def get_jobs_by_status(self, status: str) -> list[BatchJob]:
        """Returns all jobs matching a given status."""
        with self._lock:
            return [j for j in self._jobs.values() if j.status == status]

    @property
    def total_count(self) -> int:
        """Total number of jobs in the batch."""
        with self._lock:
            return len(self._jobs)

    @property
    def completed_count(self) -> int:
        """Count of jobs that have reached terminal states (ready, failed, applied, saved, cancelled)."""
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.is_terminal)

    @property
    def pending_count(self) -> int:
        """Count of jobs that are queued or currently running."""
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.is_pending)

    @property
    def ready_jobs(self) -> list[BatchJob]:
        """List of jobs that successfully generated proposals and are ready for creator review."""
        return self.get_jobs_by_status(JOB_STATUS_READY)

    @property
    def failed_jobs(self) -> list[BatchJob]:
        """List of jobs that failed during proposal generation."""
        return self.get_jobs_by_status(JOB_STATUS_FAILED)

    @property
    def cancelled_jobs(self) -> list[BatchJob]:
        """List of jobs that were cancelled."""
        return self.get_jobs_by_status(JOB_STATUS_CANCELLED)

    def summary(self) -> dict[str, int]:
        """Returns a breakdown of job counts by status."""
        with self._lock:
            counts = {s: 0 for s in sorted(VALID_JOB_STATUSES)}
            for j in self._jobs.values():
                counts[j.status] = counts.get(j.status, 0) + 1
            counts["total"] = len(self._jobs)
            return counts

    # ------------------------------------------------------------------ Human Review Actions
    def mark_reviewed(self, ticket_id: int, reviewed: bool = True) -> bool:
        """Marks a job as reviewed and approved by creator."""
        with self._lock:
            job = self._jobs.get(ticket_id)
            if job is None:
                return False
            job.mark_reviewed(reviewed)
        self._notify_ticket_status(job)
        return True

    def mark_applied(self, ticket_id: int, applied: bool = True) -> bool:
        """Marks a proposal as applied to the ticket editor."""
        with self._lock:
            job = self._jobs.get(ticket_id)
            if job is None:
                return False
            job.mark_applied(applied)
        self._notify_ticket_status(job)
        self._notify_progress()
        return True

    def mark_saved(self, ticket_id: int, saved: bool = True) -> bool:
        """Marks a proposal as saved to SQLite ticket."""
        with self._lock:
            job = self._jobs.get(ticket_id)
            if job is None:
                return False
            job.mark_saved(saved)
        self._notify_ticket_status(job)
        self._notify_progress()
        return True

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"<BatchRepurposeQueue total={self.total_count} "
                f"completed={self.completed_count} "
                f"concurrency={self.max_concurrency}>"
            )


# Alias per specification
BatchQueueManager = BatchRepurposeQueue

__all__ = [
    "DEFAULT_PLATFORMS",
    "JOB_STATUS_APPLIED",
    "JOB_STATUS_CANCELLED",
    "JOB_STATUS_FAILED",
    "JOB_STATUS_QUEUED",
    "JOB_STATUS_READY",
    "JOB_STATUS_RUNNING",
    "JOB_STATUS_SAVED",
    "RETRYABLE_JOB_STATUSES",
    "SNAPSHOT_FIELDS",
    "TERMINAL_JOB_STATUSES",
    "VALID_JOB_STATUSES",
    "BatchFinishedCallback",
    "BatchJob",
    "BatchProgressCallback",
    "BatchQueueManager",
    "BatchRepurposeQueue",
    "TicketCallback",
    "extract_source_snapshot",
    "normalize_snapshot",
    "sanitize_error_message",
]
