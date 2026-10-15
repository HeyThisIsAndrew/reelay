"""Prepare to Publish: the guided hand-off to a platform's own uploader."""

from __future__ import annotations

import sys
from functools import partial

from PySide6.QtCore import QDateTime, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QVBoxLayout)

from .. import handoff
from ..models import Platform
from .logos import platform_pixmap
from .theme import set_prop
from .tips import tip
from .widgets import label

MARK = {handoff.OK: "✓", handoff.WARN: "⚠", handoff.BLOCK: "✕"}
FILES = "Finder" if sys.platform == "darwin" else "File Explorer" if sys.platform == "win32" else "your file manager"


_label = partial(label, rich=True)


class HandoffDialog(QDialog):
    """Step 1 checks, steps 2–4 in one click, step 5 stays open for the live link."""

    def __init__(self, plan: handoff.HandoffPlan, parent=None, open_url=None, reveal=None, clipboard=None):
        super().__init__(parent)
        self.plan = plan
        self.label = Platform.LABELS.get(plan.platform, plan.platform)
        self._open_url = open_url or (lambda url: QDesktopServices.openUrl(QUrl(url)))
        self._reveal = reveal or handoff.reveal_in_file_manager
        self._clipboard = clipboard or QGuiApplication.clipboard()
        self.started = False
        self.setWindowTitle(f"Prepare to Publish on {self.label}")
        self.resize(560, 640)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(10)

        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(platform_pixmap(plan.platform, 28))
        head.addWidget(logo)
        head.addWidget(_label(f"<b style='font-size:15px'>Publish on {self.label}</b><br>"
                              f"You post it in {self.label}'s own uploader; Reelay sets everything up and records "
                              "the result.", "muted"), 1)
        lay.addLayout(head)

        # ---- 1. ready?
        lay.addWidget(_label("<b>1. Ready?</b>"))
        rows = []
        for c in plan.checks:
            rows.append(f"{MARK[c.status]} <b>{c.label}</b>: {c.detail}")
        self.checks = _label("<br>".join(rows))
        set_prop(self.checks, "danger", plan.blocked)
        lay.addWidget(self.checks)
        caption = QPlainTextEdit(plan.caption)
        caption.setReadOnly(True)
        caption.setFixedHeight(110)
        tip(caption, "The caption Reelay copies (edit it in the ticket)")
        lay.addWidget(caption)

        # ---- 2–4. one click
        lay.addWidget(_label("<b>2. Hand it over</b>"))
        self.start_btn = tip(QPushButton(f"Copy Caption, Open {self.label} && Show Video"),
                             "Copies the caption, opens the upload page in your browser and shows the video file "
                             f"selected in {FILES}, ready to drag in")
        set_prop(self.start_btn, "primary")
        self.start_btn.setEnabled(not plan.blocked)
        self.start_btn.clicked.connect(self.start)
        lay.addWidget(self.start_btn)
        self.progress = _label(plan.hint if not plan.blocked else
                               "Fix the items marked ✕ in the ticket first.", "faint")
        lay.addWidget(self.progress)
        again = QHBoxLayout()
        self.copy_btn = tip(QPushButton("Copy Caption"), "Copy the caption again")
        self.open_btn = tip(QPushButton(f"Open {self.label}"), f"Open {self.label}'s upload page again")
        self.show_btn = tip(QPushButton("Show Video"), f"Show the video file in {FILES} again")
        for b, fn in ((self.copy_btn, self.copy_caption), (self.open_btn, self.open_upload),
                      (self.show_btn, self.show_video)):
            set_prop(b, "flat")
            b.clicked.connect(fn)
            again.addWidget(b)
        again.addStretch()
        self.copy_btn.setEnabled(bool(plan.caption))
        self.show_btn.setEnabled(plan.video is not None)
        lay.addLayout(again)

        # ---- 5. confirm
        lay.addWidget(_label("<b>3. Confirm it's posted</b>"))
        self.url = tip(QLineEdit(), f"The link to the live post. In {self.label}, use Share → Copy link.")
        self.url.setPlaceholderText(f"Paste the link to the post on {self.label} (optional)")
        self.url.textChanged.connect(self._validate)
        lay.addWidget(self.url)
        wrow = QHBoxLayout()
        self.when_kind = tip(QComboBox(), "When it went live, or when it's scheduled to")
        self.when_kind.addItem("Posted just now", "now")
        self.when_kind.addItem("Posted earlier, at…", "earlier")
        self.when_kind.addItem(f"Scheduled in {self.label} for…", "scheduled")
        self.when_kind.currentIndexChanged.connect(self._sync_when)
        self.when = QDateTimeEdit()
        self.when.setCalendarPopup(True)
        self.when.setDisplayFormat("ddd MMM d, yyyy   h:mm AP")
        self.when.setDateTime(QDateTime.currentDateTime())
        self.when.dateTimeChanged.connect(self._validate)
        wrow.addWidget(self.when_kind, 1)
        wrow.addWidget(self.when)
        lay.addLayout(wrow)
        self.error = _label(prop="danger")
        lay.addWidget(self.error)
        lay.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.Close | QDialogButtonBox.Ok)
        self.ok = buttons.button(QDialogButtonBox.Ok)
        self.ok.setText("Record as Published")
        buttons.button(QDialogButtonBox.Close).setText("Not Yet")
        tip(buttons.button(QDialogButtonBox.Close), "Close without recording anything")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._sync_when()

    # ------------------------------------------------------------------ steps
    def start(self) -> None:
        if self.plan.blocked:
            return
        done = [self.copy_caption(), self.open_upload(), self.show_video()]
        names = ["Caption copied", f"{self.label} opened in your browser", f"Video shown in {FILES}"]
        self.progress.setText("<br>".join(f"{'✓' if ok else '⚠'} {name}" for ok, name in zip(done, names))
                              + f"<br><br>{self.plan.hint}<br>When it's up, paste the link below.")
        self.started = True
        self.url.setFocus()

    def copy_caption(self) -> bool:
        if not self.plan.caption:
            return False
        self._clipboard.setText(self.plan.caption)
        return True

    def open_upload(self) -> bool:
        if not self.plan.url:
            return False
        self._open_url(self.plan.url)
        return True

    def show_video(self) -> bool:
        return bool(self.plan.video) and bool(self._reveal(self.plan.video))

    # ------------------------------------------------------------------ confirm
    def _sync_when(self) -> None:
        kind = self.when_kind.currentData()
        self.when.setVisible(kind != "now")
        if kind == "scheduled" and self.when.dateTime() <= QDateTime.currentDateTime():
            self.when.setDateTime(QDateTime.currentDateTime().addSecs(3600))
        elif kind == "earlier" and self.when.dateTime() > QDateTime.currentDateTime():
            self.when.setDateTime(QDateTime.currentDateTime())
        self.ok.setText("Record as Scheduled" if kind == "scheduled" else "Record as Published")
        self._validate()

    def _validate(self) -> None:
        problem = handoff.check_post_url(self.plan.platform, self.url.text())
        kind = self.when_kind.currentData()
        now = QDateTime.currentDateTime()
        if not problem and kind == "earlier" and self.when.dateTime() > now.addSecs(300):
            problem = "That time is in the future: choose Scheduled instead."
        if not problem and kind == "scheduled" and self.when.dateTime() <= now:
            problem = "Pick a time in the future for a scheduled post."
        self.error.setText(problem or "")
        self.error.setVisible(bool(problem))
        self.ok.setEnabled(not problem)

    def values(self) -> dict:
        kind = self.when_kind.currentData()
        when = None if kind == "now" else self.when.dateTime().toPython().astimezone()
        return {"url": self.url.text().strip(), "when": when, "scheduled": kind == "scheduled"}

