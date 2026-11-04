"""Right-hand panel on the Board: the video's ticket.

"Posting to" is one row of chips, one per platform: ticked means the video is
going there. Each ticked platform gets one PlatformSection (the same widget
for every platform, built from platform_specs) holding its ready-to-paste
copy, a live count using that platform's own rules, and a ⋯ menu to mark it
Scheduled or Posted or hand it off. Over-limit text is reported, never cut.
Shared hashtags sit under the platforms.

Below that, Post automatically offers only the platforms Reelay may post to on
its own (YouTube, plus any turned on with REELAY_PUBLISH_<PLATFORM>).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from PySide6.QtCore import QDateTime, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDateTimeEdit, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QStackedWidget,
                               QVBoxLayout, QWidget)

from ..models import Account, Platform, Post, PostStatus, Privacy, Stage
from ..pipeline import Card
from ..platform_specs import COPY_FIELDS, FIELD_LIMITS, SHARED_FIELDS, SPECS, TICKET, WHEN_ON, count, platform_of_field
from ..platform_text import BLUESKY_MAX_BYTES, INSTAGRAM_MAX_HASHTAGS, hashtag_count, resolve_text
from ..publishing import default_post, parse_utc, to_utc_iso
from ..text import clock, local_text, short_title  # noqa: F401 (re-exported)
from .detail_panel import CoverView
from .table_model import fmt_count, fmt_date, fmt_duration
from .theme import set_prop
from .tips import tip
from .thumbnails import ThumbnailCache
from .ticket_widgets import PlatformPicker, PlatformSection, PublishingHistory, ReadinessHeader, TicketDetails
from .video_widgets import VideoTools
from .widgets import Collapsible, scroll_page

PRIVACY_LABELS = {Privacy.PUBLIC: "Public", Privacy.UNLISTED: "Unlisted", Privacy.PRIVATE: "Private"}


def _section(text: str) -> QLabel:
    from .theme import heading
    lab = QLabel(heading(text))
    lab.setProperty("section", True)
    return lab


# Copy blocks: key → (label, multi-line, placeholder). Keys are Post fields (platform_specs.COPY_FIELDS),
# except "tags" (a list, edited as comma-separated text). Which platform each belongs to is in platform_specs.
BLOCKS: list[tuple[str, str, bool, str]] = [
    ("title", "TITLE", False, "The video's title on YouTube"),           # inside the YouTube section, so no prefix
    ("description", "DESCRIPTION", True, "What viewers read under the video"),
    ("tags", "TAGS", False, "Search words, separated by commas"),
    ("tiktok_text", "TIKTOK CAPTION", True, "Leave empty to use the YouTube description"),
    ("instagram_text", "INSTAGRAM CAPTION", True, "Leave empty to use the YouTube description"),
    ("bluesky_text", "BLUESKY POST", True, "Leave empty to use the title (and the description, if it fits)"),
    ("threads_text", "THREADS POST", True, "Leave empty to use the title (and the description, if it fits)"),
    ("facebook_text", "FACEBOOK POST", True, "Leave empty to use the YouTube description"),
    ("hashtags", "HASHTAGS (ALL PLATFORMS)", False, "#growth #healing"),
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
    """Label + live count over an editor, with Assist and Copy under it. `compact`: the block is
    its platform's only one, so the section's header already names it and shows its count."""

    def __init__(self, key: str, label: str, multiline: bool, placeholder: str, on_copy, on_change, on_assist=None,
                 compact: bool = False):
        super().__init__()
        self.key = key
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0 if compact else 4, 0, 0)
        lay.setSpacing(4)
        head = QHBoxLayout()
        title = tip(_section(label), BLOCK_TIPS.get(key, ""))
        title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)   # clips rather than widening the panel
        title.setMinimumWidth(60)
        head.addWidget(title, 1)
        self.counter = QLabel()
        self.counter.setProperty("faint", True)
        head.addWidget(self.counter)
        title.setVisible(not compact)
        self.counter.setVisible(not compact)
        if not compact:
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
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.copy_btn = QPushButton("Copy")
        set_prop(self.copy_btn, "flat")
        tip(self.copy_btn, "Copy this text so you can paste it into the app or website")
        self.copy_btn.clicked.connect(lambda: on_copy(self))
        if on_assist is not None and key != "tags":
            from PySide6.QtWidgets import QMenu, QToolButton
            self.assist_btn = QToolButton(text="Assist ▾")
            self.assist_btn.setPopupMode(QToolButton.InstantPopup)
            self.assist_btn.setAutoRaise(True)
            tip(self.assist_btn, "Free writing help on this computer: check spelling and grammar, or rewrite with "
                                 "a local AI model. Nothing is sent to a paid service.")
            menu = QMenu(self.assist_btn)
            menu.addAction("Check Spelling && Grammar…", lambda: on_assist(self, "check"))
            menu.addAction("Rewrite with Local AI…", lambda: on_assist(self, "rewrite"))
            self.assist_btn.setMenu(menu)
            actions.addWidget(self.assist_btn)
        actions.addWidget(self.copy_btn)
        lay.addLayout(actions)

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
    ticketFieldCommitted = Signal(int, str, object)   # video row id, field, value
    markPostedRequested = Signal(int)                 # video row id
    unmarkRequested = Signal(int)                     # manual post id
    revisionsRequested = Signal(int)                  # video row id
    fillRequested = Signal(int)                       # video row id
    transcriptRequested = Signal(int)                 # video row id
    targetsChanged = Signal(int, list)                # video row id, target platforms
    platformMarkRequested = Signal(int, str, str, object, str, str)   # row id, platform, state, when, url, note
    handoffRequested = Signal(int, str)                     # row id, platform: Prepare to Publish
    soundRequested = Signal(int, str)                       # row id, platform: choose its own sound
    soundRemoved = Signal(int, str)                         # row id, platform: back to the ticket's sound
    recoverRequested = Signal(int)                          # row id: decide about interrupted sends

    def __init__(self, thumbs: ThumbnailCache, parent=None):
        super().__init__(parent)
        self.setObjectName("DetailFrame")
        self.setMinimumWidth(300)
        self.thumbs = thumbs
        self.card: Card | None = None
        self.accounts: list[Account] = []
        self.enabled: frozenset[str] = frozenset({Platform.YOUTUBE})
        self.readiness_for = None   # callable(Post) -> Readiness, set by the Board
        self.store = None           # set by the Board (writing help remembers your dictionary and model)
        self.stage_name = None      # callable(stage) -> column name, set by the Board
        self.original_path_for = None   # callable(VideoRow) -> Path | None, set by the Board
        self._readiness_timer = QTimer(self, singleShot=True, interval=200, timeout=self._refresh_readiness)
        self._states: dict = {}     # platform → pipeline.PlatformState, for the card on show
        self._links: dict[str, str] = {}   # platform → its live post's link, when known

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
        scroll, body, lay = scroll_page((18, 18, 18, 18), 6, name="DetailScroll", body_name="DetailBody",
                                        sideways=False)

        # Title block: the title with its column as a pill on the right, the facts under it. With the player
        # above (no cover), it lines up exactly with the readiness card below.
        head = QHBoxLayout()
        head.setSpacing(0)
        self.cover = CoverView()
        self.cover.setFixedSize(72, 128)
        head.addWidget(self.cover, 0, Qt.AlignTop)
        self.cover_gap = QWidget()
        self.cover_gap.setFixedWidth(12)
        head.addWidget(self.cover_gap)
        hv = QVBoxLayout()
        hv.setSpacing(3)
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        self.hook = QLabel(objectName="CommandTitle")
        self.hook.setWordWrap(True)
        title_row.addWidget(self.hook, 1)
        self.stage_label = QLabel(objectName="StagePill")
        tip(self.stage_label, "The Board column this video is in")
        title_row.addWidget(self.stage_label, 0, Qt.AlignTop)
        hv.addLayout(title_row)
        self.meta = QLabel()
        self.meta.setProperty("muted", True)
        self.meta.setWordWrap(True)
        hv.addWidget(self.meta)
        hv.addStretch()
        head.addLayout(hv, 1)
        lay.addLayout(head)

        # ---- is it ready to leave the building?
        lay.addSpacing(10)
        self.readiness_header = ReadinessHeader()
        lay.addWidget(self.readiness_header)

        # status box for uploading / scheduled / published posts
        self.status_box = QFrame(objectName="Card")
        sb = QVBoxLayout(self.status_box)
        sb.setContentsMargins(12, 10, 12, 10)
        sb.setSpacing(6)
        self.status_text = QLabel()
        self.status_text.setWordWrap(True)
        self.upload_bar = QProgressBar()
        self.upload_bar.setTextVisible(False)
        self.upload_bar.setRange(0, 1000)
        self.open_btn = tip(QPushButton("Open on YouTube"), "Watch this video on YouTube")
        self.open_btn.clicked.connect(self._open_remote)
        self.studio_btn = tip(QPushButton("Edit in YouTube Studio"), "Change this video's details in YouTube Studio")
        self.studio_btn.clicked.connect(self._open_studio)
        self.cancel_btn = tip(QPushButton("Cancel Upload"), "Stop uploading this video. Nothing is posted.")
        set_prop(self.cancel_btn, "danger")
        self.cancel_btn.clicked.connect(lambda: self.card and self.cancelUploadRequested.emit(self.card.id))
        self.recover_btn = tip(QPushButton("Decide What Happened…"),
                               "A send was cut off (Reelay closed, crashed or lost power) or missed its time. "
                               "Say whether it was posted, try again, or cancel it.")
        set_prop(self.recover_btn, "primary")
        self.recover_btn.clicked.connect(lambda: self.card and self.recoverRequested.emit(self.card.id))
        sb.addWidget(self.status_text)
        sb.addWidget(self.upload_bar)
        for b in (self.recover_btn, self.open_btn, self.studio_btn, self.cancel_btn):
            sb.addWidget(b)
        lay.addSpacing(8)
        lay.addWidget(self.status_box)

        # ---- where it's going: one chip per platform (the ticket's tags)
        lay.addSpacing(10)
        self.picker = PlatformPicker([spec.key for spec in TICKET])
        self.picker.platformToggled.connect(self._on_chip)
        self.platform_boxes = self.picker.chips             # platform → its chip (checked = going)
        lay.addWidget(self.picker)

        # ---- the copy: one section per ticked platform, all the same widget
        lay.addSpacing(6)
        self.none_note = QLabel("Tick a platform above to write its caption.")
        self.none_note.setProperty("faint", True)
        self.none_note.setWordWrap(True)
        lay.addWidget(self.none_note)
        fill_row = QHBoxLayout()
        fill_row.addStretch()
        self.fill_btn = tip(QPushButton("Fill Empty from Original Post"),
                            "Fill this ticket's empty boxes from the original caption, within each platform's "
                            "limits. Nothing already written changes, and nothing is shortened.")
        set_prop(self.fill_btn, "flat")
        self.fill_btn.clicked.connect(lambda: self.card and self.fillRequested.emit(self.card.id))
        fill_row.addWidget(self.fill_btn)
        self.repurpose_btn = tip(
            QPushButton("Repurpose with Muse Spark"),
            "Generate platform-ready copy across YouTube, TikTok, Instagram, Bluesky, Threads, "
            "and Facebook using Meta Muse Spark. Shows a proposal for your review before applying.",
        )
        set_prop(self.repurpose_btn, "primary")
        self.repurpose_btn.clicked.connect(self._repurpose)
        fill_row.addWidget(self.repurpose_btn)
        lay.addLayout(fill_row)
        self.blocks: dict[str, CopyBlock] = {}
        self.sections: dict[str, PlatformSection] = {}
        specs = {key: (label, multiline, placeholder) for key, label, multiline, placeholder in BLOCKS}

        def block(key: str, compact: bool = False) -> CopyBlock:
            label, multiline, placeholder = specs[key]
            self.blocks[key] = CopyBlock(key, label, multiline, placeholder, self._copy, self._update_counts,
                                         self._assist, compact=compact)
            return self.blocks[key]

        for spec in TICKET:
            section = PlatformSection(spec.key)
            section.markRequested.connect(
                lambda p, state, when, url, note: self.card and self.platformMarkRequested.emit(
                    self.card.id, p, state, when, url, note))
            section.handoffRequested.connect(lambda p: self.card and self.handoffRequested.emit(self.card.id, p))
            section.soundRequested.connect(lambda p: self.card and self.soundRequested.emit(self.card.id, p))
            section.soundRemoved.connect(lambda p: self.card and self.soundRemoved.emit(self.card.id, p))
            for key in spec.copy_fields:
                section.add(block(key, compact=len(spec.copy_fields) == 1))
            if spec.key == Platform.YOUTUBE:
                seo = tip(QPushButton("Get SEO Suggestions"), "Keyword ideas from YouTube search, a score for the "
                                                              "title and tags, and one-click tags. Free.")
                set_prop(seo, "flat")
                seo.clicked.connect(self._seo)
                section.add(seo)
                self.seo_btn = seo
            section.hide()                                  # shown while its chip is ticked
            self.sections[spec.key] = section
            lay.addWidget(section)
        self.hashtags_section = Collapsible(
            "HASHTAGS (ALL PLATFORMS)", key="copy.hashtags", expanded=False,
            hint="One set of hashtags you can add to any platform's text. The right side counts them.")
        for key in SHARED_FIELDS:
            self.hashtags_section.add(block(key))
        lay.addWidget(self.hashtags_section)
        self.copied = QLabel()
        self.copied.setProperty("faint", True)
        lay.addWidget(self.copied)
        self._copied_timer = QTimer(self, singleShot=True, interval=2500, timeout=lambda: self.copied.setText(""))

        # ---- the video file itself: local edits
        self.video_tools = VideoTools()
        self.video_section = Collapsible(
            "VIDEO", key="video", expanded=False,
            hint="Remove or replace the sound and add captions, on this computer. Edits make a new copy that "
                 "Reelay posts instead; the original download is never changed.")
        self.video_section.add(self.video_tools)
        self.video_tools.editChanged.connect(lambda _row: (self._refresh_video_summary(),
                                                            self._readiness_timer.start()))
        lay.addWidget(self.video_section)

        # ---- automatic posting
        self.form = QWidget()
        fl = QVBoxLayout(self.form)
        fl.setContentsMargins(0, 8, 0, 0)
        fl.setSpacing(6)
        self.manual_note = QLabel()
        self.manual_note.setProperty("faint", True)
        self.manual_note.setWordWrap(True)
        fl.addWidget(self.manual_note)

        self.channel_label = tip(_section("YOUTUBE CHANNEL"), "Which of your connected channels gets the video")
        fl.addWidget(self.channel_label)
        crow = QHBoxLayout()
        crow.setContentsMargins(0, 0, 0, 0)
        self.account = tip(QComboBox(), "Which of your connected YouTube channels gets the video")
        self.account.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.account.setMinimumContentsLength(10)
        crow.addWidget(self.account, 1)
        self.connect_btn = tip(QPushButton("Connect…"), "Go to Accounts to connect a YouTube channel")
        self.connect_btn.clicked.connect(self.connectRequested.emit)
        crow.addWidget(self.connect_btn)
        self.channel_row = QWidget()
        self.channel_row.setLayout(crow)
        fl.addWidget(self.channel_row)

        fl.addWidget(tip(_section("WHO CAN SEE IT, AND WHEN"), "Choose who can watch it on YouTube and when it goes live"))
        self.privacy = tip(QComboBox(), "Public: anyone. Unlisted: only people with the link. Private: only you.")
        self.privacy.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.privacy.setMinimumContentsLength(10)
        for key, label in PRIVACY_LABELS.items():
            self.privacy.addItem(label, key)
        fl.addWidget(self.privacy)
        self.when = tip(QComboBox(), "Post right away, or pick a date and time")
        self.when.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.when.setMinimumContentsLength(10)
        self.when.addItem("Publish as soon as it's uploaded", "now")
        self.when.addItem("Schedule for a date and time", "schedule")
        self.when.currentIndexChanged.connect(self._sync_when)
        fl.addWidget(self.when)
        self.publish_at = tip(QDateTimeEdit(), "When the video goes live, in your local time")
        self.publish_at.setCalendarPopup(True)
        self.publish_at.setDisplayFormat("ddd MMM d, yyyy   h:mm AP")
        self.publish_at.dateTimeChanged.connect(lambda _dt: self._sync_dock())
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
        self.auto_section = Collapsible(
            "POST AUTOMATICALLY", key="auto", expanded=True,
            hint="Reelay posts to the ticked platforms it can post to itself (YouTube, and others you've turned "
                 "on in Settings). Everything else, you post by hand and mark Scheduled or Posted from its ⋯ menu.")
        self.auto_section.add(self.form)
        lay.addWidget(self.auto_section)

        self.errors = QLabel()
        self.errors.setProperty("danger", True)
        self.errors.setWordWrap(True)
        lay.addSpacing(4)
        lay.addWidget(self.errors)

        # (Save and Schedule live in the action dock pinned under the scroll area; see below.)

        # ---- the ticket's own fields, and where it has gone
        lay.addSpacing(12)
        self.details = TicketDetails()
        self.details.fieldCommitted.connect(
            lambda key, value: self.card and self.ticketFieldCommitted.emit(self.card.id, key, value))
        self.details.transcriptRequested.connect(lambda: self.card and self.transcriptRequested.emit(self.card.id))
        lay.addWidget(self.details)   # collapsible on its own ("Details and provenance")
        lay.addSpacing(10)
        self.history = PublishingHistory()
        self.history.markRequested.connect(lambda: self.card and self.markPostedRequested.emit(self.card.id))
        self.history.unmarkRequested.connect(self.unmarkRequested.emit)
        self.history.revisionsRequested.connect(lambda: self.card and self.revisionsRequested.emit(self.card.id))
        self.history_section = Collapsible("WHERE IT'S BEEN POSTED", key="history", expanded=False,
                                           hint="Every post of this video, automatic or by hand, per platform")
        self.history_section.add(self.history)
        lay.addWidget(self.history_section)
        lay.addStretch()

        # ---- the action dock: always in reach, however far you've scrolled
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(0)
        pl.addWidget(scroll, 1)
        self.dock = QWidget(objectName="ActionDock")
        dl = QVBoxLayout(self.dock)
        dl.setContentsMargins(12, 10, 12, 12)
        dl.setSpacing(8)
        self.dock_summary = QLabel(objectName="DockSummary")
        self.dock_summary.setWordWrap(True)
        dl.addWidget(self.dock_summary)
        arow = QHBoxLayout()
        self.save_btn = tip(QPushButton("Save Changes"), "Save this ticket without posting anything")
        self.save_btn.clicked.connect(lambda: self.saveRequested.emit(self.collect()))
        self.upload_btn = tip(QPushButton("Schedule"), "Hand the video to the ticked platforms at the chosen time")
        set_prop(self.upload_btn, "primary")
        self.upload_btn.clicked.connect(lambda: self.uploadRequested.emit(self.collect(), self.shorts.isChecked()))
        arow.addWidget(self.save_btn)
        arow.addWidget(self.upload_btn, 1)
        dl.addLayout(arow)
        pl.addWidget(self.dock)
        return page

    def use_player(self, on: bool = True) -> None:
        """The inspector shows the video above this panel, so the small cover isn't needed."""
        self.cover.setVisible(not on)
        self.cover_gap.setVisible(not on)

    def _refresh_video_summary(self) -> None:
        self.video_section.set_summary(self.video_tools.summary() if self.card else "")

    # ---------------------------------------------------------------- state
    def set_enabled_platforms(self, enabled: frozenset[str]) -> None:
        """Platforms Reelay may post to automatically; the others are copy-paste only."""
        self.enabled = frozenset(enabled) | {Platform.YOUTUBE}
        off = [s.label for s in SPECS.values() if s.posting == WHEN_ON and s.key not in self.enabled]
        off_text = f"{', '.join(off)}: automatic posting is off (turn it on in Settings). " if off else ""
        self.manual_note.setText("Reelay posts automatically to the ticked platforms it can post to. " + off_text +
                                 "TikTok and Instagram are always posted by hand: copy the caption, post in their "
                                 "app, then mark it Scheduled or Posted from its ⋯ menu above. (TikTok only allows "
                                 "automatic posting for apps it has approved.)")
        self.manual_note.setVisible(True)

    def show_empty(self) -> None:
        self.card = None
        self.stack.setCurrentIndex(0)

    def has_unsaved_changes(self) -> bool:
        """Copy typed here that differs from what's saved (only checked when the ticket closes)."""
        if self.card is None or self._locked():
            return False
        post = self.card.post or default_post(self.card.id, self.card.row.item, None)
        for key, block in self.blocks.items():
            saved = getattr(post, key)
            if key == "tags":
                typed = [t.strip().lstrip("#") for t in block.text().split(",") if t.strip()]
                if typed != list(saved or []):
                    return True
            elif block.text().strip() != (saved or "").strip():
                return True
        return False

    def show_card(self, card: Card, accounts: list[Account], add_shorts_default: bool) -> None:
        same = self.card is not None and self.card.id == card.id
        self.card, self.accounts = card, accounts
        self.stack.setCurrentIndex(1)
        item = card.row.item
        self.cover.set_cover(self.thumbs.cover(item))
        for platform, section in self.sections.items():       # a quiet ♪ where a platform has its own sound
            sound = card.sounds.get(platform)
            section.set_sound(sound.name if sound else None)
        self.hook.setText(short_title(item.hook, 90))
        bits = [fmt_date(item.upload_date) if item.upload_date else "", fmt_duration(item.duration),
                f"{fmt_count(item.view_count)} views on {item.platform}" if item.view_count is not None else item.platform]
        self.meta.setText(" · ".join(b for b in bits if b and b != "—"))
        name = self.stage_name(card.row.stage) if self.stage_name else Stage.LABELS.get(card.row.stage, card.row.stage)
        self.stage_label.setText(name)

        original = self.original_path_for(card.row) if self.original_path_for else None
        if not same or original != self.video_tools.original:
            self.video_tools.set_video(self.store, card.id, original)
        self._refresh_video_summary()

        post = card.post or default_post(card.id, item, accounts[0].id if accounts else None)
        locked = self._locked()
        if not same or locked:
            for platform, chip in self.platform_boxes.items():
                self.picker.set_chip(platform, platform in post.target_platforms, chip.isEnabled())
            self._sync_sections(fresh=not same)
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
        self.readiness_header.set_readiness(card.readiness)

    def set_ticket(self, others: list[tuple[int, str]], history: dict) -> None:
        """The ticket fields and publishing history for the card on show."""
        if self.card is None:
            return
        from ..products import video_products
        self.details.set_ticket(self.card.row.ticket, self.card.id, others, video_products(self.card.row.item))
        self.history.set_history(history)
        # the newest live link per platform, for its section's header
        self._links = {p: e.url for p, entries in history.items() for e in entries
                       if e.url and e.status in ("published", "posted")}

    def _refresh_readiness(self) -> None:
        """Live checklist while editing (debounced; nothing is saved)."""
        if self.card is None or self.readiness_for is None or self._locked():
            return
        try:
            self.readiness_header.set_readiness(self.readiness_for(self.collect()))
        except Exception:  # noqa: BLE001 — a preview must never break editing
            pass

    def _targets(self) -> list[str]:
        """Going platforms Reelay can post to itself."""
        return [p for p in self._going() if p in self.enabled]

    def _going(self) -> list[str]:
        return [spec.key for spec in TICKET if self.platform_boxes[spec.key].isChecked()]

    def _on_chip(self, platform: str, on: bool) -> None:
        """A chip was ticked or unticked. Unticking a platform that's marked Scheduled or Posted asks first, then
        clears the mark exactly as Back to Preparing does (the same pipeline call), so nothing is lost silently."""
        if self.card is None:
            return
        state = self._states.get(platform)
        marked = bool(state and state.manual_kind and state.state in ("scheduled", "published"))
        if not on and marked:
            name = SPECS[platform].label
            word = "Posted" if state.state == "published" else "Scheduled"
            answer = QMessageBox.question(
                self, f"Stop Posting to {name}?",
                f"{name} is marked {word}. Untick it to stop posting there and clear that mark? The post on "
                f"{name} itself isn't touched.")
            if answer != QMessageBox.Yes:
                self.picker.set_chip(platform, True)
                return
            self.platformMarkRequested.emit(self.card.id, platform, "none", None, "", "")
        self._sync_sections(opened=platform if on else None)
        self._sync_platforms()
        if self._going():
            self.targetsChanged.emit(self.card.id, self._going())

    def _sync_sections(self, fresh: bool = False, opened: str | None = None) -> None:
        """Show a caption section for each ticked platform. A new ticket opens them all when there are two or
        fewer, otherwise only the first; a platform just ticked opens its own."""
        going = self._going()
        for n, (platform, section) in enumerate((p, self.sections[p]) for p in going):
            if fresh:
                section.set_expanded(len(going) <= 2 or n == 0)
        for platform, section in self.sections.items():
            section.setVisible(platform in going)
        if opened in self.sections:
            self.sections[opened].set_expanded(True)
        self.none_note.setVisible(not going)

    def set_platform_states(self, states: dict, default_when: str | None) -> None:
        """Refresh each platform's chip and section header (pipeline.platform_states), and the summary."""
        self._states = dict(states)
        for platform, section in self.sections.items():
            state = states[platform]
            section.set_state(state, platform in self.enabled, default_when, self._links.get(platform, ""))
            by_reelay = state.automatic or (state.state in ("scheduled", "sending", "failed")
                                            and not state.manual_kind)
            self.picker.set_chip(platform, state.target, not by_reelay,
                                 f"Reelay is handling {section.label}; this updates on its own." if by_reelay else "")
        self._sync_sections()
        going = [s for s in states.values() if s.target]
        live = sum(1 for s in going if s.state == "published")
        sched = sum(1 for s in going if s.state in ("scheduled", "sending"))
        prep = len(going) - sched - live
        self.picker.summary.setText(f"{prep} preparing · {sched} scheduled · {live} posted" if going else "")

    def _sync_platforms(self) -> None:
        youtube = Platform.YOUTUBE in self._targets()
        for widget in (self.channel_label, self.channel_row, self.privacy, self.shorts, self.kids):
            widget.setVisible(youtube)
        self._sync_when()
        self._readiness_timer.start()

    def _update_counts(self) -> None:
        b = self.blocks
        counts = {key: count(key, b[key].text()) for key in COPY_FIELDS}      # platform_specs: the limits
        ig = b["instagram_text"].text()
        tags_in_ig = hashtag_count(ig)
        ig_text, ig_over = counts["instagram_text"]
        counts["instagram_text"] = (f"{ig_text} · #{tags_in_ig}/{INSTAGRAM_MAX_HASHTAGS}",
                                    ig_over or tags_in_ig > INSTAGRAM_MAX_HASHTAGS)
        bs = b["bluesky_text"].text()
        bs_bytes = len(bs.encode("utf-8"))
        bs_text, bs_over = counts["bluesky_text"]
        counts["bluesky_text"] = (bs_text + (f" · {bs_bytes:,}/{BLUESKY_MAX_BYTES:,} bytes"
                                             if bs_bytes > BLUESKY_MAX_BYTES * 0.8 else ""),
                                  bs_over or bs_bytes > BLUESKY_MAX_BYTES)
        n = hashtag_count(b["hashtags"].text())
        counts["hashtags"] = (f"{n} hashtag{'s' if n != 1 else ''}", False)
        for key, (text, over) in counts.items():
            if platform_of_field(key) not in (None, Platform.YOUTUBE) and not b[key].text():
                text, over = "empty", False         # an empty platform box uses YouTube's text instead
            b[key].set_count(text, over)
        for platform, section in self.sections.items():
            # the header counts the caption itself (YouTube: the description; its title and tags count inside)
            any_over = self._summary(SPECS[platform].copy_fields)[1]
            text, _over = self._summary((SPECS[platform].caption_field,))
            section.set_count("⚠ too long" if any_over else text, any_over)
        self.hashtags_section.set_summary(*self._summary(SHARED_FIELDS))
        self._readiness_timer.start()

    def _summary(self, keys) -> tuple[str, bool]:
        """One line for a section header: its counts, "empty", or "⚠ too long"."""
        b = self.blocks
        if any(b[k].counter.property("danger") for k in keys):
            return "⚠ too long", True
        filled = [k for k in keys if b[k].text().strip()]
        return (" · ".join(b[k].counter.text() for k in filled) if filled else "empty"), False

    # ---------------------------------------------------------------- writing help & SEO
    LIMITS = {key: (lim.limit, lim.measure) for key, lim in FIELD_LIMITS.items()}   # for the rewrite helper

    def _assist(self, block: CopyBlock, action: str) -> None:
        from .assist_dialogs import GrammarDialog, RewriteDialog
        if self.store is None or block.editor.isReadOnly():
            return
        label = next(lbl for key, lbl, _m, _p in BLOCKS if key == block.key).title()
        if not block.text().strip():
            self.copied.setText("Write something first; the helper works on what's there.")
            self._copied_timer.start()
            return
        if action == "check":
            dialog = GrammarDialog(block.text(), self.store, label, self)
            if dialog.exec() == GrammarDialog.Accepted and dialog.text != block.text():
                block.set_text(dialog.text)
        else:
            limit, counter = self.LIMITS.get(block.key, (None, len))
            brand = self.store.active_brand_profile(self.card.row.profile_id) if self.card else None
            dialog = RewriteDialog(block.text(), self.store, label, limit, counter, self, brand=brand)
            if dialog.exec() == RewriteDialog.Accepted and dialog.value():
                block.set_text(dialog.value())

    def _seo(self) -> None:
        from .assist_dialogs import SEODialog
        from ..seo import seeds
        if self.card is None:
            return
        b = self.blocks
        tags = [t.strip().lstrip("#") for t in b["tags"].text().split(",") if t.strip()]
        ticket = self.card.row.ticket
        seed_list = seeds(b["title"].text(), ticket.topic or "", ticket.keywords or "", self.card.row.item.hashtags)
        dialog = SEODialog(b["title"].text(), tags, b["description"].text(), seed_list, self)
        if dialog.exec() == SEODialog.Accepted and dialog.chosen():
            merged = tags + [t for t in dialog.chosen() if t.lower() not in {x.lower() for x in tags}]
            b["tags"].set_text(", ".join(merged))

    def _repurpose(self) -> None:
        if self.card is None or self._locked():
            return
        from .repurpose_dialog import RepurposeDialog
        b = self.blocks
        target_card_id = self.card.id
        target_row_id = getattr(getattr(self.card, "row", None), "id", self.card.id)
        starting_values = {key: block.text() for key, block in b.items()}
        title = b["title"].text().strip() if "title" in b else ""
        description = b["description"].text().strip() if "description" in b else ""
        dialog = RepurposeDialog(
            self.card,
            self.store,
            parent=self,
            title=title,
            description=description,
        )
        if dialog.exec() == RepurposeDialog.Accepted and dialog.proposal:
            current_row_id = (
                getattr(getattr(self.card, "row", None), "id", self.card.id)
                if self.card is not None
                else None
            )
            same_ticket = (
                self.card is not None
                and self.card.id == target_card_id
                and current_row_id == target_row_id
            )
            editable = not self._locked() and not any(
                block.editor.isReadOnly() for block in b.values()
            )
            unchanged = all(
                b[key].text() == starting_val for key, starting_val in starting_values.items()
            )
            if not (same_ticket and editable and unchanged):
                self.copied.setText(
                    "Ticket changed while reviewing proposal; proposal was not applied."
                )
                self._copied_timer.start()
                return

            p = dialog.proposal
            for key, val in p.items():
                if key in b and val is not None:
                    b[key].set_text(val)
            self._update_counts()
            self.copied.setText("Applied Muse Spark proposal! Click Save Changes to commit.")
            self._copied_timer.start()

    def _copy(self, block: CopyBlock) -> None:
        """Copy a block; empty platform blocks copy what Reelay would use instead."""
        text = block.text().strip()
        if not text and self.card is not None:
            post = self.collect()
            platform = platform_of_field(block.key)
            if platform in (Platform.BLUESKY, Platform.THREADS):     # built from the title, as Reelay posts them
                try:
                    text = resolve_text(post, platform)
                except ValueError:
                    text = post.title
            elif platform not in (None, Platform.YOUTUBE):          # TikTok, Instagram, Facebook: the description
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
        self._sync_dock()

    def _sync_dock(self) -> None:
        """One line above Schedule saying exactly what will happen."""
        targets = self._targets()
        if not targets:
            self.dock_summary.setText("Nothing posts automatically yet: tick a platform above.")
            return
        names = ", ".join(SPECS[p].label for p in targets)
        if self.when.currentData() == "schedule":
            dt = self.publish_at.dateTime().toPython()
            when = f"{dt:%a %b} {dt.day}, {dt.hour % 12 or 12}:{dt:%M}{'am' if dt.hour < 12 else 'pm'}"
            self.dock_summary.setText(f"Goes live {when} on {names}.")
        else:
            self.dock_summary.setText(f"Publishes on {names} as soon as it's uploaded.")

    def _locked(self) -> bool:
        post = self.card.post if self.card else None
        return bool(post and post.status in (PostStatus.UPLOADING, PostStatus.SCHEDULED, PostStatus.PUBLISHED))

    def _sync_status(self) -> None:
        card = self.card
        post = card.post if card else None
        locked = self._locked()
        for block in self.blocks.values():   # copy stays copyable; edits wait until it's unscheduled
            block.set_read_only(locked)
        self.fill_btn.setVisible(not locked)
        self.repurpose_btn.setVisible(not locked)
        self.form.setVisible(not locked)
        self.save_btn.setVisible(not locked)
        self.upload_btn.setVisible(not locked)
        self.dock.setVisible(not locked)        # scheduled or live: the status box above says what's next
        self.status_box.setVisible(locked or bool(card and card.row.stage == Stage.PUBLISHED))
        self.cancel_btn.setVisible(bool(post and post.status == PostStatus.UPLOADING))
        self.upload_bar.setVisible(bool(post and post.status == PostStatus.UPLOADING))
        has_remote = bool(post and post.remote_id and post.remote_url and "youtube" in post.remote_url)
        self.open_btn.setVisible(has_remote)
        self.studio_btn.setVisible(has_remote)
        held = [d for d in card.dispatches if d.status == "interrupted"] if card else []
        self.recover_btn.setVisible(bool(held))
        if not card:
            return
        if held:
            self.status_text.setText("Waiting for you: a send was interrupted or missed its time.\n"
                                     + self._platform_lines(card))
        elif post and post.status == PostStatus.UPLOADING:
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
            self.status_text.setText("Posted.\n" + self._platform_lines(card))
        elif card.row.stage == Stage.PUBLISHED:
            self.status_text.setText("Marked as published (posted outside Reelay).")

    @staticmethod
    def _platform_lines(card: Card) -> str:
        words = {"pending": "waiting", "dispatching": "sending now", "scheduled": "scheduled on the platform",
                 "published": "live", "failed": "FAILED", "interrupted": "INTERRUPTED"}
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
        copy = {key: (b[key].text().strip() or None) for key in COPY_FIELDS     # the optional boxes
                if key not in SPECS[Platform.YOUTUBE].copy_fields}
        targets = self._going() or [Platform.YOUTUBE]
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
