"""Hover-scrub previews for Library cards: move the mouse across a thumbnail to flip through the video.

Frames come from ``previews.make_scrub_frames`` (ffmpeg fast seeks), always on a background thread:

- one worker thread at a time, so scrubbing never competes with downloads or the UI;
- only the newest request waits: sweeping the mouse across many cards drops the requests you moved past;
- images are decoded off the main thread (QImage) and turned into pixmaps on it when they arrive.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QImage, QPixmap

from ..models import VideoItem

log = logging.getLogger("reelay")


class _Signals(QObject):
    done = Signal(str, list)        # key, [QImage]


class _Job(QRunnable):
    def __init__(self, key: str, path: str, duration: float, signals: _Signals):
        super().__init__()
        self.key, self.path, self.duration, self.signals = key, path, duration, signals
        self.setAutoDelete(False)

    def run(self) -> None:
        from ..previews import make_scrub_frames
        images: list[QImage] = []
        try:
            for frame in make_scrub_frames(self.path, self.duration):
                image = QImage(str(frame))
                if not image.isNull():
                    images.append(image)
        except Exception as exc:  # noqa: BLE001 — a missing preview is cosmetic
            log.debug("scrub frames failed for %s: %s", self.key, exc)
        self.signals.done.emit(self.key, images)


class ScrubCache(QObject):
    """``frame(item, folder, fraction)`` → the frame at that point, or None (then the cover shows)."""

    loaded = Signal(str)
    MEMORY_VIDEOS = 60

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._signals = _Signals()
        self._signals.done.connect(self._on_done)
        self._frames: dict[str, list[QPixmap]] = {}
        self._failed: set[str] = set()
        self._queued: dict[str, _Job] = {}

    def frame(self, item: VideoItem, folder: Path, fraction: float) -> QPixmap | None:
        frames = self._frames.get(item.key)
        if frames:
            return frames[min(len(frames) - 1, max(0, int(fraction * len(frames))))]
        if item.key in self._failed or item.key in self._queued or not item.duration:
            return None
        path = item.path(folder)
        if path is None or not path.is_file():
            return None
        self._request(item.key, str(path), float(item.duration))
        return None

    def _request(self, key: str, path: str, duration: float) -> None:
        for old_key, job in list(self._queued.items()):          # only the newest request waits
            if self._pool.tryTake(job):                          # (one already running carries on)
                del self._queued[old_key]
        job = _Job(key, path, duration, self._signals)
        self._queued[key] = job
        self._pool.start(job)

    def _on_done(self, key: str, images: list) -> None:
        self._queued.pop(key, None)
        if not images:
            self._failed.add(key)
            return
        if len(self._frames) >= self.MEMORY_VIDEOS:
            self._frames.pop(next(iter(self._frames)))
        self._frames[key] = [QPixmap.fromImage(i) for i in images]
        self.loaded.emit(key)

    def shutdown(self) -> None:
        self._pool.clear()
        self._pool.waitForDone(3000)
