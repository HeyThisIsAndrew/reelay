"""The content ticket's own pieces in the Board's side panel:

- ReadinessHeader: READY / NOT READY, and a ✓ / ⚠ line per target platform.
- TicketDetails: transcript, cover, type, topic, keywords, creator, dates and
  provenance. Each field saves when you leave it (a logical commit), never
  while typing.
- PublishingHistory: what went where, when and how (automatic and by hand),
  with "Mark as Posted…".
- RevisionDialog: the ticket's change log, newest first.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

from PySide6.QtCore import QDateTime, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QDateTimeEdit, QDialog, QDialogButtonBox,
                               QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
                               QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..models import MANUAL_LABELS, MANUAL_PLATFORMS, ContentType, Ticket
from ..publishing import parse_utc
from ..readiness import NOT_CHECKED, READY, Readiness
from ..text import local_text
from .theme import set_prop
from .tips import tip

AUTHORIZATION_PRESETS = ("Own content", "Managed account", "Licensed", "Used with permission")
FIELD_LABELS = {
    "title": "YouTube title", "description": "YouTube description", "tags": "YouTube tags",
    "target_platforms": "Post automatically to", "tiktok_text": "TikTok caption",
    "instagram_text": "Instagram caption", "bluesky_text": "Bluesky post", "threads_text": "Threads post",
    "facebook_text": "Facebook post", "hashtags": "Hashtags", "transcript": "Transcript", "thumbnail_path": "Cover",
    "content_type": "Type", "topic": "Topic", "keywords": "Keywords", "creator": "Creator",
    "original_publish_date": "First published", "source_platform": "Came from", "source_url": "Original link",
    "imported_at": "Imported", "authorization": "Permission", "derived_from": "Cut from",
    "archived": "Archived",
}
ORIGIN_LABELS = {"local": "You (Reelay app)", "api": "AI agent (API)", "remote": "Companion app",
                 "import": "Sheet import", "automation:agent-input": "Automation, from an AI agent's text"}


def _section(text: str, hint: str) -> QLabel:
    from .theme import heading
    lab = QLabel(heading(text))
    lab.setProperty("section", True)
    return tip(lab, hint)


def _narrow(combo: QComboBox) -> QComboBox:
    """Don't let a long item (a video title) make the side panel wider than the window."""
    combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(10)
    return combo


def _faint(text: str = "", prop: str = "faint") -> QLabel:
    lab = QLabel(text)
    lab.setProperty(prop, True)
    lab.setWordWrap(True)
    return lab


