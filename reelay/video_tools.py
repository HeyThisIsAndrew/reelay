"""Local video edits with ffmpeg: remove the sound, swap in a track, burn in captions.

Everything runs on this computer and nothing is uploaded. The original file is
never changed: each edit writes a new file next to it (``<name>.edited.mp4``)
and the ticket remembers which version to post (see ``edited_path`` and
``set_edit``). "Use Original" just forgets the edit.

Captions come from a subtitle file the person already has (.srt, .vtt, .ass:
CapCut, Premiere and YouTube Studio all export these) or, when the optional
``faster-whisper`` package is installed, from a transcript made on this
computer. Burning captions needs an ffmpeg built with libass; the copy that
ships with Reelay has it, Homebrew's usually doesn't, so ``ffmpeg_for`` picks.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .ytdl import find_ffmpeg

SUBTITLE_TYPES = (".srt", ".vtt", ".ass", ".ssa")
AUDIO_TYPES = (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg")
EDIT_KEY = "video_edit.{row}"     # setting: JSON {"file", "steps", "at"}
CAPTION_STYLES = {
    # name: libass force_style (sized for 9:16 video, readable on a phone)
    "bold": "FontName=Helvetica,FontSize=13,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            "BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=60",
    "boxed": "FontName=Helvetica,FontSize=12,Bold=1,PrimaryColour=&H00FFFFFF,BackColour=&H99000000,"
             "BorderStyle=3,Outline=6,Shadow=0,Alignment=2,MarginV=60",
}
CAPTION_STYLE_LABELS = {"bold": "White with outline", "boxed": "White on a dark box"}


class VideoToolError(Exception):
    """Plain-language reason an edit couldn't be made."""


# ---------------------------------------------------------------------- ffmpeg
@lru_cache(maxsize=8)
def _filters(ffmpeg: str) -> str:
    try:
        return subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True,
                              timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def can_burn_captions(ffmpeg: str | None) -> bool:
    return bool(ffmpeg) and " subtitles " in _filters(ffmpeg)


def _bundled_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 — not installed / no binary for this platform
        return None


def ffmpeg_for(captions: bool = False) -> str | None:
    """The ffmpeg to use; for captions, one that can draw them (libass)."""
    first = find_ffmpeg()
    if not captions:
        return first
    for candidate in (first, _bundled_ffmpeg()):
        if can_burn_captions(candidate):
            return candidate
    return None


def availability() -> dict[str, tuple[bool, str]]:
    """What can be done on this computer, with the reason when something can't."""
    ff = find_ffmpeg()
    out = {"sound": (bool(ff), "" if ff else "ffmpeg isn't available, so video edits are off.")}
    out["captions"] = (bool(ffmpeg_for(captions=True)),
                       "" if ffmpeg_for(captions=True) else
                       "This computer's ffmpeg can't draw captions (it needs libass).")
    ok, why = whisper_available()
    out["transcribe"] = (ok, why)
    return out


