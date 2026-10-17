"""A different sound for one platform: e.g. a royalty-free track for YouTube while TikTok keeps the voice.

Stored per ticket and platform in ``platform_media`` (the sound file, replace or mix, the two volumes, the
copy that was made and a fingerprint of what it was made from). The master video is never touched: the
copy is the master with its sound swapped (or mixed), made with video_tools.replace_audio, which copies the
picture as it is (``-c:v copy``) and only encodes the new sound, so it's quick and lossless.

Copies are made only for platforms that have a different sound, and only when they're needed: right after
the sound is chosen (in the background) and again whenever an input changes (the master, the sound file, or
the settings), which the fingerprint detects. They're named ``<name>.edited.<platform>.mp4`` next to the
master, so Settings → Data → Clean Up Edited Copies sees them.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from .video_tools import VideoToolError, replace_audio

log = logging.getLogger("reelay")
REPLACE, MIX = "replace", "mix"


@dataclass(frozen=True)
class PlatformSound:
    video_row: int
    platform: str
    audio_path: str
    mode: str = REPLACE               # REPLACE: only the new sound · MIX: the original stays underneath
    volume_master: float = 0.25       # the original's volume when mixing
    volume_audio: float = 1.0         # the new sound's volume
    output_path: str | None = None    # the copy last made
    source_fingerprint: str | None = None

    @property
    def name(self) -> str:
        return Path(self.audio_path).name


def output_for(master: Path, platform: str) -> Path:
    stem = master.stem[:-len(".edited")] if master.stem.endswith(".edited") else master.stem
    return master.with_name(f"{stem}.edited.{platform}.mp4")


def fingerprint_for(master: Path, sound: PlatformSound) -> str | None:
    """Changes whenever the copy would come out different. None if the master or the sound file is missing."""
    parts = []
    for path in (master, Path(sound.audio_path)):
        try:
            st = path.stat()
        except OSError:
            return None
        parts.append(f"{path}|{st.st_size}|{st.st_mtime}")
    parts.append(f"{sound.mode}|{sound.volume_master:.2f}|{sound.volume_audio:.2f}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def fresh_copy(sound: PlatformSound, master: Path) -> Path | None:
    """The copy if it exists and was made from exactly these inputs, else None."""
    if not sound.output_path or not Path(sound.output_path).is_file():
        return None
    current = fingerprint_for(master, sound)
    return Path(sound.output_path) if current and current == sound.source_fingerprint else None


def build(store, sound: PlatformSound, master: Path, ffmpeg: str | None = None, runner=None) -> Path:
    """Make (or remake) the platform's copy and remember it. Raises VideoToolError with a plain reason."""
    if not Path(sound.audio_path).is_file():
        raise VideoToolError(f"The sound for this platform ({sound.name}) isn't there any more. Choose it again.")
    mark = fingerprint_for(master, sound)
    dst = output_for(master, sound.platform)
    kwargs = {"ffmpeg": ffmpeg} if ffmpeg else {}
    if runner is not None:
        kwargs["runner"] = runner
    replace_audio(master, sound.audio_path, dst,
                  original_volume=sound.volume_master if sound.mode == MIX else 0.0,
                  track_volume=sound.volume_audio, **kwargs)
    store.save_platform_sound(replace(sound, output_path=str(dst), source_fingerprint=mark))
    from .probe import probe_soon
    probe_soon(store, dst, sound.video_row)
    return dst


def path_for(store, row_id: int, platform: str, master: Path | None, build_now: bool = True) -> Path | None:
    """The file to post on `platform`: the master, or its copy with the platform's own sound (made now if
    it's missing or out of date and `build_now`; otherwise None until the background copy is ready)."""
    if master is None:
        return None
    sound = store.get_platform_sound(row_id, platform)
    if sound is None:
        return master
    copy = fresh_copy(sound, master)
    if copy is not None:
        return copy
    return build(store, sound, master) if build_now else None


# ---------------------------------------------------------------- in the background
_executor: ThreadPoolExecutor | None = None
_pending: set[tuple[int, str]] = set()
_lock = threading.Lock()


def build_soon(store, sound: PlatformSound, master: Path, on_done=None) -> None:
    """Make the copy on a background thread (one at a time, each once). `on_done(error or "")` is called on
    that thread afterwards; the ticket is reported changed either way, so its readiness updates."""
    global _executor
    key = (sound.video_row, sound.platform)
    with _lock:
        if key in _pending:
            return
        _pending.add(key)
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reelay-sound")
        _executor.submit(_job, store, sound, master, on_done)


def _job(store, sound: PlatformSound, master: Path, on_done) -> None:
    error = ""
    try:
        build(store, sound, master)
    except VideoToolError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 (one bad file mustn't stop the builder)
        log.exception("Couldn't make the %s copy", sound.platform)
        error = f"Couldn't make the copy: {exc}"
    finally:
        with _lock:
            _pending.discard((sound.video_row, sound.platform))
    store.media_probed(sound.video_row)
    if on_done is not None:
        on_done(error)


def building(row_id: int, platform: str) -> bool:
    with _lock:
        return (row_id, platform) in _pending


def wait_for_builds(timeout: float = 30) -> None:
    import time
    end = time.time() + timeout
    while time.time() < end:
        with _lock:
            if not _pending:
                return
        time.sleep(0.02)