# ====================================================================== readiness
class ReadinessRing(QWidget):
    """How many of the platforms this video is going to are ready, as a ring ("4/5")."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(40, 40)
        self.ready, self.total = 0, 0

    def set_counts(self, ready: int, total: int) -> None:
        self.ready, self.total = ready, total
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QPainter, QPen
        from . import theme
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(3, 3, self.width() - 6, self.height() - 6)
        p.setPen(QPen(QColor(theme.P.bg_pressed), 4))
        p.drawEllipse(r)
        if self.total:
            done = self.ready == self.total
            pen = QPen(QColor(theme.P.success if done else theme.P.accent_color), 4)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawArc(r, 90 * 16, -int(360 * 16 * self.ready / self.total))
        p.setPen(QColor(theme.P.text_primary))
        font = p.font()
        font.setPointSize(max(font.pointSize() - 3, 8))
        font.setBold(True)
        p.setFont(font)
        p.drawText(self.rect(), Qt.AlignCenter, f"{self.ready}/{self.total}" if self.total else "–")
        p.end()


class ReadinessHeader(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(12)
        self.ring = ReadinessRing()
        tip(self.ring, "How many of the platforms this video is going to are ready")
        top.addWidget(self.ring)
        self.badge = QLabel()
        self.badge.setVisible(False)       # the ring says it now; the text stays for screen readers and tests
        tip(self.badge, "READY when at least one platform this video is going to is ready. Platforms with "
                        "automatic posting off count as ready: you post those by hand.")
        top.addWidget(self.badge)
        self.summary = _faint()
        top.addWidget(self.summary, 1)
        lay.addLayout(top)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(0)
        lay.addLayout(self.rows)


    def set_readiness(self, readiness: Readiness | None) -> None:
        while self.rows.count():
            w = self.rows.takeAt(0).widget()
            if w:
                w.hide()
                w.deleteLater()
        if readiness is None:
            self.hide()
            return
        self.show()
        ready = readiness.ready
        self.ring.set_counts(sum(1 for p in readiness.platforms.values() if p.ready), len(readiness.platforms))
        self.badge.setText("READY" if ready else "NOT READY")
        set_prop(self.badge, "badge", "ready" if ready else "not_ready")
        blocking = [readiness.platforms[n].label for n in readiness.blocking]
        ok = [p.label + (" (by hand)" if p.state == NOT_CHECKED else "")
              for p in readiness.platforms.values() if p.ready]
        if not readiness.platforms:
            self.summary.setText("No platforms chosen.")
        elif not blocking:
            self.summary.setText("Ready for " + ", ".join(ok) + ".")
        elif ready:
            self.summary.setText(f"Ready for {', '.join(ok)}. Not ready for {', '.join(blocking)}.")
        else:
            self.summary.setText(f"Not ready for {', '.join(blocking)}.")
        for name, pr in readiness.platforms.items():
            self.rows.addWidget(_readiness_row(pr))


def _readiness_row(pr) -> QFrame:
    """One compact line per platform: logo, name, a short note, and a mark (✓ ready, – by hand, ! blocking).
    The full checks are in the row's tooltip."""
    from .logos import platform_pixmap
    from .widgets import LineLabel
    row = QFrame(objectName="ReadyRow")
    rl = QHBoxLayout(row)
    rl.setContentsMargins(8, 5, 10, 5)
    rl.setSpacing(8)
    logo = QLabel()
    logo.setPixmap(platform_pixmap(pr.platform, 14))
    logo.setFixedSize(14, 14)
    rl.addWidget(logo)
    name = QLabel(pr.label, objectName="ReadyName")
    name.setMinimumWidth(78)
    rl.addWidget(name)
    if pr.state == READY:
        note, mark, state = "Ready to post", "✓", "ready"
    elif pr.state == NOT_CHECKED:
        note = "Posted by hand" if pr.platform in ("tiktok", "instagram") else "Automatic posting off: you post it"
        mark, state = "–", "hand"
    else:
        problems = [c.message for c in pr.checks if not c.ok]
        note, mark, state = (problems[0] if problems else "Needs work"), "!", "blocking"
    text = LineLabel(note, "ReadyNote")
    rl.addWidget(text, 1)
    sign = QLabel(mark, objectName="ReadyMark")
    set_prop(sign, "state", state)
    rl.addWidget(sign)
    row.setToolTip("\n".join(f"{'✓' if c.ok else '⚠'} {c.label}: {c.message}" for c in pr.checks) or pr.note
                   or note)
    row.setAccessibleName(f"{pr.label}: {note}")
    return row


# ====================================================================== ticket fields
class CommitText(QPlainTextEdit):
    """Multi-line field that commits when it loses focus (not on every keystroke)."""
    committed = Signal()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        if self.document().isModified():
            self.document().setModified(False)
            self.committed.emit()