def _run(cmd: list[str], cwd: str | None = None, runner=subprocess.run, timeout: int = 1800) -> None:
    try:
        proc = runner(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise VideoToolError("The edit took too long and was stopped.") from None
    except OSError:
        raise VideoToolError("Couldn't start ffmpeg.") from None
    if proc.returncode != 0:
        lines = [ln for ln in (proc.stderr or "").strip().splitlines() if ln.strip()]
        detail = lines[-1][:200] if lines else f"exit code {proc.returncode}"
        raise VideoToolError(f"ffmpeg couldn't make the edit ({detail}).")


def output_path(src: Path | str) -> Path:
    """Where an edit of ``src`` is written: next to it, never over it."""
    src = Path(src)
    stem = re.sub(r"\.edited$", "", src.stem)
    return src.with_name(f"{stem}.edited.mp4")


def _check_source(src: Path | str) -> Path:
    src = Path(src)
    if not src.is_file():
        raise VideoToolError("The video file isn't on this computer. Download it first.")
    return src


def _attempt(cmd: list[str], tmp: Path, dst: Path, runner, cwd: str | None = None) -> Path:
    """Run an edit into a temporary file and move it into place only if it worked."""
    try:
        _run(cmd, cwd=cwd, runner=runner)
        return _finish(tmp, dst)
    except VideoToolError:
        tmp.unlink(missing_ok=True)
        raise


def _finish(tmp: Path, dst: Path) -> Path:
    if not tmp.is_file() or tmp.stat().st_size == 0:
        raise VideoToolError("ffmpeg didn't produce a video.")
    tmp.replace(dst)
    return dst


# ---------------------------------------------------------------------- edits
def strip_audio(src: Path | str, dst: Path | str | None = None, ffmpeg: str | None = None,
                runner=subprocess.run) -> Path:
    """A copy with no sound at all (the picture isn't re-encoded, so it's quick and lossless)."""
    src = _check_source(src)
    ff = ffmpeg or ffmpeg_for()
    if not ff:
        raise VideoToolError("ffmpeg isn't available, so video edits are off.")
    dst = Path(dst) if dst else output_path(src)
    tmp = dst.with_name(dst.stem + ".part.mp4")
    return _attempt([ff, "-y", "-loglevel", "error", "-i", str(src), "-map", "0:v:0", "-c:v", "copy", "-an",
          "-movflags", "+faststart", str(tmp)], tmp, dst, runner)


def replace_audio(src: Path | str, track: Path | str, dst: Path | str | None = None,
                  original_volume: float = 0.0, track_volume: float = 1.0, ffmpeg: str | None = None,
                  runner=subprocess.run) -> Path:
    """Swap the sound for ``track`` (looped if it's shorter than the video, cut to the video's length).

    ``original_volume`` above 0 keeps the original sound underneath (e.g. 0.3 for a voice-over
    that should stay audible). Use music you have the rights to: royalty-free or your own.
    """
    src = _check_source(src)
    track = Path(track)
    if not track.is_file():
        raise VideoToolError("The sound file isn't there any more. Pick it again.")
    ff = ffmpeg or ffmpeg_for()
    if not ff:
        raise VideoToolError("ffmpeg isn't available, so video edits are off.")
    dst = Path(dst) if dst else output_path(src)
    tmp = dst.with_name(dst.stem + ".part.mp4")
    cmd = [ff, "-y", "-loglevel", "error", "-i", str(src), "-stream_loop", "-1", "-i", str(track)]
    tv = max(0.0, float(track_volume))
    if original_volume > 0:
        ov = min(2.0, float(original_volume))
        cmd += ["-filter_complex",
                f"[1:a]volume={tv:.2f}[m];[0:a]volume={ov:.2f}[o];[o][m]amix=inputs=2:duration=first[a]",
                "-map", "0:v:0", "-map", "[a]"]
    else:
        cmd += ["-filter_complex", f"[1:a]volume={tv:.2f}[a]", "-map", "0:v:0", "-map", "[a]", "-shortest"]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(tmp)]
    try:
        return _attempt(cmd, tmp, dst, runner)
    except VideoToolError as exc:
        if original_volume > 0:
            raise VideoToolError(f"{exc} If the video has no sound of its own, set its volume to 0.") from None
        raise


def burn_captions(src: Path | str, subtitles: Path | str, dst: Path | str | None = None, style: str = "bold",
                  ffmpeg: str | None = None, runner=subprocess.run) -> Path:
    """Draw captions onto the picture (permanent, visible everywhere the video is posted)."""
    src = _check_source(src)
    subtitles = Path(subtitles)
    if subtitles.suffix.lower() not in SUBTITLE_TYPES or not subtitles.is_file():
        raise VideoToolError("Pick a caption file (.srt, .vtt or .ass).")
    ff = ffmpeg or ffmpeg_for(captions=True)
    if not ff or not can_burn_captions(ff):
        raise VideoToolError("This computer's ffmpeg can't draw captions (it needs libass).")
    dst = Path(dst) if dst else output_path(src)
    tmp = dst.with_name(dst.stem + ".part.mp4")
    force = CAPTION_STYLES.get(style, CAPTION_STYLES["bold"])
    # The subtitles filter has its own escaping rules for paths; running from a temp folder
    # with a plain relative name sidesteps them for any file name.
    with tempfile.TemporaryDirectory(prefix="reelay-captions-") as work:
        local = f"captions{subtitles.suffix.lower()}"
        shutil.copyfile(subtitles, Path(work) / local)
        vf = f"subtitles={local}" + ("" if local.endswith((".ass", ".ssa")) else f":force_style='{force}'")
        return _attempt([ff, "-y", "-loglevel", "error", "-i", str(src.resolve()), "-vf", vf,
                         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                         "-c:a", "copy", "-movflags", "+faststart", str(tmp.resolve())], tmp, dst, runner, cwd=work)


