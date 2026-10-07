"""UploadManager: the app-wide YouTube upload queue.

Owns upload workers independently of any screen, so an upload started from
the Board (or, later, the companion app) keeps going whatever is on screen.
Uploads run one at a time.

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


class UploadWorker(QThread):
    progress = Signal(int, float)    # video row id, percent
    done = Signal(int, dict)         # video row id, created video resource
    failed = Signal(int, str)        # video row id, reason

    def __init__(self, row_id: int, client, path: Path, resource: dict, parent=None):
        super().__init__(parent)
        self.row_id, self.client, self.path, self.resource = row_id, client, Path(path), resource
        self._cancel = threading.Event()
        self._last = 0.0

    def cancel(self) -> None:
        self._cancel.set()

    def _on_progress(self, sent: int, total: int) -> None:
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
            self.failed.emit(self.row_id, str(exc))
        except requests.RequestException as exc:
            self.failed.emit(self.row_id, f"Network problem during upload: {exc}")
        except OSError as exc:
            self.failed.emit(self.row_id, f"Couldn't read the video file: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected upload error")
            self.failed.emit(self.row_id, f"Unexpected error: {exc}")
        else:
            self.done.emit(self.row_id, result or {})


@dataclass
class UploadJob:
    row_id: int
    client: object
    path: Path
    resource: dict


class UploadManager(QObject):
    progress = Signal(int, float)        # row id, percent
    finished = Signal(int, bool, str)    # row id, succeeded, message for the user
    changed = Signal()                   # queue membership changed

    def __init__(self, parent=None, worker_factory: Callable[..., UploadWorker] = UploadWorker):
        super().__init__(parent)
        self.completion_handler: Callable[[int, bool, object], str] | None = None
        self._worker_factory = worker_factory
        self._queue: deque[UploadJob] = deque()
        self._worker: UploadWorker | None = None
        self._progress: dict[int, float] = {}

    # ---------------------------------------------------------------- queries
    @property
    def busy(self) -> bool:
        return self._worker is not None or bool(self._queue)

    def is_active(self, row_id: int) -> bool:
        return self._worker is not None and self._worker.row_id == row_id

    def is_queued(self, row_id: int) -> bool:
        return any(job.row_id == row_id for job in self._queue)

    def progress_of(self, row_id: int) -> float | None:
        if self.is_active(row_id) or self.is_queued(row_id):
            return self._progress.get(row_id, 0.0)
        return None

    # ---------------------------------------------------------------- control
    def enqueue(self, row_id: int, client, path: Path, resource: dict) -> None:
        if self.is_active(row_id) or self.is_queued(row_id):
            raise ValueError("This video is already uploading.")
        self._queue.append(UploadJob(row_id, client, Path(path), resource))
        self._progress[row_id] = 0.0
        self.changed.emit()
        self._start_next()

    def cancel(self, row_id: int) -> bool:
        for job in list(self._queue):
            if job.row_id == row_id:
                self._queue.remove(job)
                self._finish(row_id, False, CANCELLED)
                return True
        if self.is_active(row_id):
            self._worker.cancel()
            return True
        return False

    def shutdown(self, timeout_ms: int = 5000) -> None:
        self._queue.clear()
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(timeout_ms)

    # ---------------------------------------------------------------- internals
    def _start_next(self) -> None:
        if self._worker is not None or not self._queue:
            return
        job = self._queue.popleft()
        worker = self._worker_factory(job.row_id, job.client, job.path, job.resource, self)
        worker.progress.connect(self._on_progress)
        worker.done.connect(lambda row_id, resource: self._finish(row_id, True, resource))
        worker.failed.connect(lambda row_id, reason: self._finish(row_id, False, reason))
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        self.changed.emit()
        worker.start()

    def _on_progress(self, row_id: int, pct: float) -> None:
        self._progress[row_id] = pct
        self.progress.emit(row_id, pct)

    def _finish(self, row_id: int, ok: bool, payload) -> None:
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
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
        self._start_next()