class TicketDetails(QWidget):
    """Ticket fields. Each change is committed on blur / choice as {field: value}."""
    fieldCommitted = Signal(str, object)
    transcriptRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = False
        self._published_shown = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.toggle = QToolButton(text="▸  DETAILS AND PROVENANCE")
        self.toggle.setCheckable(True)
        self.toggle.setProperty("section", True)
        self.toggle.setAutoRaise(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextOnly)
        tip(self.toggle, "Transcript, cover, topic, keywords, where the video came from and your permission to use it")
        self.toggle.toggled.connect(self._toggle)
        lay.addWidget(self.toggle)
        self.body = QWidget()
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        lay.addWidget(self.body)
        self.body.hide()

        def line(key: str, placeholder: str, hint: str) -> QLineEdit:
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            edit.setAttribute(Qt.WA_MacShowFocusRect, False)
            tip(edit, hint)
            edit.editingFinished.connect(lambda k=key, e=edit: self._commit(k, e.text().strip() or None))
            return edit

        trow = QHBoxLayout()
        trow.addWidget(_section("TRANSCRIPT", "Everything said in the video. Saved when you click away."))
        trow.addStretch()
        get = tip(QPushButton("Get from Original Post"), "Use the original post's captions (where the platform "
                                                         "has them) as the transcript. Uses the network.")
        set_prop(get, "flat")
        get.clicked.connect(self.transcriptRequested.emit)
        trow.addWidget(get)
        bl.addLayout(trow)
        self.transcript = CommitText()
        self.transcript.setPlaceholderText("Paste or type the transcript")
        self.transcript.setFixedHeight(90)
        tip(self.transcript, "Everything said in the video. Saved when you click away.")
        self.transcript.committed.connect(lambda: self._commit("transcript", self.transcript.toPlainText() or None))
        bl.addWidget(self.transcript)

        bl.addWidget(_section("COVER IMAGE", "The image to use as the video's cover"))
        crow = QHBoxLayout()
        self.thumbnail = line("thumbnail_path", "No cover chosen", "Path of the cover image file")
        crow.addWidget(self.thumbnail, 1)
        choose = tip(QPushButton("Choose…"), "Pick a cover image file")
        choose.clicked.connect(self._choose_cover)
        crow.addWidget(choose)
        bl.addLayout(crow)

        grid = QVBoxLayout()
        grid.setSpacing(6)
        tv = QVBoxLayout()
        tv.addWidget(_section("TYPE", "What kind of video this is"))
        self.content_type = tip(QComboBox(), "Short, long-form, a clip from a longer video, or other")
        _narrow(self.content_type)
        self.content_type.addItem("Not set", None)
        for key in ContentType.ALL:
            self.content_type.addItem(ContentType.LABELS[key], key)
        self.content_type.activated.connect(lambda: self._commit("content_type", self.content_type.currentData()))
        tv.addWidget(self.content_type)
        grid.addLayout(tv)
        dv = QVBoxLayout()
        dv.addWidget(_section("FIRST PUBLISHED", "When this content was first published anywhere"))
        drow = QHBoxLayout()
        self.published = tip(QDateEdit(), "When this content was first published anywhere")
        self.published.setCalendarPopup(True)
        self.published.setDisplayFormat("MMM d, yyyy")
        self.published.editingFinished.connect(self._commit_published)
        drow.addWidget(self.published, 1)
        self.clear_published = tip(QPushButton("Clear"), "Remove the first-published date")
        set_prop(self.clear_published, "flat")
        self.clear_published.clicked.connect(lambda: self._commit("original_publish_date", None))
        drow.addWidget(self.clear_published)
        dv.addLayout(drow)
        grid.addLayout(dv)
        bl.addLayout(grid)

        bl.addWidget(_section("TOPIC", "What the video is about, in a few words"))
        self.topic = line("topic", "e.g. letting go of old identities", "What the video is about, in a few words")
        bl.addWidget(self.topic)
        bl.addWidget(_section("KEYWORDS", "Words people might search for"))
        self.keywords = line("keywords", "growth, healing, identity", "Words people might search for, "
                                                                       "separated by commas")
        bl.addWidget(self.keywords)
        bl.addWidget(_section("CREATOR", "The brand or person this content belongs to"))
        self.creator = line("creator", "Brand or person name", "The brand or person this content belongs to")
        bl.addWidget(self.creator)
        bl.addWidget(_section("CUT FROM", "If this video was cut from a longer one, pick it here"))
        self.derived = tip(QComboBox(), "The video this one was cut from. If that video is deleted, this "
                                        "link is cleared; nothing else changes.")
        _narrow(self.derived)
        self.derived.activated.connect(lambda: self._commit("derived_from", self.derived.currentData()))
        bl.addWidget(self.derived)

        bl.addSpacing(4)
        bl.addWidget(_section("WHERE IT CAME FROM", "Recorded when the video was imported"))
        self.origin = _faint(prop="muted")
        self.origin.setTextFormat(Qt.RichText)
        self.origin.setOpenExternalLinks(True)
        self.origin.setTextInteractionFlags(Qt.TextBrowserInteraction)
        bl.addWidget(self.origin)
        bl.addWidget(_section("PERMISSION", "Your right to use and repost this video"))
        self.authorization = tip(QComboBox(), "Your right to use this video: your own content, an account you "
                                              "manage, licensed, or used with permission. Type your own if none fit.")
        self.authorization.setEditable(True)
        _narrow(self.authorization)
        self.authorization.addItems(AUTHORIZATION_PRESETS)
        self.authorization.setInsertPolicy(QComboBox.NoInsert)
        self.authorization.lineEdit().setPlaceholderText("Not recorded")
        self.authorization.activated.connect(self._commit_authorization)
        self.authorization.lineEdit().editingFinished.connect(self._commit_authorization)
        bl.addWidget(self.authorization)

    def _toggle(self, on: bool) -> None:
        self.body.setVisible(on)
        self.toggle.setText(("▾" if on else "▸") + "  DETAILS AND PROVENANCE")

    def _commit(self, key: str, value) -> None:
        if not self._loading:
            self.fieldCommitted.emit(key, value)

    def _commit_published(self) -> None:
        if self.published.date() == self._published_shown:
            return   # left the field without changing it
        d = self.published.date().toPython()
        self._published_shown = self.published.date()
        self._commit("original_publish_date", f"{d.isoformat()}T00:00:00Z")

    def _commit_authorization(self) -> None:
        self._commit("authorization", self.authorization.currentText().strip() or None)

    def _choose_cover(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a Cover Image", self.thumbnail.text(),
                                              "Images (*.png *.jpg *.jpeg *.webp *.heic)")
        if path:
            self.thumbnail.setText(path)
            self._commit("thumbnail_path", path)

    def set_ticket(self, ticket: Ticket, row_id: int, others: list[tuple[int, str]], products=()) -> None:
        """Fill the fields. `others`: (video id, title) for the "Cut from" choice."""
        self._loading = True
        try:
            if not self.transcript.hasFocus():
                self.transcript.setPlainText(ticket.transcript or "")
                self.transcript.document().setModified(False)
            for edit, value in ((self.thumbnail, ticket.thumbnail_path), (self.topic, ticket.topic),
                                (self.keywords, ticket.keywords), (self.creator, ticket.creator)):
                if not edit.hasFocus():
                    edit.setText(value or "")
                    edit.setCursorPosition(0)
            self.content_type.setCurrentIndex(max(self.content_type.findData(ticket.content_type), 0))
            when = parse_utc(ticket.original_publish_date)
            self.published.setDate(when.date() if when else datetime.now().date())
            self._published_shown = self.published.date()
            self.clear_published.setEnabled(when is not None)
            self.published.setToolTip("When this content was first published anywhere" +
                                      ("" if when else " (not set yet)"))
            self.derived.clear()
            self.derived.addItem("Not cut from another video", None)
            for vid, title in others:
                if vid != row_id:
                    self.derived.addItem(title, vid)
            self.derived.setCurrentIndex(max(self.derived.findData(ticket.derived_from), 0))
            source = (ticket.source_platform or "").capitalize().replace("Tiktok", "TikTok").replace("Youtube",
                                                                                                    "YouTube")
            parts = []
            if source or ticket.source_url:
                link = (f"<a href='{html.escape(ticket.source_url)}'>original post</a>" if ticket.source_url else "")
                parts.append(" · ".join(x for x in (source, link) if x))
            if ticket.imported_at:
                parts.append(f"Imported {local_text(ticket.imported_at)}")
            for p in products:
                link = f" · <a href='{html.escape(p.url)}'>shop page</a>" if p.url else ""
                parts.append(f"Tagged product: {html.escape(p.label)}{link}")
            self.origin.setText("<br>".join(parts) or "Not recorded (made before Reelay kept provenance).")
            if not self.authorization.lineEdit().hasFocus():
                self.authorization.setEditText(ticket.authorization or "")
        finally:
            self._loading = False


