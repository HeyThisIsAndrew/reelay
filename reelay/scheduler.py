"""The dispatcher: posts scheduled videos to each platform when they're due.

A background thread (no Qt) that, every REELAY_DISPATCH_INTERVAL seconds
(default 60) or when woken by kick():

1. Recovers stale claims: dispatches left "dispatching" by a previous run of
   the app, or held by this run longer than REELAY_DISPATCH_STALE_SECONDS
   (default 1800), go back to pending. Each recovery is logged.
2. Claims due dispatches atomically through the Store (pending →
   dispatching in one UPDATE), so a restart or a second worker can never
   send the same item twice.
3. Hands each claimed item to its platform publisher. YouTube goes to the
   app-wide UploadManager via the Pipeline (one upload at a time); its result
   arrives later through the upload completion handler.
   Only platforms with a publisher are claimed: when a platform's posting
   flag is off (see flags.py) its items are left pending, untouched.
4. Records the outcome per platform: scheduled / published, a retry with
   exponential backoff for transient errors, or failed for permanent ones.
   One platform's outcome never touches another's.

Every write goes through the Store, so the change feed keeps the GUI in sync.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from .models import Dispatch, DispatchStatus, Platform
from .pipeline import Pipeline, utc_iso
from .publishers.base import PermanentError, PublishError, Publisher, TransientError, redact
from .store import Store

log = logging.getLogger("reelay")

DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_STALE_SECONDS = 1800.0
MAX_ATTEMPTS = 6
BACKOFF_BASE_SECONDS = 60
BACKOFF_MAX_SECONDS = 3600


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        log.warning("Ignoring invalid %s=%r", name, os.environ.get(name))
        return default


def backoff_seconds(attempt: int) -> int:
    """1 min, 2, 4, 8, 16, 32 … capped at an hour."""
    return min(BACKOFF_BASE_SECONDS * 2 ** max(attempt - 1, 0), BACKOFF_MAX_SECONDS)


class YouTubeHandoff:
    """Adapter so YouTube dispatches use the UploadManager like everything else does."""
    platform = Platform.YOUTUBE

    def __init__(self, pipeline: Pipeline):
        self.pipeline = pipeline

    def publish(self, job):
        self.pipeline.enqueue_youtube(job.dispatch)
        return None  # handed off; the upload completion handler records the result


class Dispatcher:
    def __init__(self, store: Store, pipeline: Pipeline, publishers: dict[str, Publisher],
                 interval: float | None = None, stale_seconds: float | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 max_attempts: int = MAX_ATTEMPTS):
        self.store, self.pipeline = store, pipeline
        self.publishers: dict[str, Publisher] = {Platform.YOUTUBE: YouTubeHandoff(pipeline), **publishers}
        self.interval = interval if interval is not None else _env_float("REELAY_DISPATCH_INTERVAL",
                                                                         DEFAULT_INTERVAL_SECONDS)
        self.stale_seconds = stale_seconds if stale_seconds is not None else _env_float(
            "REELAY_DISPATCH_STALE_SECONDS", DEFAULT_STALE_SECONDS)
        self.clock = clock
        self.max_attempts = max_attempts
        self.instance_id = pipeline.instance_id
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="reelay-dispatcher", daemon=True)
        self._thread.start()
        log.info("Dispatcher started (checks every %.0fs, stale claims after %.0fs).",
                 self.interval, self.stale_seconds)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("Dispatcher didn't stop within %.0fs (a platform call is still running).", timeout)
            self._thread = None

    def kick(self) -> None:
        """Check now instead of waiting for the next interval."""
        self._wake.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 — keep the dispatcher alive
                log.exception("Dispatcher pass failed")
            self._wake.wait(self.interval)
            self._wake.clear()

    # ---------------------------------------------------------------- one pass
    def run_once(self) -> int:
        """Recover, claim and dispatch everything due. Returns how many were handled."""
        now = self.clock()
        stale_before = utc_iso(now - timedelta(seconds=self.stale_seconds))
        for d in self.store.recover_stale(self.instance_id, stale_before):
            log.warning("Requeued stale %s dispatch for video %s (claimed by %s at %s).",
                        d.platform, d.video_row, "an earlier run" if d.claimed_by != self.instance_id
                        else "this run", d.claimed_at or "unknown time")
        handled = 0
        while not self._stop.is_set():
            claimed = self.store.claim_due(utc_iso(self.clock()), self.instance_id, limit=5,
                                           platforms=list(self.publishers))
            if not claimed:
                break
            for dispatch in claimed:
                if self._stop.is_set():
                    break
                self._dispatch(dispatch)
                handled += 1
        return handled

    def _dispatch(self, dispatch: Dispatch) -> None:
        label = Platform.LABELS.get(dispatch.platform, dispatch.platform)
        publisher = self.publishers.get(dispatch.platform)
        secrets: tuple[str, ...] = ()
        try:
            if publisher is None:
                raise PermanentError(f"{label} publishing isn't available.")
            job = self.pipeline.build_job(dispatch) if dispatch.platform != Platform.YOUTUBE \
                else _youtube_job(dispatch)
            secrets = tuple(str(v) for v in getattr(job, "credentials", {}).values() if isinstance(v, str))
            log.info("Dispatching video %s to %s.", dispatch.video_row, label)
            result = publisher.publish(job)
        except TransientError as exc:
            self._retry(dispatch, redact(str(exc), secrets))
        except PermanentError as exc:
            self._fail(dispatch, redact(str(exc), secrets))
        except PublishError as exc:
            self._retry(dispatch, redact(str(exc), secrets))
        except Exception as exc:  # noqa: BLE001 — unexpected bugs retry, then fail visibly
            log.exception("Unexpected error dispatching video %s to %s", dispatch.video_row, label)
            self._retry(dispatch, redact(f"{label}: unexpected error ({type(exc).__name__})", secrets))
        else:
            if result is not None:
                self.store.finish_dispatch(dispatch.id, result.status, result.remote_id, result.remote_url)
                if dispatch.account_id:
                    self.store.mark_verified(dispatch.account_id)
                log.info("%s: video %s %s.", label, dispatch.video_row, result.status)
                self.pipeline.refresh_post(dispatch.video_row)

    def _retry(self, dispatch: Dispatch, message: str) -> None:
        attempt = dispatch.attempts + 1
        if attempt >= self.max_attempts:
            self._fail(dispatch, f"{message} (gave up after {attempt} attempts)")
            return
        wait = backoff_seconds(attempt)
        log.warning("Video %s on %s failed (attempt %d), retrying in %ds: %s",
                    dispatch.video_row, dispatch.platform, attempt, wait, message)
        self.store.retry_dispatch(dispatch.id, f"{message} Retrying.",
                                  utc_iso(self.clock() + timedelta(seconds=wait)))
        self.pipeline.refresh_post(dispatch.video_row)

    def _fail(self, dispatch: Dispatch, message: str) -> None:
        log.warning("Video %s on %s failed permanently: %s", dispatch.video_row, dispatch.platform, message)
        self.store.finish_dispatch(dispatch.id, DispatchStatus.FAILED, error=message)
        self.pipeline.refresh_post(dispatch.video_row)


class _YouTubeJob:
    def __init__(self, dispatch: Dispatch):
        self.dispatch = dispatch
        self.credentials: dict = {}


def _youtube_job(dispatch: Dispatch) -> _YouTubeJob:
    return _YouTubeJob(dispatch)