# ---------------------------------------------------------------------- captions
@dataclass
class Segment:
    start: float
    end: float
    text: str


def _stamp(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def chunk(segments: list[Segment], max_chars: int = 32) -> list[Segment]:
    """Short caption lines for vertical video: split long segments, timing shared by length."""
    out: list[Segment] = []
    for seg in segments:
        words = seg.text.split()
        if not words:
            continue
        lines, line = [], ""
        for w in words:
            if line and len(line) + 1 + len(w) > max_chars:
                lines.append(line)
                line = w
            else:
                line = f"{line} {w}".strip()
        lines.append(line)
        total = sum(len(x) for x in lines)
        t = seg.start
        for text in lines:
            share = (seg.end - seg.start) * len(text) / total
            out.append(Segment(t, t + share, text))
            t += share
    return out


def to_srt(segments: list[Segment]) -> str:
    return "".join(f"{i}\n{_stamp(s.start)} --> {_stamp(s.end)}\n{s.text.strip()}\n\n"
                   for i, s in enumerate(segments, 1))


def whisper_available() -> tuple[bool, str]:
    try:
        import faster_whisper  # noqa: F401
    except Exception:  # noqa: BLE001
        return False, ("Automatic captions need the free faster-whisper add-on "
                       "(pip install faster-whisper). Or pick a caption file you already have.")
    return True, ""


def transcribe(src: Path | str, model_size: str = "base", language: str | None = None,
               model=None) -> list[Segment]:
    """Timed transcript of the video's speech, made on this computer (first use downloads the model)."""
    src = _check_source(src)
    if model is None:
        ok, why = whisper_available()
        if not ok:
            raise VideoToolError(why)
        from faster_whisper import WhisperModel
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(str(src), language=language, vad_filter=True)
    out = [Segment(float(s.start), float(s.end), s.text.strip()) for s in segments if s.text.strip()]
    if not out:
        raise VideoToolError("No speech was found in this video.")
    return out


def auto_captions(src: Path | str, dst_srt: Path | str | None = None, model_size: str = "base",
                  model=None) -> Path:
    """Transcribe and save an .srt next to the video (so it can be checked and fixed before burning in)."""
    src = Path(src)
    dst_srt = Path(dst_srt) if dst_srt else src.with_name(re.sub(r"\.edited$", "", src.stem) + ".captions.srt")
    dst_srt.write_text(to_srt(chunk(transcribe(src, model_size, model=model))), encoding="utf-8")
    return dst_srt


# ---------------------------------------------------------------------- which file gets posted
def get_edit(store, row_id: int) -> dict | None:
    raw = store.get_setting(EDIT_KEY.format(row=row_id), "")
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("file") else None


def set_edit(store, row_id: int, path: Path | str, steps: list[str]) -> dict:
    data = {"file": str(path), "steps": list(steps), "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    store.set_setting(EDIT_KEY.format(row=row_id), json.dumps(data))
    return data


def clear_edit(store, row_id: int) -> None:
    store.set_setting(EDIT_KEY.format(row=row_id), "")


def edited_path(store, row_id: int) -> Path | None:
    """The edited version to post instead of the original, if there is one on disk."""
    data = get_edit(store, row_id)
    if not data:
        return None
    path = Path(data["file"])
    return path if path.is_file() else None


STEP_LABELS = {"strip": "sound removed", "replace": "new sound", "captions": "captions"}


def describe(data: dict | None) -> str:
    if not data:
        return ""
    steps = [STEP_LABELS.get(s, s) for s in data.get("steps", [])]
    return ", ".join(dict.fromkeys(steps)) or "edited"