# ====================================================================== publishing history
STATUS_WORDS = {"pending": "waiting", "dispatching": "sending now", "scheduled": "scheduled", "published": "live",
                "failed": "failed", "posted": "posted by hand"}


class PublishingHistory(QWidget):
    markRequested = Signal()
    unmarkRequested = Signal(int)
    revisionsRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(_section("WHERE IT'S BEEN POSTED", "Every post of this video, automatic or by hand, per platform"))
        self.list = QVBoxLayout()
        self.list.setSpacing(4)
        lay.addLayout(self.list)
        row = QHBoxLayout()
        self.mark_btn = tip(QPushButton("Mark as Posted…"), "You posted this by hand (copy and paste)? Record "
                                                            "where and when, so the ticket knows.")
        self.mark_btn.clicked.connect(self.markRequested.emit)
        self.history_btn = tip(QPushButton("Change History…"), "Every saved change to this ticket: what, when, "
                                                               "and who made it")
        set_prop(self.history_btn, "flat")
        self.history_btn.clicked.connect(self.revisionsRequested.emit)
        row.addWidget(self.mark_btn)
        row.addStretch()
        row.addWidget(self.history_btn)
        lay.addLayout(row)

    def set_history(self, history: dict) -> None:
        while self.list.count():
            w = self.list.takeAt(0).widget()
            if w:
                w.deleteLater()
        if not history:
            self.list.addWidget(_faint("Not posted anywhere yet."))
            return
        for platform, entries in history.items():
            for e in entries:
                when = local_text(e.at) if e.at else "as soon as possible"
                how = "by Reelay" if e.method == "automatic" else "by hand"
                text = f"<b>{MANUAL_LABELS.get(platform, platform)}</b>: {STATUS_WORDS.get(e.status, e.status)} " \
                       f"{how} · {html.escape(when)}"
                if e.url:
                    text += f" · <a href='{html.escape(e.url)}'>open</a>"
                if e.error:
                    text += f"<br><span style='opacity:.8'>⚠ {html.escape(e.error)}</span>"
                if e.note:
                    text += f"<br><span style='opacity:.7'>{html.escape(e.note)}</span>"
                line = QWidget()
                ll = QHBoxLayout(line)
                ll.setContentsMargins(0, 0, 0, 0)
                lab = QLabel(text)
                lab.setTextFormat(Qt.RichText)
                lab.setWordWrap(True)
                lab.setOpenExternalLinks(True)
                lab.setProperty("danger" if e.status == "failed" else "muted", True)
                ll.addWidget(lab, 1)
                if e.method == "manual":
                    undo = tip(QPushButton("Remove"), "Remove this record (it doesn't touch the post itself)")
                    set_prop(undo, "flat")
                    undo.clicked.connect(lambda _=False, i=e.id: self.unmarkRequested.emit(i))
                    ll.addWidget(undo, 0, Qt.AlignTop)
                self.list.addWidget(line)


