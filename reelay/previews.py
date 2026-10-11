"""Lightweight copies for watching inside Reelay, made with ffmpeg and kept in the cache folder.

- ``make_preview``: an H.264/AAC copy (at most 720p) for videos whose format the built-in player can't
  decode, typically YouTube's VP9 or AV1 above 1080p. The original download is never touched.
- ``make_scrub_frames``: a handful of small frames spread across a video, for hover-scrubbing a card's
  thumbnail. Each frame is a fast seek (no full decode), so even an hour-long video takes about a second.

Both are slow, blocking calls: the app runs them on background threads. Nothing here is ever uploaded or
posted: the cache is only for previewing, and it's pruned automatically.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from .paths import cache_dir
from .video_tools import VideoToolError, _attempt, _check_source, _run, ffmpeg_for

PREVIEW_MAX_HEIGHT = 720
PREVIEW_CACHE_BYTES = 3 * 1024 ** 3        # previews kept: about 3 GB, oldest removed first
SCRUB_FRAMES = 16
SCRUB_HEIGHT = 180
SCRUB_CACHE_DIRS = 400                     # videos with scrub frames kept


def _fingerprint(src: Path) -> str:
    """Changes when the file does (path, size, modification time)."""
    st = src.stat()
    return hashlib.sha1(f"{src.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:20]


def previews_dir() -> Path:
    d = cache_dir() / "previews"
    d.mkdir(parents=True, exist_ok=True)
    return d


def scrub_dir() -> Path:
    d = cache_dir() / "scrub"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------- preview copies
def preview_path(src: Path | str) -> Path | None:
    """Where ``src``'s preview copy lives (whether or not it's been made); None if ``src`` is missing."""
    src = Path(src)
    if not src.is_file():
        return None
    return previews_dir() / f"{_fingerprint(src)}.mp4"


def existing_preview(src: Path | str) -> Path | None:
    path = preview_path(src)
    return path if path is not None and path.is_file() and path.stat().st_size > 0 else None


def make_preview(src: Path | str, ffmpeg: str | None = None, runner=None) -> Path:
    """Make (or reuse) the H.264 preview copy of ``src``. Raises VideoToolError."""
    src = _check_source(src)
    done = existing_preview(src)
    if done is not None:
        return done
    ffmpeg = ffmpeg or ffmpeg_for()
    if not ffmpeg:
        raise VideoToolError("Reelay couldn't find ffmpeg to make a preview copy.")
    dst = preview_path(src)
    tmp = dst.with_suffix(".part.mp4")
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
           "-vf", f"scale=-2:'min({PREVIEW_MAX_HEIGHT},ih)'", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "26", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(tmp)]
    result = _attempt(cmd, tmp, dst, runner or _default_runner())
    prune_previews(keep=result)
    return result


def _default_runner():
    import subprocess
    return subprocess.run


def prune_previews(keep: Path | None = None, limit: int = PREVIEW_CACHE_BYTES) -> int:
    """Remove the oldest preview copies until they fit in ``limit`` bytes. Returns how many were removed."""
    files = sorted((p for p in previews_dir().glob("*.mp4") if p != keep and not p.name.endswith(".part.mp4")),
                   key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in previews_dir().glob("*.mp4"))
    removed = 0
    for p in files:
        if total <= limit:
            break
        total -= p.stat().st_size
        p.unlink(missing_ok=True)
        removed += 1
    return removed


# ---------------------------------------------------------------------- hover-scrub frames
def scrub_frames_dir(src: Path | str) -> Path | None:
    src = Path(src)
    if not src.is_file():
        return None
    return scrub_dir() / _fingerprint(src)


def existing_scrub_frames(src: Path | str) -> list[Path]:
    folder = scrub_frames_dir(src)
    if folder is None or not (folder / "done").is_file():
        return []
    return sorted(folder.glob("f*.jpg"))


def make_scrub_frames(src: Path | str, duration: float | None, count: int = SCRUB_FRAMES,
                      height: int = SCRUB_HEIGHT, ffmpeg: str | None = None, runner=None) -> list[Path]:
    """Small frames spread evenly across the video (cached). Raises VideoToolError."""
    src = _check_source(src)
    done = existing_scrub_frames(src)
    if done:
        return done
    if not duration or duration <= 0:
        raise VideoToolError("Reelay needs the video's length to make scrub frames.")
    ffmpeg = ffmpeg or ffmpeg_for()
    if not ffmpeg:
        raise VideoToolError("Reelay couldn't find ffmpeg to make scrub frames.")
    folder = scrub_frames_dir(src)
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    run = runner or _default_runner()
    for n in range(count):
        at = duration * (n + 0.5) / count               # the middle of each slice, never the black first frame
        out = folder / f"f{n:02d}.jpg"
        _run([ffmpeg, "-y", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", str(src), "-frames:v", "1",
              "-vf", f"scale=-2:{height}", "-q:v", "6", str(out)], runner=run, timeout=60)
    frames = sorted(folder.glob("f*.jpg"))
    if not frames:
        shutil.rmtree(folder, ignore_errors=True)
        raise VideoToolError("ffmpeg didn't produce any frames.")
    (folder / "done").write_text(str(len(frames)), encoding="utf-8")
    _prune_scrub()
    return frames


def _prune_scrub(limit: int = SCRUB_CACHE_DIRS) -> None:
    dirs = sorted((d for d in scrub_dir().iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
    for d in dirs[:max(0, len(dirs) - limit)]:
        shutil.rmtree(d, ignore_errors=True)
