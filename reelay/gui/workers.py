"""Background threads: listing, metadata enrichment, downloading.

Workers never touch the GUI's VideoItems directly; they receive copies and
send results back through Qt signals (delivered on the GUI thread).
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from yt_dlp.utils import DownloadError, ExtractorError

from ..downloader import Progress, UserCancelled, download_item
from ..fetcher import list_videos, load_details
from ..models import VideoItem
from ..naming import render_stem
from ..ytdl import friendly_error

log = logging.getLogger("reelay")


class FetchWorker(QThread):
    fetched = Signal(list, list)   # items, [(url, error)]
    sourceStarted = Signal(str)    # url

    def __init__(self, urls: list[str], ydl_opts: dict, parent=None):
        super().__init__(parent)
        self.urls, self.ydl_opts = urls, ydl_opts

    def run(self) -> None:
        items: list[VideoItem] = []
        errors: list[tuple[str, str]] = []
        for url in self.urls:
            self.sourceStarted.emit(url)
            try:
                _, found = list_videos(url, self.ydl_opts)
                items.extend(found)
            except Exception as exc:  # noqa: BLE001 — report, don't crash the thread
                log.warning("Listing %s failed: %s", url, exc)
                errors.append((url, friendly_error(exc, url)))
        self.fetched.emit(items, errors)


class DetailsWorker(QThread):
    """Fills in date/caption for listings that don't include them (YouTube)."""

    loaded = Signal(str, object)   # key, VideoItem with fresh metadata

    def __init__(self, items: list[VideoItem], ydl_opts: dict, parent=None):
        super().__init__(parent)
        self.items = [replace(i) for i in items]
        self.ydl_opts = ydl_opts
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(self._one, item): item for item in self.items}
            for future in as_completed(futures):
                if self._stop.is_set():
                    pool.shutdown(wait=False, cancel_futures=True)
                    return
                fresh = future.result()
                if fresh is not None:
                    self.loaded.emit(futures[future].key, fresh)

    def _one(self, item: VideoItem) -> VideoItem | None:
        if self._stop.is_set():
            return None
        try:
            return load_details(item, self.ydl_opts)
        except Exception as exc:  # noqa: BLE001 — details are best-effort
            log.debug("details for %s failed: %s", item.key, exc)
            return None


class ProductsWorker(QThread):
    """Looks up the products tagged on TikTok videos (one page read each, a
    couple at a time). Videos it can't read are left for a later try."""

    loaded = Signal(str, object)   # key, list of product records (may be empty)
    finishedAll = Signal(int)      # how many videos had at least one product

    def __init__(self, items: list[VideoItem], ydl_opts: dict, lookup=None, parent=None):
        super().__init__(parent)
        self.items = [replace(i) for i in items]
        self.ydl_opts = ydl_opts
        self._stop = threading.Event()
        if lookup is None:
            from ..products import tiktok_products as lookup
        self.lookup = lookup

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        tagged = 0
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(self._one, item): item for item in self.items}
            for future in as_completed(futures):
                if self._stop.is_set():
                    pool.shutdown(wait=False, cancel_futures=True)
                    return
                found = future.result()
                if found is not None:
                    tagged += bool(found)
                    self.loaded.emit(futures[future].key, [p.to_record() for p in found])
        self.finishedAll.emit(tagged)

    def _one(self, item: VideoItem):
        if self._stop.is_set():
            return None
        try:
            return self.lookup(item.url, self.ydl_opts)
        except Exception as exc:  # noqa: BLE001 — best-effort; tried again next time
            log.debug("products for %s failed: %s", item.key, exc)
            return None


class DownloadWorker(QThread):
    itemStarted = Signal(str, int, int)      # key, number (1-based), total
    itemProgress = Signal(str, object)       # key, Progress
    itemDone = Signal(str, object)           # key, downloaded VideoItem
    itemFailed = Signal(str, str)            # key, reason
    batchDone = Signal(int, int, bool)       # downloaded, failed, cancelled

    def __init__(self, items: list[VideoItem], folder: Path, quality: str, ydl_opts: dict,
                 template: str, positions: dict[str, int], sleep: float = 1.5, parent=None):
        super().__init__(parent)
        self.items = [replace(i) for i in items]
        self.folder, self.quality, self.ydl_opts, self.sleep = folder, quality, ydl_opts, sleep
        self._positions = positions
        self._template = template
        self._custom: dict[str, str] = {i.key: i.custom_name for i in items}
        self._lock = threading.Lock()
        self._cancel = threading.Event()

    # Called from the GUI thread so names typed mid-download still apply.
    def set_naming(self, template: str, custom_names: dict[str, str]) -> None:
        with self._lock:
            self._template = template
            self._custom = dict(custom_names)

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelling(self) -> bool:
        return self._cancel.is_set()

    def _name_for(self, item: VideoItem) -> str:
        with self._lock:
            item.custom_name = self._custom.get(item.key, item.custom_name)
            template = self._template
        return render_stem(template, item, self._positions.get(item.key))

    def run(self) -> None:
        ok = failed = 0
        total = len(self.items)
        for n, item in enumerate(self.items, 1):
            if self._cancel.is_set():
                break
            self.itemStarted.emit(item.key, n, total)
            last_emit = [0.0]

            def on_progress(p: Progress, key=item.key) -> None:
                now = time.monotonic()
                if p.stage != "Downloading" or (p.percent or 0) >= 100 or now - last_emit[0] > 0.12:
                    last_emit[0] = now
                    self.itemProgress.emit(key, p)

            try:
                done = download_item(item, self.folder, self.quality, self.ydl_opts,
                                     name_for=self._name_for, on_progress=on_progress, cancel=self._cancel)
            except UserCancelled:
                break
            except (DownloadError, ExtractorError) as exc:
                if self._cancel.is_set():
                    break
                failed += 1
                self.itemFailed.emit(item.key, friendly_error(exc, item.url))
            except Exception as exc:  # noqa: BLE001 — one video must never kill the batch
                log.exception("Unexpected error downloading %s", item.url)
                failed += 1
                self.itemFailed.emit(item.key, f"Unexpected error: {exc}")
            else:
                ok += 1
                self.itemDone.emit(item.key, done)
            if n < total and self._cancel.wait(self.sleep):
                break
        self.batchDone.emit(ok, failed, self._cancel.is_set())


# ====================================================================== generic
class CallWorker(QThread):
    """Run one blocking call off the GUI thread (sign-in, API reads)."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, parent=None, **kwargs):
        super().__init__(parent)
        self.fn, self.args, self.kwargs = fn, args, kwargs

    def run(self) -> None:
        import requests
        from ..pipeline import PipelineError
        from ..publishers.base import PublishError
        from ..assist import AssistError
        from ..seo import SEOError
        from ..sheet_import import SheetError
        from ..transcripts import TranscriptError
        from ..youtube import YouTubeError
        try:
            result = self.fn(*self.args, **self.kwargs)
        except (YouTubeError, PublishError, SheetError, TranscriptError, PipelineError, AssistError,
                SEOError) as exc:  # plain, secret-free
            self.failed.emit(str(exc))
        except requests.RequestException as exc:
            self.failed.emit(f"Network problem: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("Background call failed")
            self.failed.emit(f"Unexpected error: {exc}")
        else:
            self.succeeded.emit(result)
