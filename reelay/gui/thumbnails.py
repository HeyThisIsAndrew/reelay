"""Cover images: fetched in background threads, cached on disk and in memory.

Signed CDN cover URLs expire, so for videos already downloaded the cache
falls back to grabbing a frame from the local file with ffmpeg.

``cover()`` never touches the disk or decodes anything on the GUI thread: reading a cached file,
downloading, grabbing a frame and scaling all happen on worker threads, which hand back a ready
QImage. The GUI thread only turns it into a pixmap.
"""

from __future__ import annotations

import logging
import re
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtGui import QImage, QPixmap

from ..models import VideoItem
from ..ytdl import find_ffmpeg

log = logging.getLogger("reelay")

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
MAX_COVER_HEIGHT = 480  # keeps memory sane: ~170 covers ≈ 60 MB


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


_SSL = _ssl_context()


class _Signals(QObject):
    done = Signal(str, object)  # key, scaled QImage (None on failure)


def _read_scaled(path: Path) -> QImage | None:
    """Decode a cover and scale it down (worker threads only)."""
    image = QImage(str(path))
    if image.isNull():
        return None
    if image.height() > MAX_COVER_HEIGHT:
        image = image.scaledToHeight(MAX_COVER_HEIGHT, Qt.SmoothTransformation)
    return image


class _FetchCover(QRunnable):
    def __init__(self, key: str, url: str, video_path: str, dest: Path, signals: _Signals):
        super().__init__()
        self.key, self.url, self.video_path, self.dest, self.signals = key, url, video_path, dest, signals

    def run(self) -> None:
        if self.dest.is_file():                      # cached already: just decode it here, off the GUI thread
            image = _read_scaled(self.dest)
            if image is not None:
                self.signals.done.emit(self.key, image)
                return
        ok = False
        if self.url:
            try:
                req = urllib.request.Request(self.url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=15, context=_SSL) as resp:
                    data = resp.read()
                if not QImage.fromData(data).isNull():
                    self.dest.write_bytes(data)
                    ok = True
            except Exception as exc:  # noqa: BLE001 — a missing cover is cosmetic
                log.debug("cover download failed for %s: %s", self.key, exc)
        if not ok and self.video_path and Path(self.video_path).is_file():
            ok = self._frame_from_video()
        self.signals.done.emit(self.key, _read_scaled(self.dest) if ok else None)

    def _frame_from_video(self) -> bool:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            return False
        out = self.dest.with_suffix(".jpg")
        try:
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-ss", "1", "-i", self.video_path,
                 "-frames:v", "1", "-vf", f"scale=-2:{MAX_COVER_HEIGHT}", str(out)],
                check=True, timeout=30, capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
            )
            out.replace(self.dest)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("frame grab failed for %s: %s", self.key, exc)
            return False


class ThumbnailCache(QObject):
    loaded = Signal(str)  # key

    def __init__(self, cache_dir: Path, parent=None):
        super().__init__(parent)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(6)
        self._signals = _Signals()
        self._signals.done.connect(self._on_done)
        self._covers: dict[str, QPixmap] = {}
        self._small: dict[tuple[str, int, int], QPixmap] = {}
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self.folder: Path | None = None  # where downloaded videos live

    def _path(self, key: str) -> Path:
        return self.cache_dir / (re.sub(r"[^\w.-]", "_", key) + ".img")

    def cover(self, item: VideoItem) -> QPixmap | None:
        """The cover, or None while it loads (``loaded`` fires later). Never blocks: no disk reads here."""
        pix = self._covers.get(item.key)
        if pix is not None:
            return pix
        if item.key in self._pending or item.key in self._failed:
            return None
        local = item.path(self.folder) if item.filename else None
        self._pending.add(item.key)
        self._pool.start(_FetchCover(item.key, item.thumbnail_url, str(local) if local else "",
                                     self._path(item.key), self._signals))
        return None

    def thumb(self, item: VideoItem, w: int, h: int, ratio: float = 1.0) -> QPixmap | None:
        """Cover cropped to fill a w×h box (cached per size)."""
        key = (item.key, w, h)
        if key in self._small:
            return self._small[key]
        cover = self.cover(item)
        if cover is None:
            return None
        pw, ph = int(w * ratio), int(h * ratio)
        scaled = cover.scaled(pw, ph, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        x, y = (scaled.width() - pw) // 2, (scaled.height() - ph) // 2
        small = scaled.copy(x, y, pw, ph)
        small.setDevicePixelRatio(ratio)
        self._small[key] = small
        return small

    def _on_done(self, key: str, image) -> None:
        self._pending.discard(key)
        if image is not None and not image.isNull():
            self._covers[key] = QPixmap.fromImage(image)
            self.loaded.emit(key)
        else:
            self._failed.add(key)

    def retry(self, key: str) -> None:
        """Allow another attempt (e.g. after the video has been downloaded)."""
        self._failed.discard(key)
