"""The ticket's VIDEO section: remove or replace the sound, add captions, on this computer."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QPushButton, QSlider, QVBoxLayout, QWidget)

from .. import video_tools as vt
from .theme import set_prop
from .tips import tip
from .workers import CallWorker


class SoundDialog(QDialog):
    """Pick a track and how loud the original stays underneath."""

    RIGHTS = ("Use music you have the rights to: royalty-free tracks or your own. Trending sounds from TikTok or "
              "Instagram can't be reused on other platforms. The track loops if it's shorter than the video and is "
              "cut at the video's end.")

    def __init__(self, parent=None, start_folder: str = "", title: str = "Replace Sound", note: str = "",
                 ok_text: str = "Make Edited Copy"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.track: Path | None = None
        self.start_folder = start_folder
        self._ok_text = ok_text
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        note = QLabel(f"{note}\n\n{self.RIGHTS}" if note else self.RIGHTS)
        note.setWordWrap(True)
        note.setProperty("muted", True)
        lay.addWidget(note)
        form = QFormLayout()
        pick = QHBoxLayout()
        self.track_label = QLabel("No sound picked")
        self.track_label.setProperty("faint", True)
        choose = tip(QPushButton("Choose Sound…"), "An audio file: MP3, M4A, WAV, AAC, FLAC or OGG")
        choose.clicked.connect(self._choose)
        pick.addWidget(self.track_label, 1)
        pick.addWidget(choose)
        form.addRow("Sound", pick)
        self.original = tip(QSlider(Qt.Horizontal), "0% removes the original sound completely. Raise it to keep "
                                                    "a voice-over audible under the music.")
        self.original.setRange(0, 100)
        self.original.setValue(0)
        self.original_label = QLabel()
        self.original.valueChanged.connect(lambda v: self.original_label.setText(f"{v}%"))
        self.original.valueChanged.emit(0)
        orow = QHBoxLayout()
        orow.addWidget(self.original, 1)
        orow.addWidget(self.original_label)
        form.addRow("Original sound", orow)
        self.volume = tip(QSlider(Qt.Horizontal), "How loud the new sound is")
        self.volume.setRange(10, 150)
        self.volume.setValue(100)
        self.volume_label = QLabel()
        self.volume.valueChanged.connect(lambda v: self.volume_label.setText(f"{v}%"))
        self.volume.valueChanged.emit(100)
        vrow = QHBoxLayout()
        vrow.addWidget(self.volume, 1)
        vrow.addWidget(self.volume_label)
        form.addRow("New sound", vrow)
        lay.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        self.ok = buttons.button(QDialogButtonBox.Ok)
        self.ok.setText(self._ok_text)
        self.ok.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _choose(self) -> None:
        exts = " ".join(f"*{e}" for e in vt.AUDIO_TYPES)
        path, _ = QFileDialog.getOpenFileName(self, "Choose Sound", self.start_folder, f"Audio ({exts})")
        if path:
            self.set_track(Path(path))

    def set_track(self, path: Path) -> None:
        self.track = path
        self.track_label.setText(path.name)
        self.ok.setEnabled(True)

    def options(self) -> dict:
        return {"original_volume": self.original.value() / 100, "track_volume": self.volume.value() / 100}

    def preset(self, track: Path | None, original_volume: float, track_volume: float) -> None:
        """Start from a sound already chosen (to change it)."""
        if track is not None:
            self.set_track(Path(track))
        self.original.setValue(round(original_volume * 100))
        self.volume.setValue(round(track_volume * 100))


class VideoTools(QWidget):
    """Buttons for local edits plus a line saying which version will be posted."""

    editChanged = Signal(int)          # video row id: the file to post changed
    message = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.store = None
        self.row_id: int | None = None
        self.original: Path | None = None
        self.worker: CallWorker | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setProperty("muted", True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)
        row = QHBoxLayout()
        self.strip_btn = tip(QPushButton("Remove Sound"), "Make a copy with no sound at all, e.g. to drop a "
                                                          "trending TikTok sound before posting elsewhere")
        self.strip_btn.clicked.connect(self._strip)
        self.replace_btn = tip(QPushButton("Replace Sound…"), "Make a copy with music you have the rights to")
        self.replace_btn.clicked.connect(self._replace)
        row.addWidget(self.strip_btn)
        row.addWidget(self.replace_btn)
        lay.addLayout(row)
        crow = QHBoxLayout()
        self.caption_style = tip(QComboBox(), "How burned-in captions look")
        for key, label in vt.CAPTION_STYLE_LABELS.items():
            self.caption_style.addItem(label, key)
        self.caption_style.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.caption_style.setMinimumContentsLength(8)
        self.captions_btn = tip(QPushButton("Add Captions…"),
                                "Draw captions onto the video from a caption file (.srt, .vtt or .ass), or make "
                                "them automatically if the faster-whisper add-on is installed")
        self.captions_btn.clicked.connect(self._captions)
        crow.addWidget(self.caption_style, 1)
        crow.addWidget(self.captions_btn)
        lay.addLayout(crow)
        brow = QHBoxLayout()
        self.show_btn = tip(QPushButton("Show File"), "Show the file that will be posted in Finder")
        set_prop(self.show_btn, "flat")
        self.show_btn.clicked.connect(self._reveal)
        self.original_btn = tip(QPushButton("Use Original"), "Post the original download again. The edited copy "
                                                             "stays on disk.")
        set_prop(self.original_btn, "flat")
        self.original_btn.clicked.connect(self._use_original)
        brow.addWidget(self.show_btn)
        brow.addStretch()
        brow.addWidget(self.original_btn)
        lay.addLayout(brow)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setProperty("faint", True)
        lay.addWidget(self.note)

    # ------------------------------------------------------------------ state
    def set_video(self, store, row_id: int, original: Path | None) -> None:
        self.store, self.row_id, self.original = store, row_id, original
        self.refresh()

    def source(self) -> Path | None:
        """What the next edit starts from: the current edited copy, so edits stack."""
        edited = vt.edited_path(self.store, self.row_id) if self.store and self.row_id is not None else None
        return edited or self.original

    def summary(self) -> str:
        data = vt.get_edit(self.store, self.row_id) if self.store and self.row_id is not None else None
        return f"Edited: {vt.describe(data)}" if data and vt.edited_path(self.store, self.row_id) else "Original"

    def refresh(self) -> None:
        busy = bool(self.worker and self.worker.isRunning())
        have = bool(self.original and self.original.is_file())
        edited = vt.edited_path(self.store, self.row_id) if self.store and self.row_id is not None else None
        caps = vt.availability()
        if not have:
            self.status.setText("Download the video first to edit it here.")
        elif edited:
            self.status.setText(f"Posting the edited copy ({vt.describe(vt.get_edit(self.store, self.row_id))}): "
                                f"{edited.name}. The original is untouched.")
        else:
            self.status.setText("Posting the original download. Edits make a new copy; the original is never "
                                "changed.")
        self.strip_btn.setEnabled(have and caps["sound"][0] and not busy)
        self.replace_btn.setEnabled(have and caps["sound"][0] and not busy)
        self.captions_btn.setEnabled(have and caps["captions"][0] and not busy)
        self.caption_style.setEnabled(self.captions_btn.isEnabled())
        self.original_btn.setVisible(bool(edited))
        self.show_btn.setEnabled(have)
        notes = [why for ok, why in (caps["sound"], caps["captions"]) if not ok]
        self.note.setText(" ".join(notes))
        self.note.setVisible(bool(notes))

    # ------------------------------------------------------------------ actions
    def _run(self, label: str, step: str, fn, *args, **kwargs) -> None:
        if self.worker and self.worker.isRunning():
            return
        src, row_id = self.source(), self.row_id
        if src is None or self.original is None:
            return
        steps = list((vt.get_edit(self.store, row_id) or {}).get("steps", [])) if vt.edited_path(
            self.store, row_id) else []
        dst = vt.output_path(self.original)
        self.status.setText(f"{label}… (on this computer)")
        self.worker = CallWorker(fn, src, *args, dst=dst, **kwargs, parent=self)
        self.worker.succeeded.connect(lambda path: self._done(row_id, path, steps + [step]))
        self.worker.failed.connect(self._failed)
        self.worker.start()
        self.refresh()
        self.status.setText(f"{label}… (on this computer)")

    def _done(self, row_id: int, path, steps: list[str]) -> None:
        vt.set_edit(self.store, row_id, path, steps)
        if row_id == self.row_id:
            self.refresh()
        self.message.emit(f"Edited copy ready ({vt.describe({'steps': steps})}). Reelay will post it.")
        self.editChanged.emit(row_id)

    def _failed(self, msg: str) -> None:
        self.refresh()
        self.status.setText(msg)
        set_prop(self.status, "danger", True)
        self.message.emit(msg)

    def _strip(self) -> None:
        set_prop(self.status, "danger", False)
        self._run("Removing the sound", "strip", vt.strip_audio)

    def _replace(self) -> None:
        dialog = SoundDialog(self, str(self.original.parent) if self.original else "")
        if dialog.exec() != QDialog.Accepted or not dialog.track:
            return
        set_prop(self.status, "danger", False)
        self._run("Replacing the sound", "replace", vt.replace_audio, dialog.track, **dialog.options())

    def _captions(self) -> None:
        subs = self._pick_captions()
        if not subs:
            return
        set_prop(self.status, "danger", False)
        self._run("Adding captions", "captions", vt.burn_captions, subs, style=self.caption_style.currentData())

    def _pick_captions(self) -> Path | None:
        """A caption file the person has; offers automatic captions when faster-whisper is installed."""
        ok, why = vt.whisper_available()
        start = str(self.original.parent) if self.original else ""
        if ok:
            from PySide6.QtWidgets import QMessageBox
            box = QMessageBox(QMessageBox.Question, "Add Captions",
                              "Make captions automatically from the speech (on this computer), or use a caption "
                              "file you already have?", parent=self)
            auto = box.addButton("Make Automatically", QMessageBox.AcceptRole)
            box.addButton("Choose File…", QMessageBox.ActionRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() is auto:
                self._auto_captions()
                return None
            if box.clickedButton() is box.button(QMessageBox.Cancel):
                return None
        exts = " ".join(f"*{e}" for e in vt.SUBTITLE_TYPES)
        path, _ = QFileDialog.getOpenFileName(self, "Choose Caption File", start, f"Captions ({exts})")
        return Path(path) if path else None

    def _auto_captions(self) -> None:
        """Transcribe to an .srt next to the video, show it, then burn it in when the person is happy."""
        if self.worker and self.worker.isRunning() or not self.original:
            return
        self.status.setText("Listening to the video and writing captions… (the first time downloads a model)")
        self.worker = CallWorker(vt.auto_captions, self.original, parent=self)

        def made(srt):
            from PySide6.QtWidgets import QMessageBox
            self.refresh()
            if QMessageBox.question(self, "Captions Ready",
                                    f"Captions saved to {Path(srt).name}. You can open it to fix any words first. "
                                    "Burn them into the video now?") == QMessageBox.Yes:
                self._run("Adding captions", "captions", vt.burn_captions, Path(srt),
                          style=self.caption_style.currentData())
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(srt)))
        self.worker.succeeded.connect(made)
        self.worker.failed.connect(self._failed)
        self.worker.start()

    def _use_original(self) -> None:
        if self.row_id is None:
            return
        vt.clear_edit(self.store, self.row_id)
        self.refresh()
        self.message.emit("Reelay will post the original download.")
        self.editChanged.emit(self.row_id)

    def _reveal(self) -> None:
        path = self.source()
        if path and path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def shutdown(self) -> None:
        if self.worker:
            self.worker.wait(5000)
