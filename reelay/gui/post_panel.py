"""Right-hand panel on the Board: the video's ticket.

Copy blocks hold ready-to-paste text for every platform (YouTube title,
description and tags; TikTok, Instagram, Bluesky, Threads and Facebook copy;
shared hashtags), each with a Copy button and a live count using that
platform's own rules. Over-limit text is reported, never cut.

Below that, Auto-post offers only the platforms Reelay may post to on its own
(YouTube, plus any turned on with REELAY_PUBLISH_<PLATFORM>).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from PySide6.QtCore import QDateTime, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDateTimeEdit, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QStackedWidget,
                               QVBoxLayout, QWidget)

from ..models import Account, Platform, Post, PostStatus, Privacy, Stage
from ..pipeline import Card
from ..platform_text import (BLUESKY_MAX_BYTES, BLUESKY_MAX_GRAPHEMES, FACEBOOK_MAX_CHARS, INSTAGRAM_MAX_CHARS,
                             INSTAGRAM_MAX_HASHTAGS, THREADS_MAX_CHARS, TIKTOK_MAX_CHARS, grapheme_count,
                             hashtag_count, resolve_text, threads_length)
from ..publishing import DESCRIPTION_MAX_BYTES, TAGS_MAX_CHARS, TITLE_MAX, default_post, parse_utc, to_utc_iso
from ..text import clock, local_text, short_title  # noqa: F401 (re-exported)
from .detail_panel import CoverView
from .table_model import fmt_count, fmt_date, fmt_duration
from .theme import set_prop
from .tips import tip
from .thumbnails import ThumbnailCache

PRIVACY_LABELS = {Privacy.PUBLIC: "Public", Privacy.UNLISTED: "Unlisted", Privacy.PRIVATE: "Private"}


def _section(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setProperty("section", True)
    return lab


def _count(used: int, limit: int, unit: str = "") -> tuple[str, bool]:
    return f"{used:,}/{limit:,}{unit}", used > limit


# Copy blocks: key → (label, multi-line, placeholder). Keys are Post fields,
# except "tags" (a list, edited as comma-separated text).
BLOCKS: list[tuple[str, str, bool, str]] = [
    ("title", "YOUTUBE TITLE", False, "The video's title on YouTube"),
    ("description", "YOUTUBE DESCRIPTION", True, "What viewers read under the video"),
    ("tags", "YOUTUBE TAGS", False, "Search words, separated by commas"),
    ("tiktok_text", "TIKTOK CAPTION", True, "Leave empty to use the YouTube description"),
    ("instagram_text", "INSTAGRAM CAPTION", True, "Leave empty to use the YouTube description"),
    ("bluesky_text", "BLUESKY POST", True, "Leave empty to use the title (and the description, if it fits)"),
    ("threads_text", "THREADS POST", True, "Leave empty to use the title (and the description, if it fits)"),
    ("facebook_text", "FACEBOOK POST", True, "Leave empty to use the YouTube description"),
    ("hashtags", "HASHTAGS FOR EVERY PLATFORM", False, "#growth #healing"),
]

BLOCK_TIPS = {
    "title": "Up to 100 characters. Shown on YouTube and used as the starting point for other platforms.",
    "description": "Up to 5,000 bytes (emoji count as several). Also the fallback caption for TikTok, "
                   "Instagram and Facebook.",
    "tags": "Words that help people find the video in YouTube search. Up to 500 characters in total.",
    "tiktok_text": "Up to 4,000 characters. Copy it and paste it into TikTok when you post.",
    "instagram_text": "Up to 2,200 characters and 30 hashtags. Copy it and paste it into Instagram when you post.",
    "bluesky_text": "Up to 300 characters. Emoji and accented letters each count once, but the post must also "
                    "stay under 3,000 bytes.",
    "threads_text": "Up to 500 characters. Emoji count as several characters on Threads.",
    "facebook_text": "Up to 63,206 characters.",
    "hashtags": "One set of hashtags you can add to any platform's text.",
}


class CopyBlock(QWidget):
    """Label + live count + Copy button over an editor."""

    def __init__(self, key: str, label: str, multiline: bool, placeholder: str, on_copy, on_change):
        super().__init__()
        self.key = key
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.addWidget(tip(_section(label), BLOCK_TIPS.get(key, "")))
        head.addStretch()
        self.counter = QLabel()
        self.counter.setProperty("faint", True)
        head.addWidget(self.counter)
        self.copy_btn = QPushButton("Copy")
        set_prop(self.copy_btn, "flat")
        tip(self.copy_btn, "Copy this text so you can paste it into the app or website")
        self.copy_btn.clicked.connect(lambda: on_copy(self))
        head.addWidget(self.copy_btn)
        lay.addLayout(head)
        if multiline:
            self.editor = QPlainTextEdit()
            self.editor.setFixedHeight(110 if key == "description" else 72)
            self.editor.textChanged.connect(on_change)
        else:
            self.editor = QLineEdit()
            self.editor.textChanged.connect(lambda _t: on_change())
        self.editor.setPlaceholderText(placeholder)
        tip(self.editor, BLOCK_TIPS.get(key, ""))
        tip(self.counter, "How much of the platform's limit this text uses. Red means it's too long; "
                          "Reelay never cuts your text.")
        lay.addWidget(self.editor)

    def text(self) -> str:
        return self.editor.toPlainText() if isinstance(self.editor, QPlainTextEdit) else self.editor.text()

    def set_text(self, value: str) -> None:
        if isinstance(self.editor, QPlainTextEdit):
            self.editor.setPlainText(value)
        else:
            self.editor.setText(value)
            self.editor.setCursorPosition(0)

    def set_read_only(self, read_only: bool) -> None:
        self.editor.setReadOnly(read_only)

    def set_count(self, text: str, over: bool) -> None:
        self.counter.setText(text)
        set_prop(self.counter, "danger", over)


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
        self.enabled: frozenset[str] = frozenset({Platform.YOUTUBE})

        self.stack = QStackedWidget()
        empty = QWidget()
        el = QVBoxLayout(empty)
        el.addStretch()
        for text, prop in (("No video selected", "muted"),
                           ("Pick a card to see its ticket: ready-to-paste copy for every platform. "
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
        self.open_btn = tip(QPushButton("Open on YouTube"), "Watch this video on YouTube")
        self.open_btn.clicked.connect(self._open_remote)
        self.studio_btn = tip(QPushButton("Edit in YouTube Studio"), "Change this video's details in YouTube Studio")
        self.studio_btn.clicked.connect(self._open_studio)
        self.cancel_btn = tip(QPushButton("Cancel Upload"), "Stop uploading this video. Nothing is posted.")
        set_prop(self.cancel_btn, "danger")
        self.cancel_btn.clicked.connect(lambda: self.card and self.cancelUploadRequested.emit(self.card.id))
        for b in (self.open_btn, self.studio_btn, self.cancel_btn):
            brow.addWidget(b)
        sb.addWidget(self.status_text)
        sb.addWidget(self.upload_bar)
        sb.addLayout(brow)
        lay.addSpacing(8)
        lay.addWidget(self.status_box)

        # ---- copy blocks (the ticket)
        lay.addSpacing(6)
        self.blocks: dict[str, CopyBlock] = {}
        for key, label, multiline, placeholder in BLOCKS:
            block = CopyBlock(key, label, multiline, placeholder, self._copy, self._update_counts)
            self.blocks[key] = block
            lay.addWidget(block)
        self.copied = QLabel()
        self.copied.setProperty("faint", True)
        lay.addWidget(self.copied)
        self._copied_timer = QTimer(self, singleShot=True, interval=2500, timeout=lambda: self.copied.setText(""))

        # ---- automatic posting
        self.form = QWidget()
        fl = QVBoxLayout(self.form)
        fl.setContentsMargins(0, 8, 0, 0)
        fl.setSpacing(6)
        fl.addWidget(tip(_section("POST AUTOMATICALLY TO"),
                         "Reelay posts to the ticked platforms for you. Everything else, you copy and paste "
                         "from the text above."))
        self.platform_row = QHBoxLayout()
        self.platform_boxes: dict[str, QCheckBox] = {}
        for platform in Platform.ALL:
            box = tip(QCheckBox(Platform.LABELS[platform]), f"Let Reelay post this video to {Platform.LABELS[platform]}")
            box.toggled.connect(self._sync_platforms)
            self.platform_boxes[platform] = box
            self.platform_row.addWidget(box)
        self.platform_row.addStretch()
        fl.addLayout(self.platform_row)
        self.manual_note = QLabel()
        self.manual_note.setProperty("faint", True)
        self.manual_note.setWordWrap(True)
        fl.addWidget(self.manual_note)

        self.channel_label = tip(_section("YOUTUBE CHANNEL"), "Which of your connected channels gets the video")
        fl.addWidget(self.channel_label)
        crow = QHBoxLayout()
        crow.setContentsMargins(0, 0, 0, 0)
        self.account = tip(QComboBox(), "Which of your connected YouTube channels gets the video")
        crow.addWidget(self.account, 1)
        self.connect_btn = tip(QPushButton("Connect a Channel…"), "Go to Accounts to connect a YouTube channel")
        self.connect_btn.clicked.connect(self.connectRequested.emit)
        crow.addWidget(self.connect_btn)
        self.channel_row = QWidget()
        self.channel_row.setLayout(crow)
        fl.addWidget(self.channel_row)

        fl.addWidget(tip(_section("WHO CAN SEE IT, AND WHEN"), "Choose who can watch it on YouTube and when it goes live"))
        self.privacy = tip(QComboBox(), "Public: anyone. Unlisted: only people with the link. Private: only you.")
        for key, label in PRIVACY_LABELS.items():
            self.privacy.addItem(label, key)
        fl.addWidget(self.privacy)
        self.when = tip(QComboBox(), "Post right away, or pick a date and time")
        self.when.addItem("Publish as soon as it's uploaded", "now")
        self.when.addItem("Schedule for a date and time", "schedule")
        self.when.currentIndexChanged.connect(self._sync_when)
        fl.addWidget(self.when)
        self.publish_at = tip(QDateTimeEdit(), "When the video goes live, in your local time")
        self.publish_at.setCalendarPopup(True)
        self.publish_at.setDisplayFormat("ddd MMM d, yyyy   h:mm AP")
        fl.addWidget(self.publish_at)
        self.sched_note = QLabel()
        self.sched_note.setProperty("faint", True)
        self.sched_note.setWordWrap(True)
        fl.addWidget(self.sched_note)

        self.shorts = tip(QCheckBox("Add #Shorts to the description"),
                          "Helps YouTube treat a vertical video under 3 minutes as a Short")
        self.kids = tip(QCheckBox("Made for kids"), "Required by YouTube if the video is made for children. "
                                                    "Comments and some features are turned off.")
        fl.addSpacing(4)
        fl.addWidget(self.shorts)
        fl.addWidget(self.kids)
        lay.addWidget(self.form)

        self.errors = QLabel()
        self.errors.setProperty("danger", True)
        self.errors.setWordWrap(True)
        lay.addSpacing(4)
        lay.addWidget(self.errors)

        arow = QHBoxLayout()
        self.save_btn = tip(QPushButton("Save Changes"), "Save this ticket without posting anything")
        self.save_btn.clicked.connect(lambda: self.saveRequested.emit(self.collect()))
        self.upload_btn = tip(QPushButton("Schedule"), "Hand the video to the ticked platforms at the chosen time")
        set_prop(self.upload_btn, "primary")
        self.upload_btn.clicked.connect(lambda: self.uploadRequested.emit(self.collect(), self.shorts.isChecked()))
        arow.addWidget(self.save_btn)
        arow.addWidget(self.upload_btn, 1)
        lay.addSpacing(4)
        lay.addLayout(arow)
        lay.addStretch()
        scroll.setWidget(body)
        return scroll

    # ---------------------------------------------------------------- state
    def set_enabled_platforms(self, enabled: frozenset[str]) -> None:
        """Platforms Reelay may post to automatically; the others are copy-paste only."""
        self.enabled = frozenset(enabled) | {Platform.YOUTUBE}
        for platform, box in self.platform_boxes.items():
            box.setVisible(platform in self.enabled)
        off = [Platform.LABELS[p] for p in Platform.ALL if p not in self.enabled]
        off_text = f"{', '.join(off)}: automatic posting is off (turn it on in Settings). " if off else ""
        self.manual_note.setText(off_text + "TikTok and Instagram: copy the caption above and post in their apps. "
                                 "(TikTok only allows automatic posting for apps it has approved.)")
        self.manual_note.setVisible(True)

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
        locked = self._locked()
        if not same or locked:
            for platform, box in self.platform_boxes.items():
                box.blockSignals(True)
                box.setChecked(platform in post.target_platforms and platform in self.enabled)
                box.blockSignals(False)
            for key, block in self.blocks.items():
                value = getattr(post, key)
                block.set_text(", ".join(value) if key == "tags" else (value or ""))
        self.account.blockSignals(True)
        self.account.clear()
        for acc in accounts:
            self.account.addItem(acc.label, acc.id)
        if not accounts:
            self.account.addItem("No YouTube channel connected", None)
        self.account.setCurrentIndex(max(self.account.findData(post.account_id), 0))
        self.account.blockSignals(False)
        self.connect_btn.setVisible(not accounts)

        if not same or locked:
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
        self._update_counts()
        self._sync_status()

    def _targets(self) -> list[str]:
        return [p for p, box in self.platform_boxes.items() if box.isChecked() and p in self.enabled]

    def _sync_platforms(self) -> None:
        youtube = Platform.YOUTUBE in self._targets()
        for widget in (self.channel_label, self.channel_row, self.privacy, self.shorts, self.kids):
            widget.setVisible(youtube)
        self._sync_when()

    def _update_counts(self) -> None:
        b = self.blocks
        title, desc = b["title"].text(), b["description"].text()
        tags = [t.strip().lstrip("#") for t in b["tags"].text().split(",") if t.strip()]
        counts = {
            "title": _count(len(title.strip()), TITLE_MAX),
            "description": _count(len(desc.encode("utf-8")), DESCRIPTION_MAX_BYTES, " bytes"),
            "tags": _count(sum(len(t) + 1 for t in tags), TAGS_MAX_CHARS),
            "tiktok_text": _count(len(b["tiktok_text"].text()), TIKTOK_MAX_CHARS),
            "threads_text": _count(threads_length(b["threads_text"].text()), THREADS_MAX_CHARS),
            "facebook_text": _count(len(b["facebook_text"].text()), FACEBOOK_MAX_CHARS),
        }
        ig = b["instagram_text"].text()
        ig_text, ig_over = _count(len(ig), INSTAGRAM_MAX_CHARS)
        tags_in_ig = hashtag_count(ig)
        counts["instagram_text"] = (f"{ig_text} · #{tags_in_ig}/{INSTAGRAM_MAX_HASHTAGS}",
                                    ig_over or tags_in_ig > INSTAGRAM_MAX_HASHTAGS)
        bs = b["bluesky_text"].text()
        bs_text, bs_over = _count(grapheme_count(bs), BLUESKY_MAX_GRAPHEMES)
        bs_bytes = len(bs.encode("utf-8"))
        counts["bluesky_text"] = (bs_text + (f" · {bs_bytes:,}/{BLUESKY_MAX_BYTES:,} bytes"
                                             if bs_bytes > BLUESKY_MAX_BYTES * 0.8 else ""),
                                  bs_over or bs_bytes > BLUESKY_MAX_BYTES)
        n = hashtag_count(b["hashtags"].text())
        counts["hashtags"] = (f"{n} hashtag{'s' if n != 1 else ''}", False)
        for key, (text, over) in counts.items():
            if key in ("bluesky_text", "threads_text", "tiktok_text", "instagram_text", "facebook_text") \
                    and not b[key].text():
                text, over = "empty", False
            b[key].set_count(text, over)

    def _copy(self, block: CopyBlock) -> None:
        """Copy a block; empty platform blocks copy what Reelay would use instead."""
        text = block.text().strip()
        if not text and self.card is not None:
            post = self.collect()
            if block.key in (Platform.BLUESKY + "_text", Platform.THREADS + "_text"):
                try:
                    text = resolve_text(post, block.key.removesuffix("_text"))
                except ValueError:
                    text = post.title
            elif block.key in ("tiktok_text", "instagram_text", "facebook_text"):
                text = post.description
        if not text:
            self.copied.setText("Nothing to copy.")
        else:
            QGuiApplication.clipboard().setText(text)
            label = next(lbl for key, lbl, _m, _p in BLOCKS if key == block.key).title()
            self.copied.setText(f"Copied the {label.lower()} ({len(text):,} characters). Paste it where you post.")
        self._copied_timer.start()

    def _sync_when(self) -> None:
        scheduled = self.when.currentData() == "schedule"
        self.publish_at.setVisible(scheduled)
        self.sched_note.setVisible(scheduled)
        self.privacy.setEnabled(not scheduled)
        parts = []
        targets = self._targets()
        if Platform.YOUTUBE in targets or Platform.FACEBOOK in targets:
            parts.append("YouTube and Facebook get the video now and publish it at this time.")
        if Platform.BLUESKY in targets or Platform.THREADS in targets:
            parts.append("Reelay posts to Bluesky and Threads at this time, so it must be running.")
        self.sched_note.setText(" ".join(parts))
        self.upload_btn.setText("Schedule" if scheduled else "Publish Now")
        self.upload_btn.setEnabled(bool(targets))

    def _locked(self) -> bool:
        post = self.card.post if self.card else None
        return bool(post and post.status in (PostStatus.UPLOADING, PostStatus.SCHEDULED, PostStatus.PUBLISHED))

    def _sync_status(self) -> None:
        card = self.card
        post = card.post if card else None
        locked = self._locked()
        for block in self.blocks.values():   # copy stays copyable; edits wait until it's unscheduled
            block.set_read_only(locked)
        self.form.setVisible(not locked)
        self.save_btn.setVisible(not locked)
        self.upload_btn.setVisible(not locked)
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
            if len(card.dispatches) > 1:
                self.status_text.setText(self.status_text.text() + "\n" + self._platform_lines(card))
        elif post and post.status == PostStatus.SCHEDULED:
            self.status_text.setText(f"Goes live {local_text(post.publish_at) or 'as soon as possible'}.\n"
                                     + self._platform_lines(card))
        elif post and post.status == PostStatus.PUBLISHED:
            self.status_text.setText("Published.\n" + self._platform_lines(card))
        elif card.row.stage == Stage.PUBLISHED:
            self.status_text.setText("Marked as published (posted outside Reelay).")

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
                line += f": {d.error}"
            lines.append(line)
        return "\n".join(lines)

    def set_progress(self, row_id: int, pct: float) -> None:
        if self.card and self.card.id == row_id:
            self.card.progress = pct
            self._sync_status()

    def show_errors(self, errors: list[str]) -> None:
        self.errors.setText("\n".join(f"• {e}" for e in errors))

    def collect(self) -> Post:
        """The post as currently filled in. Text is taken exactly as typed (validated on save)."""
        assert self.card is not None
        base = self.card.post or Post(video_row=self.card.id)
        when = None
        if self.when.currentData() == "schedule":
            when = to_utc_iso(self.publish_at.dateTime().toPython())
        b = self.blocks
        tags = [t.strip().lstrip("#") for t in b["tags"].text().split(",") if t.strip()]
        copy = {key: (b[key].text().strip() or None) for key in
                ("tiktok_text", "instagram_text", "bluesky_text", "threads_text", "facebook_text", "hashtags")}
        # Keep copy-paste-only targets (set by agents) alongside the auto-post choices.
        manual = [p for p in base.target_platforms if p not in self.enabled]
        targets = self._targets() + manual or [Platform.YOUTUBE]
        return replace(base, account_id=self.account.currentData(), title=b["title"].text().strip(),
                       description=b["description"].text().strip(), tags=tags,
                       privacy=self.privacy.currentData(), publish_at=when, made_for_kids=self.kids.isChecked(),
                       target_platforms=targets, **copy)

    def _open_remote(self) -> None:
        if self.card and self.card.post and self.card.post.remote_url:
            QDesktopServices.openUrl(QUrl(self.card.post.remote_url))

    def _open_studio(self) -> None:
        if self.card and self.card.post and self.card.post.remote_id:
            QDesktopServices.openUrl(QUrl(f"https://studio.youtube.com/video/{self.card.post.remote_id}/edit"))
