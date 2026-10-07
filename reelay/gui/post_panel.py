"""Right-hand panel on the Board: edit and publish a video's YouTube post."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from PySide6.QtCore import QDateTime, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDateTimeEdit, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QStackedWidget,
                               QVBoxLayout, QWidget)

from ..models import Account, Platform, Post, PostStatus, Privacy, Stage, VideoRow
from ..platform_text import BLUESKY_MAX_GRAPHEMES, THREADS_MAX_CHARS, grapheme_count, threads_length
from ..publishing import TITLE_MAX, default_post, humanize_name, parse_utc, to_utc_iso
from ..pipeline import Card
from ..text import clock, local_text, short_title  # noqa: F401 (re-exported)
from .detail_panel import CoverView
from .table_model import fmt_count, fmt_date, fmt_duration
from .theme import set_prop
from .thumbnails import ThumbnailCache

PRIVACY_LABELS = {Privacy.PUBLIC: "Public", Privacy.UNLISTED: "Unlisted", Privacy.PRIVATE: "Private"}


def _section(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setProperty("section", True)
    return lab


class PostPanel(QFrame):
    saveRequested = Signal(object)            # Post
    uploadRequested = Signal(object, bool)    # Post, add #Shorts
    cancelUploadRequested = Signal(int)       # video row id
    connectRequested = Signal()

    def __init__(self, thumbs: ThumbnailCache, parent=None):
        super().__init__(parent)
        self.setObjectName("DetailFrame")
        self.setMinimumWidth(330)
        self.thumbs = thumbs
        self.card: Card | None = None
        self.accounts: list[Account] = []

        self.stack = QStackedWidget()
        empty = QWidget()
        el = QVBoxLayout(empty)
        el.addStretch()
        for text, prop in (("No video selected", "muted"),
                           ("Pick a card to write its YouTube title, description and schedule. "
                            "Drag cards between columns to move them through your pipeline.", "faint")):
            lab = QLabel(text)
            lab.setProperty(prop, True)
            lab.setWordWrap(True)
            lab.setAlignment(Qt.AlignCenter)
            el.addWidget(lab)
        el.addStretch()
        el.setContentsMargins(24, 24, 24, 24)
        self.stack.addWidget(empty)
        self.stack.addWidget(self._build_editor())
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.stack)

    # ---------------------------------------------------------------- build
    def _build_editor(self) -> QWidget:
        scroll = QScrollArea(objectName="DetailScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget(objectName="DetailBody")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(6)

        head = QHBoxLayout()
        self.cover = CoverView()
        self.cover.setFixedSize(72, 128)
        head.addWidget(self.cover, 0, Qt.AlignTop)
        hv = QVBoxLayout()
        self.hook = QLabel(objectName="DetailTitle")
        self.hook.setWordWrap(True)
        self.meta = QLabel()
        self.meta.setProperty("muted", True)
        self.meta.setWordWrap(True)
        self.stage_label = QLabel()
        self.stage_label.setProperty("faint", True)
        hv.addWidget(self.hook)
        hv.addWidget(self.meta)
        hv.addWidget(self.stage_label)
        hv.addStretch()
        head.addSpacing(10)
        head.addLayout(hv, 1)
        lay.addLayout(head)

        # status box for uploading / scheduled / published posts
        self.status_box = QFrame(objectName="Card")
        sb = QVBoxLayout(self.status_box)
        sb.setContentsMargins(12, 10, 12, 10)
        self.status_text = QLabel()
        self.status_text.setWordWrap(True)
        self.upload_bar = QProgressBar()
        self.upload_bar.setTextVisible(False)
        self.upload_bar.setRange(0, 1000)
        brow = QHBoxLayout()
        self.open_btn = QPushButton("Open on YouTube")
        self.open_btn.clicked.connect(self._open_remote)
        self.studio_btn = QPushButton("Edit in Studio")
        self.studio_btn.clicked.connect(self._open_studio)
        self.cancel_btn = QPushButton("Cancel Upload")
        set_prop(self.cancel_btn, "danger")
        self.cancel_btn.clicked.connect(lambda: self.card and self.cancelUploadRequested.emit(self.card.id))
        for b in (self.open_btn, self.studio_btn, self.cancel_btn):
            brow.addWidget(b)
        sb.addWidget(self.status_text)
        sb.addWidget(self.upload_bar)
        sb.addLayout(brow)
        lay.addSpacing(8)
        lay.addWidget(self.status_box)

        self.form = QWidget()
        fl = QVBoxLayout(self.form)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(6)

        fl.addWidget(_section("POST TO"))
        prow = QHBoxLayout()
        self.platform_boxes: dict[str, QCheckBox] = {}
        for platform in Platform.ALL:
            box = QCheckBox(Platform.LABELS[platform])
            box.toggled.connect(self._sync_platforms)
            self.platform_boxes[platform] = box
            prow.addWidget(box)
        prow.addStretch()
        fl.addLayout(prow)

        self.channel_label = _section("YOUTUBE CHANNEL")
        fl.addWidget(self.channel_label)
        crow = QHBoxLayout()
        self.account = QComboBox()
        crow.addWidget(self.account, 1)
        self.connect_btn = QPushButton("Connect…")
        self.connect_btn.clicked.connect(self.connectRequested.emit)
        crow.addWidget(self.connect_btn)
        self.channel_row = QWidget()
        self.channel_row.setLayout(crow)
        crow.setContentsMargins(0, 0, 0, 0)
        fl.addWidget(self.channel_row)

        trow = QHBoxLayout()
        trow.addWidget(_section("TITLE"))
        trow.addStretch()
        self.title_count = QLabel()
        self.title_count.setProperty("faint", True)
        trow.addWidget(self.title_count)
        fl.addLayout(trow)
        self.title = QLineEdit()
        self.title.setMaxLength(TITLE_MAX)
        self.title.textChanged.connect(lambda t: self.title_count.setText(f"{len(t)}/{TITLE_MAX}"))
        fl.addWidget(self.title)

        fl.addWidget(_section("DESCRIPTION"))
        self.description = QPlainTextEdit()
        self.description.setMinimumHeight(120)
        fl.addWidget(self.description)

        fl.addWidget(_section("TAGS"))
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("comma, separated, tags")
        fl.addWidget(self.tags)

        self.override_boxes: dict[str, tuple[QWidget, QPlainTextEdit, QLabel]] = {}
        for platform, limit in ((Platform.BLUESKY, BLUESKY_MAX_GRAPHEMES), (Platform.THREADS, THREADS_MAX_CHARS)):
            wrap = QWidget()
            wl = QVBoxLayout(wrap)
            wl.setContentsMargins(0, 0, 0, 0)
            wl.setSpacing(4)
            head = QHBoxLayout()
            head.addWidget(_section(f"{Platform.LABELS[platform].upper()} TEXT"))
            head.addStretch()
            counter = QLabel()
            counter.setProperty("faint", True)
            head.addWidget(counter)
            wl.addLayout(head)
            edit = QPlainTextEdit()
            edit.setFixedHeight(76)
            edit.setPlaceholderText("Leave empty to post the title (plus the description if it fits).")
            edit.textChanged.connect(lambda p=platform: self._update_counter(p))
            wl.addWidget(edit)
            self.override_boxes[platform] = (wrap, edit, counter)
            fl.addWidget(wrap)

        fl.addWidget(_section("VISIBILITY & TIMING"))
        self.privacy = QComboBox()
        for key, label in PRIVACY_LABELS.items():
            self.privacy.addItem(label, key)
        fl.addWidget(self.privacy)
        self.when = QComboBox()
        self.when.addItem("Publish as soon as it's uploaded", "now")
        self.when.addItem("Schedule for a date and time", "schedule")
        self.when.currentIndexChanged.connect(self._sync_when)
        fl.addWidget(self.when)
        self.publish_at = QDateTimeEdit()
        self.publish_at.setCalendarPopup(True)
        self.publish_at.setDisplayFormat("ddd MMM d, yyyy   h:mm AP")
        fl.addWidget(self.publish_at)
        self.sched_note = QLabel("Scheduled videos upload as private and YouTube makes them public at this time.")
        self.sched_note.setProperty("faint", True)
        self.sched_note.setWordWrap(True)
        fl.addWidget(self.sched_note)

        self.shorts = QCheckBox("Add #Shorts to the description")
        self.kids = QCheckBox("Made for kids")
        fl.addSpacing(4)
        fl.addWidget(self.shorts)
        fl.addWidget(self.kids)

        self.errors = QLabel()
        self.errors.setProperty("danger", True)
        self.errors.setWordWrap(True)
        fl.addSpacing(4)
        fl.addWidget(self.errors)

        arow = QHBoxLayout()
        self.save_btn = QPushButton("Save Draft")
        self.save_btn.clicked.connect(lambda: self.saveRequested.emit(self.collect()))
        self.upload_btn = QPushButton("Upload")
        set_prop(self.upload_btn, "primary")
        self.upload_btn.clicked.connect(lambda: self.uploadRequested.emit(self.collect(), self.shorts.isChecked()))
        arow.addWidget(self.save_btn)
        arow.addWidget(self.upload_btn, 1)
        fl.addSpacing(4)
        fl.addLayout(arow)
        lay.addWidget(self.form)
        lay.addStretch()
        scroll.setWidget(body)
        return scroll

    # ---------------------------------------------------------------- state
    def show_empty(self) -> None:
        self.card = None
        self.stack.setCurrentIndex(0)

    def show_card(self, card: Card, accounts: list[Account], add_shorts_default: bool) -> None:
        same = self.card is not None and self.card.id == card.id
        self.card, self.accounts = card, accounts
        self.stack.setCurrentIndex(1)
        item = card.row.item
        self.cover.set_cover(self.thumbs.cover(item))
        self.hook.setText(short_title(item.hook, 90))
        bits = [fmt_date(item.upload_date) if item.upload_date else "", fmt_duration(item.duration),
                f"{fmt_count(item.view_count)} views on {item.platform}" if item.view_count is not None else item.platform]
        self.meta.setText(" · ".join(b for b in bits if b and b != "—"))
        self.stage_label.setText(f"In {Stage.LABELS.get(card.row.stage, card.row.stage)}")

        post = card.post or default_post(card.id, item, accounts[0].id if accounts else None)
        if not same or not self.form.isEnabled():
            for platform, box in self.platform_boxes.items():
                box.blockSignals(True)
                box.setChecked(platform in post.target_platforms)
                box.blockSignals(False)
            for platform, (_wrap, edit, _counter) in self.override_boxes.items():
                edit.setPlainText({Platform.BLUESKY: post.bluesky_text,
                                   Platform.THREADS: post.threads_text}[platform] or "")
        self.account.blockSignals(True)
        self.account.clear()
        for acc in accounts:
            self.account.addItem(acc.label, acc.id)
        if not accounts:
            self.account.addItem("No YouTube channel connected", None)
        idx = self.account.findData(post.account_id)
        self.account.setCurrentIndex(max(idx, 0))
        self.account.blockSignals(False)
        self.connect_btn.setVisible(not accounts)

        if not same or not self.form.isEnabled():
            self.title.setText(post.title)
            self.description.setPlainText(post.description)
            self.tags.setText(", ".join(post.tags))
            self.tags.setCursorPosition(0)
            self.title.setCursorPosition(0)
            self.privacy.setCurrentIndex(max(self.privacy.findData(post.privacy), 0))
            when = parse_utc(post.publish_at)
            self.when.setCurrentIndex(1 if when else 0)
            default_time = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
            ts = when.timestamp() if when else default_time.timestamp()
            self.publish_at.setDateTime(QDateTime.fromSecsSinceEpoch(int(ts)))
            self.kids.setChecked(post.made_for_kids)
            self.shorts.setChecked(add_shorts_default)
            self.errors.setText(post.error if post.status in (PostStatus.FAILED, PostStatus.DRAFT) else "")
        self._sync_platforms()
        self._sync_when()
        self._sync_status()

    def _targets(self) -> list[str]:
        return [p for p, box in self.platform_boxes.items() if box.isChecked()]

    def _sync_platforms(self) -> None:
        targets = self._targets()
        youtube = Platform.YOUTUBE in targets
        for widget in (self.channel_label, self.channel_row, self.privacy, self.shorts, self.kids):
            widget.setVisible(youtube)
        for platform, (wrap, _edit, _counter) in self.override_boxes.items():
            wrap.setVisible(platform in targets)
            self._update_counter(platform)
        self._sync_when()

    def _update_counter(self, platform: str) -> None:
        _wrap, edit, counter = self.override_boxes[platform]
        text = edit.toPlainText()
        if platform == Platform.BLUESKY:
            used, limit = grapheme_count(text), BLUESKY_MAX_GRAPHEMES
        else:
            used, limit = threads_length(text), THREADS_MAX_CHARS
        counter.setText(f"{used}/{limit}" if text else "using the title")
        counter.setProperty("danger", used > limit)
        counter.style().unpolish(counter)
        counter.style().polish(counter)

    def _sync_when(self) -> None:
        scheduled = self.when.currentData() == "schedule"
        self.publish_at.setVisible(scheduled)
        self.sched_note.setVisible(scheduled)
        self.privacy.setEnabled(not scheduled)
        self.sched_note.setText("YouTube and Facebook get the video now and publish it at this time; Reelay posts "
                                "to Bluesky and Threads at this time (Reelay must be running).")
        self.upload_btn.setText("Schedule" if scheduled else "Publish Now")

    def _sync_status(self) -> None:
        card = self.card
        post = card.post if card else None
        locked = bool(post and post.status in (PostStatus.UPLOADING, PostStatus.SCHEDULED, PostStatus.PUBLISHED))
        self.form.setEnabled(not locked)
        self.form.setVisible(not locked)
        self.status_box.setVisible(locked or bool(card and card.row.stage == Stage.PUBLISHED))
        self.cancel_btn.setVisible(bool(post and post.status == PostStatus.UPLOADING))
        self.upload_bar.setVisible(bool(post and post.status == PostStatus.UPLOADING))
        has_remote = bool(post and post.remote_id and post.remote_url and "youtube" in post.remote_url)
        self.open_btn.setVisible(has_remote)
        self.studio_btn.setVisible(has_remote)
        if not card:
            return
        if post and post.status == PostStatus.UPLOADING:
            if card.queued:
                self.status_text.setText("Waiting for the current upload to finish…")
            else:
                self.status_text.setText(f"Uploading to {card.account.display_name if card.account else 'YouTube'}… "
                                         f"{(card.progress or 0):.0f}%")
            self.upload_bar.setValue(int((card.progress or 0) * 10))
        elif post and post.status == PostStatus.SCHEDULED:
            self.status_text.setText(f"Goes live {local_text(post.publish_at) or 'as soon as possible'}.\n"
                                     + self._platform_lines(card))
        elif post and post.status == PostStatus.PUBLISHED:
            self.status_text.setText("Published.\n" + self._platform_lines(card))
        elif card.row.stage == Stage.PUBLISHED:
            self.status_text.setText("Marked as published (posted outside Reelay).")
        if post and post.status == PostStatus.UPLOADING and len(card.dispatches) > 1:
            self.status_text.setText(self.status_text.text() + "\n" + self._platform_lines(card))

    @staticmethod
    def _platform_lines(card: Card) -> str:
        words = {"pending": "waiting", "dispatching": "sending now", "scheduled": "scheduled on the platform",
                 "published": "live", "failed": "FAILED"}
        lines = []
        for d in card.dispatches:
            line = f"• {Platform.LABELS.get(d.platform, d.platform)}: {words.get(d.status, d.status)}"
            if d.status == "pending" and d.attempts:
                line += f" (retry {d.attempts})"
            if d.error:
                line += f" — {d.error}"
            lines.append(line)
        return "\n".join(lines)

    def set_progress(self, row_id: int, pct: float) -> None:
        if self.card and self.card.id == row_id:
            self.card.progress = pct
            self._sync_status()

    def show_errors(self, errors: list[str]) -> None:
        self.errors.setText("\n".join(f"• {e}" for e in errors))

    def collect(self) -> Post:
        """The post as currently filled in."""
        assert self.card is not None
        base = self.card.post or Post(video_row=self.card.id)
        when = None
        if self.when.currentData() == "schedule":
            when = to_utc_iso(self.publish_at.dateTime().toPython())
        tags = [t.strip().lstrip("#") for t in self.tags.text().split(",") if t.strip()]
        overrides = {p: (edit.toPlainText().strip() or None) for p, (_w, edit, _c) in self.override_boxes.items()}
        return replace(base, account_id=self.account.currentData(), title=self.title.text().strip(),
                       description=self.description.toPlainText().strip(), tags=tags,
                       privacy=self.privacy.currentData(), publish_at=when, made_for_kids=self.kids.isChecked(),
                       target_platforms=self._targets() or [Platform.YOUTUBE],
                       bluesky_text=overrides[Platform.BLUESKY], threads_text=overrides[Platform.THREADS])

    def _open_remote(self) -> None:
        if self.card and self.card.post and self.card.post.remote_url:
            QDesktopServices.openUrl(QUrl(self.card.post.remote_url))

    def _open_studio(self) -> None:
        if self.card and self.card.post and self.card.post.remote_id:
            QDesktopServices.openUrl(QUrl(f"https://studio.youtube.com/video/{self.card.post.remote_id}/edit"))