class MarkPostedDialog(QDialog):
    def __init__(self, parent=None, default_platform: str = "tiktok"):
        super().__init__(parent)
        self.setWindowTitle("Mark as Posted")
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 14)
        lay.setSpacing(8)
        lay.addWidget(_faint("Record a post you made by hand, so this ticket's history is complete. "
                             "Nothing is posted.", "muted"))
        self.platform = tip(QComboBox(), "Where you posted it")
        for p in MANUAL_PLATFORMS:
            self.platform.addItem(MANUAL_LABELS[p], p)
        self.platform.setCurrentIndex(max(self.platform.findData(default_platform), 0))
        lay.addWidget(_section("PLATFORM", "Where you posted it"))
        lay.addWidget(self.platform)
        lay.addWidget(_section("WHEN IT WENT LIVE", "Your local time"))
        self.when = tip(QDateTimeEdit(QDateTime.currentDateTime()), "When it went live, in your local time")
        self.when.setCalendarPopup(True)
        self.when.setDisplayFormat("ddd MMM d, yyyy   h:mm AP")
        self.when.setMaximumDateTime(QDateTime.currentDateTime().addSecs(300))
        lay.addWidget(self.when)
        lay.addWidget(_section("LINK (OPTIONAL)", "The address of the live post"))
        self.url = tip(QLineEdit(), "The address of the live post, if you have it")
        self.url.setPlaceholderText("https://")
        lay.addWidget(self.url)
        self.note = tip(QLineEdit(), "Anything worth remembering about this post")
        self.note.setPlaceholderText("Note (optional)")
        lay.addWidget(self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Save)
        buttons.button(QDialogButtonBox.Save).setText("Mark as Posted")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def values(self) -> dict:
        when = self.when.dateTime().toPython().astimezone(timezone.utc)
        return {"platform": self.platform.currentData(), "posted_at": when, "url": self.url.text().strip(),
                "note": self.note.text().strip()}


