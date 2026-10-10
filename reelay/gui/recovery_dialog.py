"""Interrupted sends: Reelay never guesses whether something was posted, it asks."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from ..models import Dispatch, Platform
from ..pipeline import MISSED_MESSAGE, PipelineError
from ..text import local_text, short_title
from .logos import platform_pixmap
from .theme import set_prop
from .tips import tip


class RecoveryDialog(QDialog):
    """One row per interrupted send: Try Again / It Was Posted… / Cancel It."""

    def __init__(self, pipeline, dispatches: list[Dispatch], parent=None, ask_link=None):
        super().__init__(parent)
        self.pipeline = pipeline
        self.resolved: list[tuple[int, str]] = []         # (dispatch id, action)
        self._ask_link = ask_link or self._ask_link_dialog
        self.setWindowTitle("Check Interrupted Posts")
        self.resize(640, 460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(10)
        intro = QLabel("These sends were cut off (Reelay closed, crashed or lost power) or missed their time while "
                       "Reelay wasn't running. Reelay doesn't retry them on its own, so nothing is posted twice. "
                       "Check each platform, then choose.")
        intro.setWordWrap(True)
        intro.setProperty("muted", True)
        lay.addWidget(intro)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        self.rows = QVBoxLayout(body)
        self.rows.setSpacing(8)
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)
        self.row_widgets: dict[int, QFrame] = {}
        for d in dispatches:
            self._add_row(d)
        self.rows.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("Decide Later")
        tip(buttons.button(QDialogButtonBox.Close), "They stay on hold (marked on their cards) until you choose")
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _add_row(self, d: Dispatch) -> None:
        label = Platform.LABELS.get(d.platform, d.platform)
        row = self.pipeline.store.get_video(d.video_row)
        post = self.pipeline.store.get_post(d.video_row)
        title = (post.title if post and post.title else short_title(row.item.hook, 70)) if row else f"Video {d.video_row}"
        missed = d.error == MISSED_MESSAGE
        frame = QFrame(objectName="Card")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(12, 10, 12, 10)
        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(platform_pixmap(d.platform, 20))
        head.addWidget(logo)
        name = QLabel(f"<b>{title}</b> → {label}")
        name.setTextFormat(Qt.RichText)
        name.setWordWrap(True)
        head.addWidget(name, 1)
        fl.addLayout(head)
        when = local_text(d.publish_at or d.due_at)
        why = QLabel(f"{'Was due' if missed else 'Was being sent; due'} {when}. {d.error}")
        why.setWordWrap(True)
        why.setProperty("faint", True)
        fl.addWidget(why)
        buttons = QHBoxLayout()
        retry = tip(QPushButton("Post Now" if missed else "Try Again"),
                    f"It isn't on {label}: send it now (it goes live right away if its time has passed)")
        posted = tip(QPushButton("It Was Posted…"), f"It's already on {label}: record it (paste its link if you "
                                                    "have it). Nothing is sent.")
        cancel = tip(QPushButton("Cancel It"), f"Don't send it to {label}")
        set_prop(cancel, "flat")
        retry.clicked.connect(lambda _=False, i=d.id: self._resolve(i, "retry"))
        posted.clicked.connect(lambda _=False, i=d.id, p=d.platform: self._posted(i, p))
        cancel.clicked.connect(lambda _=False, i=d.id: self._resolve(i, "cancel"))
        for b in (retry, posted, cancel):
            buttons.addWidget(b)
        buttons.addStretch()
        fl.addLayout(buttons)
        self.rows.addWidget(frame)
        self.row_widgets[d.id] = frame

    def _ask_link_dialog(self, platform: str) -> str | None:
        label = Platform.LABELS.get(platform, platform)
        text, ok = QInputDialog.getText(self, "It Was Posted", f"Link to the post on {label} (optional):",
                                        QLineEdit.Normal, "")
        return text if ok else None

    def _posted(self, dispatch_id: int, platform: str) -> None:
        link = self._ask_link(platform)
        if link is None:
            return
        self._resolve(dispatch_id, "posted", link)

    def _resolve(self, dispatch_id: int, action: str, url: str = "") -> None:
        try:
            self.pipeline.resolve_interrupted(dispatch_id, action, url)
        except PipelineError as exc:
            QMessageBox.warning(self, "Check Interrupted Posts", str(exc))
            return
        self.resolved.append((dispatch_id, action))
        frame = self.row_widgets.pop(dispatch_id, None)
        if frame is not None:
            frame.hide()
            frame.setParent(None)
        if not self.row_widgets:
            self.accept()
