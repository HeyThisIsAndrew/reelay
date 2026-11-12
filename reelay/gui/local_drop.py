"""Drop a finished video anywhere on Reelay (or use Add Video Files… in the Library): it becomes a ticket.

One application-wide event filter, so a file dropped on the Library, the Board, a column or the side panel all
do the same thing. Copying a large export can take a few seconds, so it runs off the GUI thread; the new
ticket then opens on the Board, ready for Repurpose.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Signal
from PySide6.QtWidgets import QFileDialog, QWidget

from ..local_import import NO_TRANSCRIPT_NOTE, VIDEO_EXTENSIONS, import_and_transcribe, is_video_file
from .workers import CallWorker

FILE_FILTER = "Videos (" + " ".join(f"*{ext}" for ext in VIDEO_EXTENSIONS) + ")"


def video_paths(mime) -> list[Path]:
    """The local video files in a drag (other files and web links are left alone)."""
    if mime is None or not mime.hasUrls():
        return []
    return [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile() and is_video_file(u.toLocalFile())]


def summary(imported, problems) -> str:
    """One plain status line for what happened."""
    new = [i for i in imported if not i.already_there]
    again = [i for i in imported if i.already_there]
    parts = []
    if new:
        parts.append(f"Added {len(new)} video{'s' if len(new) != 1 else ''} to the Board's first column"
                     if len(new) > 1 else f"Added “{new[0].title}” to the Board's first column")
        missing = [i for i in new if not i.transcript_source]
        if missing:
            parts.append("no transcript yet for " + (f"{len(missing)} of them" if len(new) > 1 else "it") +
                         ": add a caption file with the same name, or paste one on the ticket")
    if again:
        parts.append(f"{len(again)} already had a ticket")
    if problems:
        parts.append(" ".join(problems))
    return ". ".join(p[:1].upper() + p[1:] for p in parts) + "." if parts else ""


class LocalImporter(QObject):
    """Owns the drop filter and the background copy. `imported(row_ids, message)` when done."""

    imported = Signal(list, str)
    failed = Signal(str)

    def __init__(self, store, profile_getter, parent: QWidget):
        super().__init__(parent)
        self.store = store
        self.profile_getter = profile_getter
        self.worker: CallWorker | None = None

    # ---------------------------------------------------------------- entry points
    def choose(self, parent: QWidget | None = None) -> None:
        paths, _ = QFileDialog.getOpenFileNames(parent or self.parent(), "Add finished videos", "", FILE_FILTER)
        if paths:
            self.start([Path(p) for p in paths])

    def start(self, paths: list[Path]) -> bool:
        profile = self.profile_getter()
        if profile is None or not paths or self.busy():
            return False
        self.worker = CallWorker(import_and_transcribe, self.store, profile, paths, parent=self)
        self.worker.succeeded.connect(self._done)
        self.worker.failed.connect(self.failed.emit)
        self.worker.start()
        return True

    def busy(self) -> bool:
        return bool(self.worker and self.worker.isRunning())

    def _done(self, result) -> None:
        imported, problems = result
        self.imported.emit([i.row_id for i in imported], summary(imported, problems))

    # ---------------------------------------------------------------- drag and drop, anywhere in the window
    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt API)
        kind = event.type()
        if kind in (QEvent.DragEnter, QEvent.DragMove):
            if video_paths(event.mimeData()):
                event.acceptProposedAction()
                return True
        elif kind == QEvent.Drop:
            paths = video_paths(event.mimeData())
            if paths:
                event.acceptProposedAction()
                self.start(paths)
                return True
        return False


__all__ = ["LocalImporter", "NO_TRANSCRIPT_NOTE", "summary", "video_paths"]