class RevisionDialog(QDialog):
    """The change log. With `on_restore(revision_id) -> error or None`, a change
    can be undone (the undo is logged as a new change)."""

    def __init__(self, revisions, parent=None, titles: dict[int, str] | None = None, on_restore=None, reload=None):
        super().__init__(parent)
        self._on_restore, self._reload = on_restore, reload
        self.setWindowTitle("Change History")
        self.resize(760, 440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.addWidget(_faint("Every saved change to this ticket, newest first. Typing isn't recorded, only saves.",
                             "muted"))
        tree = QTreeWidget()
        self._titles = titles or {}
        tree.setHeaderLabels(["WHEN", "WHO", "WHAT", "FROM", "TO"])
        tree.setRootIsDecorated(False)
        tree.setWordWrap(True)
        tree.header().setSectionResizeMode(3, QHeaderView.Stretch)
        tree.header().setSectionResizeMode(4, QHeaderView.Stretch)
        self.tree = tree
        lay.addWidget(tree, 1)
        self.status = _faint(prop="danger")
        lay.addWidget(self.status)
        row = QHBoxLayout()
        self.restore_btn = tip(QPushButton("Restore the Earlier Value"),
                               "Put the highlighted field back to what it was before that change. The undo is "
                               "saved as a new change, so nothing is lost.")
        self.restore_btn.setEnabled(False)
        self.restore_btn.setVisible(on_restore is not None)
        self.restore_btn.clicked.connect(self._restore)
        tree.itemSelectionChanged.connect(lambda: self.restore_btn.setEnabled(bool(tree.selectedItems()) and bool(
            tree.selectedItems()[0].data(0, Qt.UserRole))))
        row.addWidget(self.restore_btn)
        row.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        lay.addLayout(row)
        self.fill(revisions)

    def _shown(self, field: str, value: str | None) -> str:
        if value is None or value == "":
            return "(empty)"
        if field == "derived_from" and value.isdigit():
            return self._titles.get(int(value), f"video {value}")
        if field in ("original_publish_date", "imported_at", "archived"):
            return local_text(value) or value
        return value if len(value) <= 300 else value[:300] + "…"

    def fill(self, revisions) -> None:
        tree = self.tree
        tree.clear()
        for r in revisions:
            item = QTreeWidgetItem([local_text(r.at) or r.at, ORIGIN_LABELS.get(r.origin, r.origin),
                                    FIELD_LABELS.get(r.field, r.field), self._shown(r.field, r.old_value),
                                    self._shown(r.field, r.new_value)])
            item.setData(0, Qt.UserRole, r.id)
            for col in (3, 4):
                item.setToolTip(col, (r.old_value if col == 3 else r.new_value) or "")
            tree.addTopLevelItem(item)
        if not revisions:
            tree.addTopLevelItem(QTreeWidgetItem(["No changes saved yet.", "", "", "", ""]))
        for col in range(3):
            tree.resizeColumnToContents(col)

    def _restore(self) -> None:
        items = self.tree.selectedItems()
        if not items or self._on_restore is None:
            return
        error = self._on_restore(items[0].data(0, Qt.UserRole))
        self.status.setText(error or "")
        if not error and self._reload is not None:
            self.fill(self._reload())



# ====================================================================== platform tags
class PlatformRow(QWidget):
    """One platform on the ticket: its logo, where it stands, and ONE choice:
    Not posting · Preparing · Scheduled · Posted. Choosing Scheduled asks when
    it goes live. (`going` is the underlying "is this a target" state, kept
    hidden so the rest of the ticket can read it.)"""
    goingToggled = Signal(str, bool)
    markRequested = Signal(str, str, object)   # platform, "scheduled" | "posted" | "none", when (datetime | None)
    handoffRequested = Signal(str)             # platform: open Prepare to Publish

    CHOICES = (("skip", "Not posting"), ("preparing", "Preparing"), ("scheduled", "Scheduled"), ("posted", "Posted"))

    def __init__(self, platform: str, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QButtonGroup

        from ..models import Platform
        from .logos import platform_pixmap
        self.platform = platform
        self.label = Platform.LABELS.get(platform, platform)
        self._loading = False
        self._choice = "skip"
        self._default_when = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 2, 0, 6)
        outer.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        logo = QLabel()
        logo.setPixmap(platform_pixmap(platform, 18))
        logo.setFixedSize(20, 20)
        tip(logo, self.label)
        top.addWidget(logo)
        self.name = QLabel(f"<b>{self.label}</b>")
        top.addWidget(self.name)
        self.state = _faint()
        self.state.setWordWrap(True)
        top.addWidget(self.state, 1)
        self.handoff_btn = tip(QPushButton("Publish…"),
                               f"Prepare to Publish on {self.label}: Reelay checks the video and caption, copies "
                               f"the caption, opens {self.label}'s uploader and shows the video file. You post it "
                               "there, then paste the link back.")
        set_prop(self.handoff_btn, "flat")
        self.handoff_btn.clicked.connect(lambda: self.handoffRequested.emit(self.platform))
        self.handoff_btn.setVisible(False)
        top.addWidget(self.handoff_btn, 0, Qt.AlignTop)
        outer.addLayout(top)
        seg = QHBoxLayout()
        seg.setContentsMargins(0, 0, 0, 0)
        seg.setSpacing(0)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QPushButton] = {}
        hints = {"skip": f"This video isn't going to {self.label}.",
                 "preparing": f"You plan to post it on {self.label} but haven't scheduled it yet. Its logo shows "
                              "(faded) on the card.",
                 "scheduled": f"You've scheduled it in {self.label}'s app. Reelay asks when it goes live, and moves "
                              "the card to Published after that time.",
                 "posted": f"It's live on {self.label}. When every platform it's going to is posted, the card "
                           "moves to Published."}
        for n, (key, text) in enumerate(self.CHOICES):
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setProperty("segment", "first" if n == 0 else "last" if n == len(self.CHOICES) - 1 else "middle")
            tip(btn, hints[key])
            self.group.addButton(btn)
            self.buttons[key] = btn
            seg.addWidget(btn)
            btn.clicked.connect(lambda _=False, k=key: self.choose(k))
        seg.addStretch()
        outer.addLayout(seg)
        self.going = QCheckBox()          # hidden model of "target"
        self.going.setVisible(False)
        self.going.toggled.connect(lambda on: not self._loading and self.goingToggled.emit(self.platform, on))

    def choose(self, key: str) -> None:
        """What happens when a choice is clicked."""
        if self._loading or key == self._choice:
            return
        previous, marked = self._choice, self._choice in ("scheduled", "posted")
        if key == "scheduled":
            dialog = WhenDialog(self.label, self.window(), self._default_when)
            if dialog.exec() != QDialog.Accepted:
                self._show_choice(previous)
                return
            self._choice = key
            self._set_going(True, quiet=True)
            self.markRequested.emit(self.platform, "scheduled", dialog.when())
        elif key == "posted":
            self._choice = key
            self._set_going(True, quiet=True)
            self.markRequested.emit(self.platform, "posted", None)
        else:
            self._choice = key
            if marked:
                self.markRequested.emit(self.platform, "none", None)
            self._set_going(key == "preparing", quiet=False)

    def _set_going(self, on: bool, quiet: bool) -> None:
        if quiet:
            self.going.blockSignals(True)
        self.going.setChecked(on)
        if quiet:
            self.going.blockSignals(False)

    def _show_choice(self, key: str) -> None:
        self._choice = key
        self.buttons[key].setChecked(True)

    def set_state(self, state, auto_enabled: bool, default_when: str | None = None) -> None:
        """state: pipeline.PlatformState."""
        self._loading = True
        self._default_when = default_when
        try:
            self.going.setChecked(state.target)
            live = state.state == "published"
            if live:
                choice = "posted"
            elif state.state in ("scheduled", "sending"):
                choice = "scheduled"
            elif state.target:
                choice = "preparing"
            else:
                choice = "skip"
            self._show_choice(choice)
            by_reelay = state.automatic or (state.state in ("scheduled", "sending", "failed") and not state.manual_kind)
            for btn in self.buttons.values():
                btn.setEnabled(not by_reelay)
            when = local_text(state.at) if state.at else ""
            text = {"none": "Preparing: not scheduled yet" if state.target else "",
                    "failed": "Reelay couldn't post it", "sending": "Reelay is posting it now",
                    "scheduled": f"Goes live {when}" if when else "Scheduled",
                    "published": f"Live since {when}" if when else "Live"}[state.state]
            if by_reelay and state.state != "published":
                text += " (by Reelay)"
            elif state.state == "none" and state.target and auto_enabled:
                text = "Preparing: Reelay can post it for you (Schedule below)"
            self.state.setText(text)
            set_prop(self.state, "danger", state.state == "failed")
            set_prop(self.state, "success", live)
            self.setToolTip(f"Reelay is handling {self.label}; this updates on its own." if by_reelay else "")
            # posted by hand (Reelay can't post here) and not live yet: offer the guided hand-off
            self.handoff_btn.setVisible(not auto_enabled and not by_reelay and not live)
        finally:
            self._loading = False


class WhenDialog(QDialog):
    """When does the post go live? (For posts scheduled by hand.)"""

    def __init__(self, platform_label: str, parent=None, default: str | None = None):
        super().__init__(parent)
        from datetime import timedelta
        self.setWindowTitle(f"Scheduled on {platform_label}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 14)
        lay.addWidget(_faint(f"When does it go live on {platform_label}? Reelay moves the card to Published once "
                             "every platform is live.", "muted"))
        when = parse_utc(default) if default else None
        if when is None:
            local = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
            start = QDateTime(local)
        else:
            start = QDateTime.fromSecsSinceEpoch(int(when.timestamp()))
        self.edit = tip(QDateTimeEdit(start), "Your local time")
        self.edit.setCalendarPopup(True)
        self.edit.setDisplayFormat("ddd MMM d, yyyy   h:mm AP")
        lay.addWidget(self.edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        buttons.button(QDialogButtonBox.Ok).setText("Mark as Scheduled")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def when(self) -> datetime:
        return self.edit.dateTime().toPython().astimezone(timezone.utc)
