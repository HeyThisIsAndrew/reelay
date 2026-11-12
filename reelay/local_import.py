"""Bring in a finished video from this computer: one file in, one ticket out.

Everything else in the Library starts from a link (a video that is already posted somewhere). A creator's
newest video usually isn't posted anywhere yet: it's an .mp4 that just came out of the editor. This turns
that file into a ticket the same way a download does:

- the file is copied into the profile's library folder (the original is never moved or changed), so the
  ticket keeps working if the export folder is cleaned up;
- the ticket's title comes from the file name, its length and size from the file itself;
- provenance is recorded as "Own content" from "This computer";
- a caption file next to the video with the same name (``My video.srt``, ``.vtt`` or ``.txt``; DaVinci
  Resolve, Premiere and Final Cut all export these) becomes the ticket's transcript, which is what the
  Repurpose Engine writes from. Without one the ticket says so plainly.

The same file brought in twice is recognised (by its content, not its name) and gives the existing ticket.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import VideoItem
from .probe import probe
from .transcripts import captions_to_text

log = logging.getLogger("reelay")

LOCAL_PLATFORM = "Local"            # VideoItem.platform for files brought in from this computer
OWN_CONTENT = "Own content"
VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".webm", ".mkv")
CAPTION_EXTENSIONS = (".srt", ".vtt", ".txt")
_HASH_BYTES = 8 * 1024 * 1024        # the first 8 MB plus the size identify a file well enough
NO_TRANSCRIPT_NOTE = ("No transcript yet. Drafts are written from what the video says, so add one before "
                      "Repurpose: put a caption file with the same name next to the video (My video.srt), "
                      "paste it into the Transcript field, or fetch captions once the video is on YouTube.")


class LocalImportError(Exception):
    """A plain-language reason a file couldn't be brought in."""


@dataclass
class Imported:
    row_id: int
    title: str
    path: Path
    transcript_source: str      # "caption file" | "" (none)
    already_there: bool = False


def is_video_file(path: Path | str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def file_id(path: Path) -> str:
    """A stable id for the file's content: the same export brought in twice is one ticket."""
    h = hashlib.sha1()
    size = path.stat().st_size
    h.update(str(size).encode())
    with path.open("rb") as f:
        h.update(f.read(_HASH_BYTES))
    return h.hexdigest()[:16]


def title_from_name(path: Path) -> str:
    """"my_new_video FINAL v2.mp4" → "My new video". Export suffixes people add are dropped; hyphens inside a
    word or number ("16-35", "a7-iv") are kept."""
    stem = re.sub(r"[_.]+|\s+-+\s+|^-+|-+$", " ", path.stem)
    stem = re.sub(r"\b(final|export(ed)?|master|v\d+|render|4k|1080p|2160p)\b", " ", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\s+", " ", stem).strip()
    return (stem[:1].upper() + stem[1:]) if stem else path.stem


def sidecar_transcript(video: Path) -> str:
    """Text of a caption file next to the video with the same name ("" when there isn't one)."""
    for ext in CAPTION_EXTENSIONS:
        for candidate in (video.with_suffix(ext), video.with_suffix(ext.upper())):
            if candidate.is_file():
                try:
                    raw = candidate.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    continue
                text = raw.strip() if ext == ".txt" else captions_to_text(raw)
                if text:
                    return text
    return ""


def _library_copy(src: Path, folder: Path, vid: str) -> Path:
    """Copy into the library folder, keeping the readable name; never overwrite a different file."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / src.name
    if target.exists():
        if target.resolve() == src.resolve():
            return target                                 # already in the library folder
        if target.stat().st_size == src.stat().st_size and file_id(target) == vid:
            return target                                 # the same file, copied before
        target = folder / f"{src.stem}_{vid[:6]}{src.suffix}"
    if not target.exists():
        tmp = target.with_name(f".{target.name}.part")
        shutil.copy2(src, tmp)
        tmp.replace(target)
    return target


def import_files(store, profile, paths, copy_into_library: bool = True) -> tuple[list[Imported], list[str]]:
    """Bring in video files for a profile. Returns (imported, problems), problems as plain sentences."""
    imported: list[Imported] = []
    problems: list[str] = []
    for raw in paths:
        src = Path(raw).expanduser()
        if not src.is_file():
            problems.append(f"{src.name}: not found.")
            continue
        if not is_video_file(src):
            problems.append(f"{src.name}: not a video file Reelay can post ({', '.join(VIDEO_EXTENSIONS)}).")
            continue
        try:
            vid = file_id(src)
        except OSError as exc:
            problems.append(f"{src.name}: couldn't be read ({exc.strerror or exc}).")
            continue
        existing = store.video_row_id(profile.id, LOCAL_PLATFORM, vid)
        if existing is not None:
            row = store.get_video(existing)
            imported.append(Imported(existing, row.item.title if row else src.stem, src,
                                     "", already_there=True))
            continue
        try:
            dest = _library_copy(src, Path(profile.output_folder), vid) if copy_into_library else src
        except OSError as exc:
            problems.append(f"{src.name}: couldn't be copied into the library folder ({exc.strerror or exc}).")
            continue
        info = probe(dest)
        item = VideoItem(
            platform=LOCAL_PLATFORM, video_id=vid, url="", title=title_from_name(src),
            upload_date="", duration=info.duration if info else None,
            width=info.width if info else None, height=info.height if info else None,
            uploader=profile.name, filename=dest.name, folder=str(dest.parent),
            downloaded_at=datetime.now().isoformat(timespec="seconds"),
        )
        row_id = store.save_videos(profile.id, [item], authorization=OWN_CONTENT)[item.key]
        transcript = sidecar_transcript(src)
        if transcript:
            store.update_ticket(row_id, {"transcript": transcript})
        imported.append(Imported(row_id, item.title, dest, "caption file" if transcript else ""))
        log.info("Imported %s as ticket %s (%s)", src.name, row_id,
                 "with transcript" if transcript else "no transcript")
    return imported, problems


def transcribe_missing(store, imported: list[Imported], transcribe=None) -> int:
    """For new tickets without a caption file: transcribe the speech on this computer when the optional
    faster-whisper add-on is installed (video_tools.transcribe). Nothing is sent anywhere. Returns how many
    tickets got a transcript; does nothing without the add-on."""
    from .video_tools import VideoToolError, whisper_available
    from .video_tools import transcribe as local_transcribe
    if transcribe is None:
        if not whisper_available()[0]:
            return 0
        transcribe = local_transcribe
    done = 0
    for item in imported:
        if item.already_there or item.transcript_source:
            continue
        try:
            segments = transcribe(item.path)
        except VideoToolError as exc:
            log.info("No transcript for %s: %s", item.path.name, exc)
            continue
        text = " ".join(s.text for s in segments).strip()
        if text:
            store.update_ticket(item.row_id, {"transcript": text})
            item.transcript_source = "speech on this computer"
            done += 1
    return done


def import_and_transcribe(store, profile, paths) -> tuple[list[Imported], list[str]]:
    """What the window runs off the GUI thread: bring the files in, then transcribe any without captions."""
    imported, problems = import_files(store, profile, paths)
    transcribe_missing(store, imported)
    return imported, problems
