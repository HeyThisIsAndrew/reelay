"""What a video file really is: its size, length, displayed width and height, codecs and frame rate.

Platform metadata says what the platform reported when the video was listed; the file on disk can differ (a
different quality was downloaded, or a local edit made a new copy). Readiness checks the file that will
actually be posted, so it asks here.

ffprobe gives the most reliable answer, so it's used when it's installed (Homebrew's ffmpeg ships it). The
ffmpeg bundled with Reelay has no ffprobe, so otherwise the same facts are read from ffmpeg's own summary of
the file (``ffmpeg -i``), which every copy of Reelay can run.

Probing takes a fraction of a second, so results are kept in the database (``media_probes``) keyed by the
file's path, size and modification time: a file that changes is probed again, one that doesn't never is.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .ytdl import find_ffmpeg

TIMEOUT = 30
log = logging.getLogger("reelay")


@dataclass(frozen=True)
class MediaInfo:
    size: int                       # bytes
    duration: float | None = None   # seconds
    width: int | None = None        # as shown (rotation applied)
    height: int | None = None
    video_codec: str = ""
    audio_codec: str = ""           # "" when the file has no sound
    fps: float | None = None
    bitrate: int | None = None      # bits per second, whole file

    @property
    def landscape(self) -> bool:
        return bool(self.width and self.height and self.width > self.height)

    @property
    def vertical(self) -> bool:
        return bool(self.width and self.height and self.height > self.width)

    @property
    def megabytes(self) -> float:
        return self.size / (1024 * 1024)


def find_ffprobe() -> str | None:
    """ffprobe next to the ffmpeg Reelay uses, or on the PATH (or Homebrew's), else None."""
    ff = find_ffmpeg()
    if ff:
        beside = Path(ff).with_name("ffprobe")
        if beside.is_file() and os.access(beside, os.X_OK):
            return str(beside)
    found = shutil.which("ffprobe")
    if found:
        return found
    for folder in ("/opt/homebrew/bin", "/usr/local/bin"):
        candidate = Path(folder) / "ffprobe"
        if candidate.is_file():
            return str(candidate)
    return None


def probe(path: Path | str) -> MediaInfo | None:
    """The file's facts, or None if it isn't there or isn't a video Reelay can read."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return None
    info = _with_ffprobe(path, size) if find_ffprobe() else None
    if info is None:
        info = _with_ffmpeg(path, size)
    return info


def _swap_if_turned(width: int | None, height: int | None, rotation: float | None):
    if width and height and rotation is not None and round(abs(rotation)) % 180 == 90:
        return height, width
    return width, height


def _with_ffprobe(path: Path, size: int) -> MediaInfo | None:
    try:
        out = subprocess.run([find_ffprobe(), "-v", "error", "-print_format", "json", "-show_format",
                              "-show_streams", str(path)], capture_output=True, text=True, timeout=TIMEOUT)
        data = json.loads(out.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if video is None:
        return None
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    rotation = None
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            rotation = float(side["rotation"])
    if rotation is None and (video.get("tags") or {}).get("rotate"):
        rotation = float(video["tags"]["rotate"])
    width, height = _swap_if_turned(video.get("width"), video.get("height"), rotation)
    fmt = data.get("format") or {}
    return MediaInfo(size=size, duration=_float(fmt.get("duration") or video.get("duration")),
                     width=width, height=height, video_codec=video.get("codec_name", ""),
                     audio_codec=(audio or {}).get("codec_name", ""), fps=_rate(video.get("avg_frame_rate")),
                     bitrate=_int(fmt.get("bit_rate")))


_DURATION = re.compile(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)")
_BITRATE = re.compile(r"Duration:.*?bitrate: (\d+) kb/s")
_VIDEO = re.compile(r"Stream #\d+:\d+[^\n]*?: Video: (\w+)([^\n]*)")
_AUDIO = re.compile(r"Stream #\d+:\d+[^\n]*?: Audio: (\w+)")
_SIZE = re.compile(r", (\d{2,5})x(\d{2,5})")
_FPS = re.compile(r", (\d+(?:\.\d+)?) fps")
_ROTATION = re.compile(r"rotation of (-?\d+(?:\.\d+)?) degrees|rotate\s*:\s*(-?\d+)")


def _with_ffmpeg(path: Path, size: int) -> MediaInfo | None:
    ff = find_ffmpeg()
    if not ff:
        return None
    try:   # no output file: ffmpeg prints what it found and exits with an error, which is expected
        out = subprocess.run([ff, "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                             timeout=TIMEOUT).stderr
    except (OSError, subprocess.SubprocessError):
        return None
    video = next((m for m in _VIDEO.finditer(out) if "attached pic" not in m.group(2)), None)
    if video is None:
        return None
    dims = _SIZE.search(video.group(2))
    width, height = (int(dims.group(1)), int(dims.group(2))) if dims else (None, None)
    turn = _ROTATION.search(out)
    rotation = float(turn.group(1) or turn.group(2)) if turn else None
    width, height = _swap_if_turned(width, height, rotation)
    d = _DURATION.search(out)
    duration = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)) if d else None
    fps = _FPS.search(video.group(2))
    audio = _AUDIO.search(out)
    rate = _BITRATE.search(out)
    return MediaInfo(size=size, duration=duration, width=width, height=height, video_codec=video.group(1),
                     audio_codec=audio.group(1) if audio else "", fps=float(fps.group(1)) if fps else None,
                     bitrate=int(rate.group(1)) * 1000 if rate else None)


def _float(value) -> float | None:
    try:
        return float(value) if value not in (None, "", "N/A") else None
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


def _rate(value) -> float | None:
    if not value or value in ("0/0", "N/A"):
        return None
    if "/" in str(value):
        num, den = str(value).split("/", 1)
        return round(float(num) / float(den), 3) if float(den) else None
    return _float(value)


def fingerprint(path: Path | str) -> tuple[int, float] | None:
    """(size, modification time): changes whenever the file does."""
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime


def cached_probe(store, path: Path | str, probe: Callable[[Path], MediaInfo | None] | None = probe
                 ) -> MediaInfo | None:
    """The file's facts from the database if the file hasn't changed since; otherwise probe it (unless
    `probe` is None: then just look) and remember the answer."""
    path = Path(path)
    mark = fingerprint(path)
    if mark is None:
        return None
    known = store.get_probe(str(path), *mark)
    if known is not None or probe is None:
        return known
    info = probe(path)
    if info is not None:
        store.save_probe(str(path), *mark, info)
    return info


# ---------------------------------------------------------------- in the background
_executor: ThreadPoolExecutor | None = None
_pending: set[str] = set()
_lock = threading.Lock()


def probe_soon(store, path: Path | str, row_id: int | None = None, profile_id: int | None = None) -> None:
    """Probe `path` on a background thread (one at a time, each file once) and remember the result. When
    `row_id` is given, the video is reported changed afterwards, so its readiness is checked again against
    the real file. Safe to call often: a file already known, or already waiting, is skipped."""
    global _executor
    path = Path(path)
    key = str(path)
    mark = fingerprint(path)
    if mark is None or store.get_probe(key, *mark) is not None:
        return
    with _lock:
        if key in _pending:
            return
        _pending.add(key)
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reelay-probe")
        _executor.submit(_job, store, path, row_id, profile_id)


def _job(store, path: Path, row_id: int | None, profile_id: int | None) -> None:
    try:
        if cached_probe(store, path) is not None and row_id is not None:
            store.media_probed(row_id, profile_id)
    except Exception:  # noqa: BLE001 (a bad file must never take the prober down)
        log.exception("Couldn't inspect %s", path.name)
    finally:
        with _lock:
            _pending.discard(str(path))


def wait_for_probes(timeout: float = 10) -> None:
    """For tests and shutdown: wait until queued probes are done."""
    import time
    end = time.time() + timeout
    while time.time() < end:
        with _lock:
            if not _pending:
                return
        time.sleep(0.02)
