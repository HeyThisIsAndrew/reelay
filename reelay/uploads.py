"""UploadManager: the app-wide YouTube upload queue.

Owns upload workers independently of any screen, so an upload started from
the Board (or, later, the companion app) keeps going whatever is on screen.
Uploads run one at a time.

Thread-safe: `enqueue`, `cancel` and the queries may be called from any thread
(the agent API, the dispatcher). Workers are always started on the manager's
own (GUI) thread via a queued signal.

When an upload ends, `completion_handler(row_id, ok, payload)` runs first
(the Pipeline sets it to update the database) and returns a user message;
then `finished(row_id, ok, message)` is emitted for the UI.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QThread, Signal

log = logging.getLogger("reelay")

CANCELLED = "Upload cancelled."


class UploadFailure(str):
    """Why an upload stopped (a plain message), plus what's known about YouTube's side:
    transient = a connection problem, nothing complete reached YouTube, safe to try again;
    uncertain = it failed after the last piece was sent, so YouTube may have the video."""

    def __new__(cls, message: str, transient: bool = False, uncertain: bool = False):
        obj = super().__new__(cls, message)
        obj.transient, obj.uncertain = transient, uncertain
        return obj


class UploadWorker(QThread):
    progress = Signal(int, float)    # video row id, percent
    done = Signal(int, dict)         # video row id, created video resource
    failed = Signal(int, str)        # video row id, reason

    def __init__(self, row_id: int, client, path: Path, resource: dict, parent=None):
        super().__init__(parent)
        self.row_id, self.client, self.path, self.resource = row_id, client, Path(path), resource
        self._cancel = threading.Event()
        self._last = 0.0
        self.sent, self.total = 0, 0
        self.result: dict | None = None     # set before `done` is emitted, so a shutdown can still record it
        self.thumbnail: Path | None = None  # the ticket's cover: set as the video's thumbnail once it's up

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def last_piece_sent(self) -> bool:
        """The final chunk is on its way (or done): YouTube may finish the video any moment."""
        from .youtube import CHUNK_SIZE
        return bool(self.total) and self.total - self.sent <= CHUNK_SIZE

    def _on_progress(self, sent: int, total: int) -> None:
        self.sent, self.total = sent, total
        now = time.monotonic()
        if sent >= total or now - self._last > 0.2:
            self._last = now
            self.progress.emit(self.row_id, sent / total * 100 if total else 0.0)

    def run(self) -> None:
        import requests

        from .youtube import UploadCancelled, YouTubeError
        try:
            result = self.client.upload(self.path, self.resource, on_progress=self._on_progress,
                                        cancel=self._cancel)
        except UploadCancelled:
            self.failed.emit(self.row_id, CANCELLED)
        except YouTubeError as exc:
            kept_failing = str(exc).startswith("Upload kept failing")      # connection, after many retries
            self.failed.emit(self.row_id, UploadFailure(str(exc), transient=kept_failing,
                                                        uncertain=kept_failing and self.last_piece_sent))
        except requests.RequestException as exc:
            self.failed.emit(self.row_id, UploadFailure(f"Network problem during upload: {exc}", transient=True,
                                                        uncertain=self.last_piece_sent))
        except OSError as exc:
            self.failed.emit(self.row_id, f"Couldn't read the video file: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected upload error")
            self.failed.emit(self.row_id, f"Unexpected error: {exc}")
        else:
            self.result = result or {}
            if self.thumbnail is not None and self.result.get("id"):
                self.result["cover_note"] = self._set_cover(self.result["id"])
            self.done.emit(self.row_id, self.result)

    def _set_cover(self, video_id: str) -> str:
        """Set the cover as the thumbnail. "" when it worked, otherwise why not: the video is up either way, so
        a refused cover is a note for the person, never a failed post."""
        from .covers import CoverError, prepare_cover
        from .youtube import YouTubeError
        try:
            data, mime = prepare_cover(self.thumbnail)
            self.client.set_thumbnail(video_id, data, mime)
        except (CoverError, YouTubeError) as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001 (a network hiccup here mustn't undo the upload)
            log.warning("Couldn't set the thumbnail for %s: %s", video_id, exc)
            return f"The cover couldn't be set ({exc}). Set it in YouTube Studio."
        return ""


@dataclass
class UploadJob:
    row_id: int
    client: object
    path: Path
    resource: dict
    thumbnail: Path | None = None


class UploadManager(QObject):
    progress = Signal(int, float)        # row id, percent
    finished = Signal(int, bool, str)    # row id, succeeded, message for the user
    changed = Signal()                   # queue membership changed
    _kick = Signal()                     # internal: start the next job on our thread

    def __init__(self, parent=None, worker_factory: Callable[..., UploadWorker] = UploadWorker):
        super().__init__(parent)
        self.completion_handler: Callable[[int, bool, object], str] | None = None
        self.progress_handler: Callable[[int, float], None] | None = None
        # pause_handler(row_id, uncertain) records an upload stopped by shutdown, before it stops
        self.pause_handler: Callable[[int, bool], None] | None = None
        self._worker_factory = worker_factory
        self._lock = threading.RLock()
        self._queue: deque[UploadJob] = deque()
        self._worker: UploadWorker | None = None
        self._progress: dict[int, float] = {}
        self._kick.connect(self._start_next)

    # ---------------------------------------------------------------- queries
    @property
    def busy(self) -> bool:
        with self._lock:
            return self._worker is not None or bool(self._queue)

    def is_active(self, row_id: int) -> bool:
        with self._lock:
            return self._worker is not None and self._worker.row_id == row_id

    def is_queued(self, row_id: int) -> bool:
        with self._lock:
            return any(job.row_id == row_id for job in self._queue)

    def queue_position(self, row_id: int) -> int | None:
        """0 = uploading now, 1 = next, …; None if not in the queue."""
        with self._lock:
            if self.is_active(row_id):
                return 0
            for n, job in enumerate(self._queue, 1 if self._worker is not None else 0):
                if job.row_id == row_id:
                    return n
            return None

    def progress_of(self, row_id: int) -> float | None:
        with self._lock:
            if self.is_active(row_id) or self.is_queued(row_id):
                return self._progress.get(row_id, 0.0)
            return None

    # ---------------------------------------------------------------- control
    def enqueue(self, row_id: int, client, path: Path, resource: dict, thumbnail: Path | None = None) -> None:
        with self._lock:
            if self.is_active(row_id) or self.is_queued(row_id):
                raise ValueError("This video is already uploading.")
            self._queue.append(UploadJob(row_id, client, Path(path), resource,
                                         Path(thumbnail) if thumbnail else None))
            self._progress[row_id] = 0.0
        self.changed.emit()
        self._kick.emit()  # queued onto our thread when called from another one

    def cancel(self, row_id: int) -> bool:
        with self._lock:
            job = next((j for j in self._queue if j.row_id == row_id), None)
            if job is not None:
                self._queue.remove(job)
            active = None if job is not None else (self._worker if self.is_active(row_id) else None)
        if job is not None:
            self._finish(row_id, False, CANCELLED)
            return True
        if active is not None:
            active.cancel()
            return True
        return False

    def shutdown(self, timeout_ms: int = 5000) -> None:
        """Reelay is closing. Each waiting or running upload is recorded as paused (to continue
        next time) BEFORE it's stopped; an upload that finishes while stopping is still recorded
        as done, and one whose last piece was already sent is flagged for a person to check."""
        with self._lock:
            queued = [job.row_id for job in self._queue]
            self._queue.clear()
            worker = self._worker
        for row_id in queued:
            self._pause(row_id, uncertain=False)
        if worker is None:
            return
        for signal in (worker.done, worker.failed):
            try:
                signal.disconnect()
            except (RuntimeError, TypeError):
                pass
        if worker.result is None and not worker.last_piece_sent:
            self._pause(worker.row_id, uncertain=False)
            worker.cancel()
            worker.wait(timeout_ms)
            return
        worker.wait(timeout_ms)          # the last piece is on its way: give it a chance to land
        if worker.result is not None and self.completion_handler is not None:
            try:
                self.completion_handler(worker.row_id, True, worker.result)
            except Exception:  # noqa: BLE001
                log.exception("Recording the finished upload failed for row %s", worker.row_id)
        else:
            self._pause(worker.row_id, uncertain=True)
            worker.cancel()

    def _pause(self, row_id: int, uncertain: bool) -> None:
        if self.pause_handler is None:
            return
        try:
            self.pause_handler(row_id, uncertain)
        except Exception:  # noqa: BLE001
            log.exception("Couldn't record the paused upload for row %s", row_id)

    # ---------------------------------------------------------------- internals
    def _start_next(self) -> None:
        with self._lock:
            if self._worker is not None or not self._queue:
                return
            job = self._queue.popleft()
            worker = self._worker_factory(job.row_id, job.client, job.path, job.resource, self)
            if job.thumbnail is not None:
                worker.thumbnail = job.thumbnail
            self._worker = worker
        worker.progress.connect(self._on_progress)
        worker.done.connect(lambda row_id, resource: self._finish(row_id, True, resource))
        worker.failed.connect(lambda row_id, reason: self._finish(row_id, False, reason))
        worker.finished.connect(self._on_worker_finished)
        self.changed.emit()
        worker.start()

    def _on_progress(self, row_id: int, pct: float) -> None:
        with self._lock:
            self._progress[row_id] = pct
        if self.progress_handler is not None:
            try:
                self.progress_handler(row_id, pct)
            except Exception:  # noqa: BLE001
                log.exception("Upload progress handler failed")
        self.progress.emit(row_id, pct)

    def _finish(self, row_id: int, ok: bool, payload) -> None:
        with self._lock:
            self._progress.pop(row_id, None)
        message = payload if isinstance(payload, str) else ""
        if self.completion_handler is not None:
            try:
                message = self.completion_handler(row_id, ok, payload) or message
            except Exception:  # noqa: BLE001
                log.exception("Upload completion handler failed for row %s", row_id)
        self.finished.emit(row_id, ok, message)
        self.changed.emit()

    def _on_worker_finished(self) -> None:
        with self._lock:
            worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
        self._start_next()
