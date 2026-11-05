"""Batch Review GUI for Reelay: Multi-ticket proposal generation and review via Meta Muse Spark.

Post-hackathon product work for Reelay (branch batch-v2).
Founder: Andrew Baxter. Local-first PySide6 desktop tool for video creators.

Design & Guarantees:
- Creator framing: "Repurpose these videos (N videos) — One batch, six platforms each, you approve before saving."
- Privacy Banner: Short banner "Your video text stays private until you approve it" with "ⓘ How it works"
  button opening technical disclosure pop-over.
- Explicit Batch Opt-in: Checkbox "Allow Muse Spark to draft copy for this batch" (unchecked by default).
  Batch generation cannot start until checked!
- Queue / Ticket List View:
    * Thumbnail (from ThumbnailCache if available) or fallback icon.
    * Video title (clear, readable).
    * Platform logo chips: YouTube, TikTok, Instagram, Bluesky, Threads, Facebook (visible at all times!).
    * Status pill: "Queued", "Running", "Ready", "Failed", "Applied", "Saved", "Cancelled".
    * Compliance pill: "PASS" (green pill, success_soft) or "OVER LIMIT" (red pill, danger_soft).
    * Review indicator: "Reviewed" vs "Unreviewed" (creator has opened and looked at the proposal).
    * Persistence indicator: "In Memory Only" vs "Saved".
    * Actions per ticket: "Review", "Retry", "Cancel", "Save".
- Proposal Review:
    * Full RepurposeDialog style review with disclosure, source snapshot, logo chips, field cards,
      live counters, PASS/OVER LIMIT pills.
    * Variant switcher if variants exist (allows creator to select alternate title or hook!).
    * "Apply to Ticket" populates in-memory fields, marks ticket as reviewed=True and applied=True.
- Batch Action: "Apply Reviewed Only":
    * Crucial safety requirement: ONLY applies proposals for tickets that creator has reviewed.
    * NEVER applies unreviewed tickets!
- Save Changes Step:
    * Explicit "Save Changes" action per ticket and batch.
    * "Apply populates in-memory fields only. Save commits changes to ticket."
    * Never auto-publishes, auto-posts, or auto-saves.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtGui import QCloseEvent, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..batch_repurpose import (
    JOB_STATUS_APPLIED,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_READY,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SAVED,
    BatchJob,
    extract_source_snapshot,
)
from ..models import Platform, Post
from ..platform_specs import FIELD_LIMITS, SPECS, TICKET, count
from ..platform_specs import label as platform_label
from ..privacy import (
    DISCLOSURE_TEXT,
    PrivacyOptInRequiredError,
    is_cloud_ai_enabled,
    set_cloud_ai_enabled,
)
from ..repurpose_engine import (
    EM_DASH,
    EM_DASH_ERROR,
    SCHEMA_FIELDS,
    RepurposeError,
    generate_repurpose_proposal,
)
from .icons import icon as app_icon, pixmap as app_pixmap
from .logos import platform_icon, platform_pixmap
from .theme import BASE_PT, P, set_prop
from .thumbnails import ThumbnailCache
from .tips import tip
from .widgets import label, scroll_page

log = logging.getLogger("reelay.gui.batch")

# Field definitions & limits
FIELD_NAMES: dict[str, str] = {
    "title": "Title",
    "description": "Description",
    "tags": "Tags",
    "tiktok_text": "Caption",
    "instagram_text": "Caption",
    "bluesky_text": "Post",
    "threads_text": "Post",
    "facebook_text": "Post",
    "hashtags": "Shared hashtags",
}

FIELD_UNITS: dict[str, str] = {
    "title": "characters",
    "description": "UTF-8 bytes",
    "tags": "characters",
    "tiktok_text": "characters",
    "instagram_text": "characters",
    "bluesky_text": "graphemes",
    "threads_text": "characters",
    "facebook_text": "characters",
}

MULTILINE = {"description", "tiktok_text", "instagram_text", "bluesky_text", "threads_text", "facebook_text"}
EDITOR_HEIGHT = {"description": 140, "facebook_text": 100, "tiktok_text": 100, "instagram_text": 100}
HASHTAG_PLATFORMS = {Platform.TIKTOK, Platform.INSTAGRAM, Platform.FACEBOOK}
SHARED_KEY = "_shared"

APPLY_NOTE = "Apply populates in-memory fields only. Save commits changes to ticket."


def counter_text(key: str, text: str) -> tuple[str, bool]:
    """'74/100 characters · PASS' and whether it fails (over limit, or # in Threads)."""
    if key == "hashtags":
        n = len(text.split())
        return f"{n} hashtag{'s' if n != 1 else ''} · PASS", False
    if key not in FIELD_LIMITS:
        return f"{len(text)} characters · PASS", False
    lim = FIELD_LIMITS[key]
    used = lim.measure(text)
    _, over = count(key, text)
    shown = f"{used:,}/{lim.limit:,} {FIELD_UNITS.get(key, 'characters')}"
    if key == "threads_text" and "#" in text:
        return f"{shown} · ⚠ remove # (Threads posts keep hashtags out of text)", True
    if EM_DASH in text:
        return f"{shown} · ⚠ contains em dash (—)", True
    return f"{shown} · {'⚠ OVER LIMIT' if over else 'PASS'}", over


def shadow(widget: QWidget, blur: int = 24, alpha: int = 120, dy: int = 6) -> None:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, dy)
    effect.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(effect)


# ====================================================================== Data Model
@dataclass
class BatchTicket:
    """Represents a single ticket item in the Batch Review GUI."""

    card: Any
    job: BatchJob
    variants_list: list[dict[str, str]] = field(default_factory=list)
    selected_variant_idx: int = 0
    applied_copy: dict[str, str] | None = None

    @property
    def id(self) -> int:
        return self.job.ticket_id

    @property
    def title(self) -> str:
        snap_title = (self.job.source_snapshot.get("title") or "").strip()
        if snap_title:
            return snap_title
        card_title = getattr(self.card, "title", None)
        if card_title and str(card_title).strip():
            return str(card_title).strip()
        return f"Ticket #{self.id}"

    @property
    def status_label(self) -> str:
        """User-facing capitalized status pill text."""
        mapping = {
            JOB_STATUS_QUEUED: "Queued",
            JOB_STATUS_RUNNING: "Running",
            JOB_STATUS_READY: "Ready",
            JOB_STATUS_FAILED: "Failed",
            JOB_STATUS_APPLIED: "Applied",
            JOB_STATUS_SAVED: "Saved",
            JOB_STATUS_CANCELLED: "Cancelled",
        }
        return mapping.get(self.job.status, self.job.status.capitalize())

    @property
    def reviewed(self) -> bool:
        return self.job.reviewed

    @reviewed.setter
    def reviewed(self, value: bool) -> None:
        self.job.mark_reviewed(value)

    @property
    def applied(self) -> bool:
        return self.job.applied

    @applied.setter
    def applied(self, value: bool) -> None:
        self.job.mark_applied(value)

    @property
    def saved(self) -> bool:
        return self.job.saved

    @saved.setter
    def saved(self, value: bool) -> None:
        self.job.mark_saved(value)

    @property
    def active_proposal(self) -> dict[str, str] | None:
        if self.applied_copy:
            return self.applied_copy
        if self.variants_list and 0 <= self.selected_variant_idx < len(self.variants_list):
            return self.variants_list[self.selected_variant_idx]
        return self.job.proposal

    @property
    def compliance_status(self) -> str | None:
        """Returns 'PASS', 'OVER LIMIT', or None if no proposal exists yet."""
        p = self.active_proposal
        if not p:
            return None
        for key in SCHEMA_FIELDS:
            if key not in FIELD_LIMITS:
                continue
            text = p.get(key, "")
            _, over = count(key, text)
            if over or EM_DASH in text:
                return "OVER LIMIT"
            if key == "threads_text" and "#" in text:
                return "OVER LIMIT"
        return "PASS"


# ====================================================================== Worker Thread
class BatchWorker(QThread):
    """Background worker executing batch proposal generation without blocking GUI."""

    ticketStarted = Signal(int)
    ticketSucceeded = Signal(int, dict, object)  # ticket_id, proposal, variants
    ticketFailed = Signal(int, str)              # ticket_id, error message
    progressUpdated = Signal(int, int, int, int) # completed, total, ready, failed
    batchFinished = Signal(int, int, bool)       # ready, failed, cancelled

    def __init__(
        self,
        tickets: list[BatchTicket],
        generate_fn: Callable[..., dict[str, str]] | None = None,
        variant_generator: Callable[..., Any] | None = None,
        store: Any = None,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.tickets = list(tickets)
        self.generate_fn = generate_fn or generate_repurpose_proposal
        self.variant_generator = variant_generator
        self.store = store
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def run(self) -> None:
        total = len(self.tickets)
        ready = 0
        failed = 0
        completed = 0

        for t in self.tickets:
            if self._cancel_event.is_set():
                break

            # If already generated or saved, keep existing result
            if t.job.status in (JOB_STATUS_READY, JOB_STATUS_APPLIED, JOB_STATUS_SAVED):
                completed += 1
                ready += 1
                self.progressUpdated.emit(completed, total, ready, failed)
                continue

            self.ticketStarted.emit(t.id)
            try:
                proposal = self.generate_fn(
                    title=t.job.source_snapshot.get("title", ""),
                    description=t.job.source_snapshot.get("description", ""),
                    notes=t.job.source_snapshot.get("notes", ""),
                    transcript=t.job.source_snapshot.get("transcript", ""),
                    brand_voice=t.job.source_snapshot.get("brand_voice", ""),
                    store=self.store,
                    opt_in=True,
                )
                variants = None
                if self.variant_generator:
                    try:
                        variants = self.variant_generator(t.job, proposal)
                    except Exception as e:
                        log.debug("Variant generation failed for %s: %s", t.id, e)

                ready += 1
                self.ticketSucceeded.emit(t.id, proposal, variants)
            except Exception as exc:
                log.warning("Batch repurpose failed for ticket %s: %s", t.id, exc)
                failed += 1
                self.ticketFailed.emit(t.id, str(exc))

            completed += 1
            self.progressUpdated.emit(completed, total, ready, failed)

        self.batchFinished.emit(ready, failed, self._cancel_event.is_set())


# ====================================================================== Review Dialog
class TicketProposalReviewDialog(QDialog):
    """Proposal review dialog for a single ticket within the batch flow.

    Displays disclosure, source snapshot, logo chips, field cards, live counters,
    PASS/OVER LIMIT pills, variant switcher, and 'Apply to Ticket' action.
    """

    def __init__(
        self,
        ticket: BatchTicket,
        store: Any = None,
        pipeline: Any = None,
        parent: QWidget | None = None,
        generate_fn: Callable | None = None,
    ) -> None:
        super().__init__(parent)
        self.ticket = ticket
        self.store = store
        self.pipeline = pipeline
        self.generate_fn = generate_fn or generate_repurpose_proposal
        self.edited_copy: dict[str, str] = {}
        self.editors: dict[str, QLineEdit | QPlainTextEdit] = {}
        self.counters: dict[str, QLabel] = {}
        self.field_boxes: dict[str, QWidget] = {}
        self.sections: dict[str, QWidget] = {}

        self.setWindowTitle(f"Review Copy — {self.ticket.title}")
        self.setMinimumSize(820, 680)
        self.resize(850, 840)

        # Style matching RepurposeDialog and Reelay theme
        self.setStyleSheet(f"""
            QDialog {{ background: {P.bg_primary}; color: {P.text_primary}; }}
            QFrame#RpCard {{ background: {P.bg_surface}; border: 1px solid {P.divider}; border-radius: 14px; }}
            QFrame#RpField {{ background: {P.bg_raised}; border: 1px solid {P.divider}; border-radius: 12px; }}
            QFrame#RpBanner {{ background: {P.accent_soft}; border: 1px solid {P.accent_ring}; border-radius: 12px; }}
            QFrame#RpOverlay {{ background: {P.bg_surface}; border: 1px solid {P.accent_ring}; border-radius: 16px; }}
            QLabel#RpTitle {{ font-size: {BASE_PT + 8}pt; font-weight: 800; }}
            QLabel#RpVideo {{ font-size: {BASE_PT + 2}pt; font-weight: 600; color: {P.text_secondary}; }}
            QLabel#RpSection {{ font-size: {BASE_PT + 3}pt; font-weight: 700; }}
            QLabel#RpFieldName {{ font-size: {BASE_PT + 1}pt; font-weight: 700; }}
            QLabel#RpPill {{ border-radius: 11px; padding: 3px 10px; font-weight: 700;
                             background: {P.bg_hover}; color: {P.text_secondary}; }}
            QLabel#RpPill[state="pass"] {{ background: {P.success_soft}; color: {P.success_ink}; }}
            QLabel#RpPill[state="fail"] {{ background: {P.danger_soft}; color: {P.danger_ink}; }}
            QLabel#RpPill[state="empty"] {{ background: {P.bg_hover}; color: {P.text_muted}; }}
            QFrame#RpField QPlainTextEdit, QFrame#RpField QLineEdit {{
                font-size: {BASE_PT + 1}pt; padding: 10px 12px; border-radius: 10px;
                background: {P.bg_surface}; border: 1px solid {P.border}; color: {P.text_primary}; }}
            QFrame#RpField QPlainTextEdit:focus, QFrame#RpField QLineEdit:focus {{
                border: 2px solid {P.accent_color}; }}
            QToolButton#RpChip {{ border: 1px solid {P.divider}; border-radius: 16px; padding: 5px 9px;
                                  background: transparent; color: {P.text_secondary}; font-weight: 600; }}
            QToolButton#RpChip:checked {{ background: {P.accent_soft}; color: {P.text_primary};
                                          border: 2px solid {P.accent_ring}; }}
            QPushButton#RpQuiet {{ background: transparent; border: 1px solid {P.divider}; color: {P.text_secondary};
                                   border-radius: 10px; padding: 8px 16px; }}
            QPushButton#RpQuiet:hover {{ color: {P.text_primary}; border-color: {P.border}; }}
            QPushButton[primary="true"] {{ border-radius: 10px; padding: 9px 20px; font-weight: 700; }}
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Scrolling content area
        self.scroll, _page, lay = scroll_page(margins=(24, 20, 24, 12), spacing=12, sideways=False)
        outer.addWidget(self.scroll, 1)

        # Header
        lay.addWidget(label("Review Muse Spark Copy", name="RpTitle"))
        lay.addWidget(label(self.ticket.title, name="RpVideo"))
        lay.addWidget(label("Review and edit copy across 6 platforms before applying to this ticket.", prop="muted"))

        # Privacy banner
        banner = QFrame(objectName="RpBanner")
        bl = QHBoxLayout(banner)
        bl.setContentsMargins(14, 10, 10, 10)
        bl.setSpacing(10)
        lock = QLabel()
        lock.setPixmap(app_icon("sparkles", 18).pixmap(18, 18))
        bl.addWidget(lock)
        bl.addWidget(label("<b>Your video text stays private until you approve it.</b>", rich=True), 1)
        self.info_btn = tip(QPushButton("ⓘ  How it works"), "What is sent, where it goes, and what stays private")
        self.info_btn.setObjectName("RpQuiet")
        self.info_btn.clicked.connect(self._toggle_disclosure)
        bl.addWidget(self.info_btn)
        lay.addWidget(banner)

        # Source snapshot card
        source_card = QFrame(objectName="RpCard")
        sl = QVBoxLayout(source_card)
        sl.setContentsMargins(16, 12, 16, 12)
        sl.setSpacing(6)
        srow = QHBoxLayout()
        srow.addWidget(label("<b>Source Snapshot — Read-only</b>", rich=True, wrap=False), 1)
        self.snapshot_toggle = QPushButton("Show all")
        self.snapshot_toggle.setObjectName("RpQuiet")
        self.snapshot_toggle.clicked.connect(self._toggle_snapshot)
        srow.addWidget(self.snapshot_toggle)
        sl.addLayout(srow)
        sl.addWidget(label("Source text grounded for Muse Spark generation.", prop="faint"))
        self.source_preview = QPlainTextEdit()
        self.source_preview.setReadOnly(True)
        self.source_preview.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.source_preview.setStyleSheet("QPlainTextEdit { min-height: 80px; }")
        self.source_preview.setFixedHeight(95)
        self.source_preview.setPlainText(self._format_snapshot(self.ticket.job.source_snapshot))
        sl.addWidget(self.source_preview)
        lay.addWidget(source_card)

        # Variant switcher if variants exist
        self.variant_card = QFrame(objectName="RpCard")
        vl = QHBoxLayout(self.variant_card)
        vl.setContentsMargins(16, 10, 16, 10)
        vl.setSpacing(10)
        vl.addWidget(label("<b>Copy Angle / Variant:</b>", rich=True, wrap=False))
        self.variant_combo = QComboBox()
        self.variant_combo.setMinimumWidth(220)
        self._populate_variants()
        self.variant_combo.currentIndexChanged.connect(self._on_variant_changed)
        vl.addWidget(self.variant_combo)
        self.add_variant_btn = tip(QPushButton("+ Alternate Angle"), "Duplicate proposal to craft an alternate angle")
        self.add_variant_btn.setObjectName("RpQuiet")
        self.add_variant_btn.clicked.connect(self._add_variant)
        vl.addWidget(self.add_variant_btn)
        vl.addStretch()
        lay.addWidget(self.variant_card)

        # Platform chips row (always visible!)
        chip_row = QHBoxLayout()
        chip_row.setSpacing(6)
        self.platform_chips: dict[str, QToolButton] = {}
        for spec in TICKET:
            chip = QToolButton(objectName="RpChip")
            chip.setText(platform_label(spec.key))
            chip.setIcon(platform_icon(spec.key, 18))
            chip.setIconSize(QSize(18, 18))
            chip.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            chip.setCheckable(True)
            chip.setChecked(True)
            chip.clicked.connect(lambda _c=False, k=spec.key: self._jump_to(k))
            self.platform_chips[spec.key] = chip
            chip_row.addWidget(chip)
        chip_row.addStretch()
        lay.addLayout(chip_row)

        # Results sections
        self.results_container = QWidget()
        self.fields_layout = QVBoxLayout(self.results_container)
        self.fields_layout.setContentsMargins(0, 0, 0, 0)
        self.fields_layout.setSpacing(12)
        lay.addWidget(self.results_container)

        groups: list[tuple[str, str, tuple[str, ...]]] = [
            (p.key, platform_label(p.key), SPECS[p.key].copy_fields) for p in TICKET
        ]
        groups.append((SHARED_KEY, "Shared hashtags", ("hashtags",)))

        for key, title_text, fields in groups:
            sec_card = QFrame(objectName="RpCard")
            sec_l = QVBoxLayout(sec_card)
            sec_l.setContentsMargins(16, 14, 16, 16)
            sec_l.setSpacing(10)
            hrow = QHBoxLayout()
            hrow.setSpacing(10)
            if key != SHARED_KEY:
                logo = QLabel()
                logo.setPixmap(platform_pixmap(key, 24))
                hrow.addWidget(logo)
            hrow.addWidget(label(title_text, name="RpSection"))
            hrow.addStretch()
            sec_l.addLayout(hrow)
            for f in fields:
                sec_l.addWidget(self._make_field(f, platform=None if key == SHARED_KEY else key))
            shadow(sec_card, blur=20, alpha=70, dy=4)
            self.sections[key] = sec_card
            self.fields_layout.addWidget(sec_card)

        # Footer
        footer = QFrame(objectName="RpCard")
        footer.setStyleSheet(f"QFrame#RpCard {{ border-radius: 0; border-left: none; border-right: none; "
                             f"border-bottom: none; background: {P.bg_primary}; }}")
        bottom = QHBoxLayout(footer)
        bottom.setContentsMargins(24, 12, 24, 14)
        bottom.setSpacing(10)

        self.footer_note = label(APPLY_NOTE, prop="faint")
        bottom.addWidget(self.footer_note, 1)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("RpQuiet")
        self.cancel_btn.clicked.connect(self.reject)
        bottom.addWidget(self.cancel_btn)

        self.save_btn = tip(QPushButton("Save Changes"), "Commit changes directly to ticket")
        self.save_btn.setObjectName("RpQuiet")
        self.save_btn.clicked.connect(self._save_ticket_changes)
        bottom.addWidget(self.save_btn)

        self.apply_btn = tip(QPushButton("Apply to Ticket"), "Populate ticket in-memory fields and mark as reviewed")
        set_prop(self.apply_btn, "primary", True)
        self.apply_btn.clicked.connect(self._accept_and_apply)
        bottom.addWidget(self.apply_btn)
        outer.addWidget(footer)

        # Technical disclosure overlay
        self.disclosure_backdrop = QFrame(self)
        self.disclosure_backdrop.setStyleSheet("background: rgba(0, 0, 0, 150);")
        self.disclosure_backdrop.hide()
        self.disclosure_overlay = QFrame(self, objectName="RpOverlay")
        ol = QVBoxLayout(self.disclosure_overlay)
        ol.setContentsMargins(20, 16, 20, 16)
        ol.setSpacing(10)
        orow = QHBoxLayout()
        orow.addWidget(label("How Muse Spark drafts work", name="RpSection"), 1)
        close = QPushButton("Got it")
        set_prop(close, "primary", True)
        close.clicked.connect(self._toggle_disclosure)
        orow.addWidget(close)
        ol.addLayout(orow)
        self.disclosure_label = label(DISCLOSURE_TEXT, prop="muted")
        self.disclosure_label.setWordWrap(True)
        ol.addWidget(self.disclosure_label)
        shadow(self.disclosure_overlay, blur=48, alpha=170, dy=12)
        self.disclosure_overlay.hide()

        # Load proposal copy into editors
        self._load_active_proposal()

    # ------------------------------------------------------------------ Overlay & UI helpers
    def _toggle_disclosure(self) -> None:
        o = self.disclosure_overlay
        if o.isVisible():
            o.hide()
            self.disclosure_backdrop.hide()
            return
        self._place_overlay()
        self.disclosure_backdrop.show()
        self.disclosure_backdrop.raise_()
        o.show()
        o.raise_()

    def _place_overlay(self) -> None:
        o = self.disclosure_overlay
        w = min(self.width() - 48, 600)
        o.setFixedWidth(w)
        o.adjustSize()
        o.move((self.width() - w) // 2, 70)
        self.disclosure_backdrop.setGeometry(0, 0, self.width(), self.height())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if getattr(self, "disclosure_overlay", None) is not None and self.disclosure_overlay.isVisible():
            self._place_overlay()

    def _toggle_snapshot(self) -> None:
        expanded = self.source_preview.height() > 100
        h = 95 if expanded else 260
        self.source_preview.setFixedHeight(h)
        self.snapshot_toggle.setText("Show all" if expanded else "Show less")

    def _jump_to(self, platform: str) -> None:
        sec = self.sections.get(platform)
        if sec is not None:
            self.scroll.ensureWidgetVisible(sec, 0, 30)

    def _make_field(self, key: str, platform: str | None = None) -> QWidget:
        box = QFrame(objectName="RpField")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(14, 12, 14, 14)
        bl.setSpacing(8)
        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        if platform:
            lg = QLabel()
            lg.setPixmap(platform_pixmap(platform, 16))
            hdr.addWidget(lg)
        hdr.addWidget(QLabel(FIELD_NAMES.get(key, key.capitalize()), objectName="RpFieldName"))
        hdr.addStretch()
        cnt = QLabel("", objectName="RpPill")
        cnt.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.counters[key] = cnt
        hdr.addWidget(cnt)
        bl.addLayout(hdr)

        if key in MULTILINE:
            ed = QPlainTextEdit()
            height = EDITOR_HEIGHT.get(key, 96)
            ed.setStyleSheet(f"QPlainTextEdit {{ min-height: {height - 24}px; }}")
            ed.setFixedHeight(height)
            ed.textChanged.connect(lambda k=key: self._update_field_count(k))
        else:
            ed = QLineEdit()
            ed.setMinimumHeight(40)
            ed.textChanged.connect(lambda _t, k=key: self._update_field_count(k))

        self.editors[key] = ed
        bl.addWidget(ed)
        self.field_boxes[key] = box
        return box

    @staticmethod
    def _format_snapshot(snap: dict[str, str]) -> str:
        return "\n".join([
            f"Title: {snap.get('title') or '(none)'}",
            f"Description: {snap.get('description') or '(none)'}",
            f"Notes: {snap.get('notes') or '(none)'}",
            f"Transcript: {snap.get('transcript') or '(none)'}",
            f"Brand Voice Guidance: {snap.get('brand_voice') or '(none)'}",
        ])

    # ------------------------------------------------------------------ Variants & Values
    def _populate_variants(self) -> None:
        self.variant_combo.blockSignals(True)
        self.variant_combo.clear()
        if not self.ticket.variants_list and self.ticket.job.proposal:
            self.ticket.variants_list.append(dict(self.ticket.job.proposal))

        if not self.ticket.variants_list:
            self.variant_combo.addItem("Default Proposal", 0)
        else:
            for idx, _v in enumerate(self.ticket.variants_list):
                label_txt = f"Variant {idx + 1}"
                if idx == 0:
                    label_txt += " (Primary)"
                elif idx == 1:
                    label_txt += " (Hook / Angle)"
                else:
                    label_txt += f" (Option {idx + 1})"
                self.variant_combo.addItem(label_txt, idx)

        sel = max(0, min(self.ticket.selected_variant_idx, self.variant_combo.count() - 1))
        self.variant_combo.setCurrentIndex(sel)
        self.variant_combo.blockSignals(False)

    def _on_variant_changed(self, idx: int) -> None:
        self.ticket.selected_variant_idx = idx
        self._load_active_proposal()

    def _add_variant(self) -> None:
        current = self.get_current_proposal()
        new_variant = dict(current)
        # Suffix hook/title to distinguish
        if "title" in new_variant and new_variant["title"]:
            new_variant["title"] = f"{new_variant['title']} (Alt)"
        self.ticket.variants_list.append(new_variant)
        self._populate_variants()
        self.variant_combo.setCurrentIndex(len(self.ticket.variants_list) - 1)

    def _load_active_proposal(self) -> None:
        prop = self.ticket.active_proposal or {}
        for key, ed in self.editors.items():
            val = str(prop.get(key, "") or "").strip()
            if isinstance(ed, QPlainTextEdit):
                ed.setPlainText(val)
            else:
                ed.setText(val)
            self._update_field_count(key)

    def _update_field_count(self, key: str) -> None:
        ed = self.editors[key]
        text = ed.toPlainText().strip() if isinstance(ed, QPlainTextEdit) else ed.text().strip()
        cnt_label = self.counters[key]
        if not text:
            cnt_label.setText("empty · not applied")
            set_prop(cnt_label, "state", "empty")
        else:
            shown, bad = counter_text(key, text)
            cnt_label.setText(shown)
            set_prop(cnt_label, "state", "fail" if bad else "pass")
        self._sync_apply_btn()

    def _sync_apply_btn(self) -> None:
        # Enable apply if there is at least some copy and no over-limits
        has_any = any(
            (ed.toPlainText().strip() if isinstance(ed, QPlainTextEdit) else ed.text().strip())
            for ed in self.editors.values()
        )
        has_fail = any(
            counter_text(k, ed.toPlainText().strip() if isinstance(ed, QPlainTextEdit) else ed.text().strip())[1]
            for k, ed in self.editors.items()
            if (ed.toPlainText().strip() if isinstance(ed, QPlainTextEdit) else ed.text().strip())
        )
        self.apply_btn.setEnabled(has_any and not has_fail)

    def get_current_proposal(self) -> dict[str, str]:
        res: dict[str, str] = {}
        for k, ed in self.editors.items():
            val = ed.toPlainText().strip() if isinstance(ed, QPlainTextEdit) else ed.text().strip()
            if val:
                res[k] = val
        return res

    def _save_ticket_changes(self) -> None:
        """Commits changes directly to the ticket via pipeline or store."""
        copy = self.get_current_proposal()
        self.edited_copy = copy
        self.ticket.applied_copy = dict(copy)
        self.ticket.reviewed = True
        self.ticket.applied = True

        card = self.ticket.card
        base_post = getattr(card, "post", None) or Post(video_row=self.ticket.id)
        raw_tags = copy.get("tags", "")
        tags = [s.strip().lstrip("#") for s in raw_tags.split(",") if s.strip()] if raw_tags else base_post.tags
        from dataclasses import replace
        updated_post = replace(
            base_post,
            title=copy.get("title", base_post.title),
            description=copy.get("description", base_post.description),
            tags=tags,
            tiktok_text=copy.get("tiktok_text", base_post.tiktok_text),
            instagram_text=copy.get("instagram_text", base_post.instagram_text),
            bluesky_text=copy.get("bluesky_text", base_post.bluesky_text),
            threads_text=copy.get("threads_text", base_post.threads_text),
            facebook_text=copy.get("facebook_text", base_post.facebook_text),
            hashtags=copy.get("hashtags", base_post.hashtags),
        )

        try:
            if self.pipeline is not None and hasattr(self.pipeline, "save_draft"):
                self.pipeline.save_draft(updated_post)
            elif self.store is not None and hasattr(self.store, "save_post"):
                self.store.save_post(updated_post)
            self.ticket.card.post = updated_post
            self.ticket.saved = True
            self.footer_note.setText("Changes committed to database. (Never auto-published).")
        except Exception as exc:
            self.footer_note.setText(f"Save error: {exc}")
            set_prop(self.footer_note, "danger", True)

    def _accept_and_apply(self) -> None:
        self.edited_copy = self.get_current_proposal()
        self.ticket.applied_copy = dict(self.edited_copy)
        self.ticket.reviewed = True
        self.ticket.applied = True
        self.accept()

    def reject(self) -> None:
        # Creator opened and inspected the proposal, marking reviewed=True
        self.ticket.reviewed = True
        super().reject()


# ====================================================================== Queue Row Card
class BatchTicketRow(QFrame):
    """Clean card row for one queued ticket in the Batch Review GUI."""

    reviewRequested = Signal(int)
    retryRequested = Signal(int)
    cancelRequested = Signal(int)
    saveRequested = Signal(int)

    def __init__(
        self,
        ticket: BatchTicket,
        thumbs: ThumbnailCache | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.ticket = ticket
        self.thumbs = thumbs
        self.setObjectName("RpQueueItem")
        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumHeight(84)

        if self.thumbs and hasattr(self.thumbs, "loaded"):
            self.thumbs.loaded.connect(self._on_thumb_loaded)

        row_lay = QHBoxLayout(self)
        row_lay.setContentsMargins(12, 10, 12, 10)
        row_lay.setSpacing(14)

        # 1. Thumbnail
        self.thumb_label = QLabel()
        self.thumb_label.setFixedSize(54, 54)
        self.thumb_label.setStyleSheet(f"border-radius: 8px; background: {P.bg_raised}; border: 1px solid {P.divider};")
        self.thumb_label.setAlignment(Qt.AlignCenter)
        self._update_thumb()
        row_lay.addWidget(self.thumb_label)

        # 2. Center info
        info_lay = QVBoxLayout()
        info_lay.setSpacing(4)

        # Video title
        self.title_label = QLabel(self.ticket.title)
        self.title_label.setStyleSheet("font-weight: 700; font-size: 13px;")
        self.title_label.setToolTip(self.ticket.title)
        info_lay.addWidget(self.title_label)

        # Platform logo chips row (visible at all times!)
        chips_lay = QHBoxLayout()
        chips_lay.setSpacing(5)
        for spec in TICKET:
            lg = QLabel()
            lg.setPixmap(platform_pixmap(spec.key, 16))
            lg.setToolTip(f"{platform_label(spec.key)} copy supported")
            chips_lay.addWidget(lg)
        chips_lay.addStretch()
        info_lay.addLayout(chips_lay)

        # Badges row: Status pill, Compliance pill, Review indicator, Persistence indicator
        badge_lay = QHBoxLayout()
        badge_lay.setSpacing(8)

        self.status_pill = QLabel(self.ticket.status_label, objectName="RpPill")
        self.compliance_pill = QLabel("PASS", objectName="RpPill")
        self.review_pill = QLabel("Unreviewed", objectName="RpPill")
        self.persistence_pill = QLabel("In Memory Only", objectName="RpPill")

        badge_lay.addWidget(self.status_pill)
        badge_lay.addWidget(self.compliance_pill)
        badge_lay.addWidget(self.review_pill)
        badge_lay.addWidget(self.persistence_pill)
        badge_lay.addStretch()
        info_lay.addLayout(badge_lay)

        row_lay.addLayout(info_lay, 1)

        # 3. Action buttons
        act_lay = QHBoxLayout()
        act_lay.setSpacing(8)

        self.review_btn = tip(QPushButton("Review"), "Open and review Muse Spark proposal for this video")
        self.review_btn.setObjectName("RpQuiet")
        self.review_btn.clicked.connect(lambda: self.reviewRequested.emit(self.ticket.id))
        act_lay.addWidget(self.review_btn)

        self.retry_btn = tip(QPushButton("Retry"), "Regenerate copy for this video")
        self.retry_btn.setObjectName("RpQuiet")
        self.retry_btn.clicked.connect(lambda: self.retryRequested.emit(self.ticket.id))
        act_lay.addWidget(self.retry_btn)

        self.cancel_btn = tip(QPushButton("Cancel"), "Cancel generating this video")
        self.cancel_btn.setObjectName("RpQuiet")
        self.cancel_btn.clicked.connect(lambda: self.cancelRequested.emit(self.ticket.id))
        act_lay.addWidget(self.cancel_btn)

        self.save_btn = tip(QPushButton("Save"), "Commit applied changes to SQLite ticket")
        self.save_btn.setObjectName("RpQuiet")
        self.save_btn.clicked.connect(lambda: self.saveRequested.emit(self.ticket.id))
        act_lay.addWidget(self.save_btn)

        row_lay.addLayout(act_lay)

        self.refresh()

    def _update_thumb(self) -> None:
        if self.thumbs and hasattr(self.ticket.card, "row") and hasattr(self.ticket.card.row, "item"):
            pix = self.thumbs.thumb(self.ticket.card.row.item, 54, 54, self.devicePixelRatioF())
            if pix and not pix.isNull():
                self.thumb_label.setPixmap(pix)
                return
        self.thumb_label.setPixmap(app_pixmap("library", P.text_muted, 24, self.devicePixelRatioF()))

    def _on_thumb_loaded(self, _key: str) -> None:
        self._update_thumb()

    def refresh(self) -> None:
        """Synchronizes row visual state with BatchTicket model."""
        self.title_label.setText(self.ticket.title)

        # Status pill
        status = self.ticket.job.status
        self.status_pill.setText(self.ticket.status_label)
        set_prop(self.status_pill, "state", status)

        # Compliance pill (visible when proposal exists)
        comp = self.ticket.compliance_status
        if comp:
            self.compliance_pill.setVisible(True)
            self.compliance_pill.setText(comp)
            set_prop(self.compliance_pill, "state", "pass" if comp == "PASS" else "fail")
        else:
            self.compliance_pill.setVisible(False)

        # Review indicator
        if self.ticket.reviewed:
            self.review_pill.setText("Reviewed")
            set_prop(self.review_pill, "state", "reviewed")
        else:
            self.review_pill.setText("Unreviewed")
            set_prop(self.review_pill, "state", "unreviewed")

        # Persistence indicator
        if self.ticket.saved:
            self.persistence_pill.setText("Saved")
            set_prop(self.persistence_pill, "state", "saved")
        else:
            self.persistence_pill.setText("In Memory Only")
            set_prop(self.persistence_pill, "state", "memory")

        # Buttons visibility
        has_proposal = bool(self.ticket.job.proposal or self.ticket.applied_copy)
        self.review_btn.setEnabled(has_proposal or status in (JOB_STATUS_READY, JOB_STATUS_APPLIED, JOB_STATUS_SAVED))
        set_prop(self.review_btn, "primary", status == JOB_STATUS_READY and not self.ticket.reviewed)

        self.retry_btn.setVisible(status in (JOB_STATUS_FAILED, JOB_STATUS_CANCELLED))
        self.cancel_btn.setVisible(status in (JOB_STATUS_QUEUED, JOB_STATUS_RUNNING))
        self.save_btn.setVisible(self.ticket.applied and not self.ticket.saved)

        # Error tooltip if failed
        if status == JOB_STATUS_FAILED and self.ticket.job.error:
            self.status_pill.setToolTip(f"Generation error: {self.ticket.job.error}")
        else:
            self.status_pill.setToolTip("")


# ====================================================================== Main Batch Dialog
class BatchReviewDialog(QDialog):
    """Batch Review GUI for Reelay (Post-hackathon product work, branch batch-v2).

    Empowers video creators to repurpose batches of videos with Meta Muse Spark:
    - Creator header & subtext
    - Technical disclosure pop-over overlay
    - Explicit Batch Opt-in checkbox gate
    - Progress bar and batch status label
    - Queue / Ticket List View with 6 platform chips visible at all times
    - PASS / OVER LIMIT compliance pills
    - Review indicator & Persistence indicator
    - Proposal review with variant switcher
    - Batch action: "Apply Reviewed Only" (NEVER applies unreviewed tickets!)
    - Save Changes step (commits in-memory fields to ticket; never auto-publishes)
    """

    def __init__(
        self,
        cards: list[Any] | None = None,
        store: Any = None,
        pipeline: Any = None,
        thumbs: ThumbnailCache | None = None,
        parent: QWidget | None = None,
        *,
        generate_fn: Callable[..., dict[str, str]] | None = None,
        variant_generator: Callable[..., Any] | None = None,
        brand_voice: str = "",
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.pipeline = pipeline
        self.thumbs = thumbs
        self.generate_fn = generate_fn or generate_repurpose_proposal
        self.variant_generator = variant_generator
        self.brand_voice = brand_voice.strip()

        # Build BatchTicket list from passed cards
        self.cards = list(cards) if cards else []
        self.tickets: list[BatchTicket] = []
        self.tickets_by_id: dict[int, BatchTicket] = {}
        for c in self.cards:
            cid = getattr(c, "id", None)
            if cid is None and hasattr(c, "row"):
                cid = getattr(c.row, "id", 0)
            cid = int(cid or 0)
            snap = extract_source_snapshot(c, store=self.store, brand_voice=self.brand_voice)
            job = BatchJob.from_ticket(cid, c, source_snapshot=snap)
            ticket = BatchTicket(card=c, job=job)
            self.tickets.append(ticket)
            self.tickets_by_id[cid] = ticket

        self._worker: BatchWorker | None = None

        self.setWindowTitle("Repurpose Batch with Muse Spark")
        self.setMinimumSize(920, 720)
        self.resize(960, 780)

        # Style using Reelay theme palette
        self.setStyleSheet(f"""
            QDialog {{ background: {P.bg_primary}; color: {P.text_primary}; }}
            QFrame#RpCard {{ background: {P.bg_surface}; border: 1px solid {P.divider}; border-radius: 14px; }}
            QFrame#RpQueueItem {{ background: {P.bg_surface}; border: 1px solid {P.divider}; border-radius: 12px; }}
            QFrame#RpQueueItem:hover {{ border-color: {P.border}; }}
            QFrame#RpBanner {{ background: {P.accent_soft}; border: 1px solid {P.accent_ring}; border-radius: 12px; }}
            QFrame#RpOverlay {{ background: {P.bg_surface}; border: 1px solid {P.accent_ring}; border-radius: 16px; }}
            QLabel#RpTitle {{ font-size: {BASE_PT + 9}pt; font-weight: 800; }}
            QLabel#RpVideo {{ font-size: {BASE_PT + 2}pt; font-weight: 600; color: {P.text_secondary}; }}
            QLabel#RpSection {{ font-size: {BASE_PT + 3}pt; font-weight: 700; }}
            QLabel#RpPill {{ border-radius: 11px; padding: 3px 10px; font-weight: 700;
                             background: {P.bg_hover}; color: {P.text_secondary}; font-size: {BASE_PT}pt; }}
            QLabel#RpPill[state="pass"] {{ background: {P.success_soft}; color: {P.success_ink}; }}
            QLabel#RpPill[state="fail"] {{ background: {P.danger_soft}; color: {P.danger_ink}; }}
            QLabel#RpPill[state="queued"] {{ background: {P.bg_hover}; color: {P.text_muted}; }}
            QLabel#RpPill[state="running"] {{ background: {P.accent_soft}; color: {P.accent_color}; }}
            QLabel#RpPill[state="ready"] {{ background: {P.accent_soft}; color: {P.text_primary}; }}
            QLabel#RpPill[state="applied"] {{ background: {P.bg_hover}; color: {P.text_primary}; border: 1px solid {P.accent_ring}; }}
            QLabel#RpPill[state="saved"] {{ background: {P.success_soft}; color: {P.success_ink}; }}
            QLabel#RpPill[state="cancelled"] {{ background: {P.bg_hover}; color: {P.text_muted}; }}
            QLabel#RpPill[state="reviewed"] {{ background: {P.success_soft}; color: {P.success_ink}; }}
            QLabel#RpPill[state="unreviewed"] {{ background: {P.bg_hover}; color: {P.text_muted}; }}
            QLabel#RpPill[state="memory"] {{ background: {P.bg_hover}; color: {P.text_secondary}; }}
            QPushButton#RpQuiet {{ background: transparent; border: 1px solid {P.divider}; color: {P.text_secondary};
                                   border-radius: 10px; padding: 7px 15px; font-weight: 600; }}
            QPushButton#RpQuiet:hover {{ color: {P.text_primary}; border-color: {P.border}; }}
            QPushButton[primary="true"] {{ border-radius: 10px; padding: 8px 18px; font-weight: 700; }}
            QProgressBar {{ border: 1px solid {P.divider}; border-radius: 6px; background: {P.bg_surface};
                            text-align: center; font-weight: 700; color: {P.text_primary}; }}
            QProgressBar::chunk {{ background: {P.accent_color}; border-radius: 5px; }}
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header area (creator framing)
        header_frame = QFrame(objectName="RpCard")
        header_frame.setStyleSheet(f"QFrame#RpCard {{ border-radius: 0; border-left: none; border-right: none; "
                                   f"border-top: none; background: {P.bg_primary}; }}")
        hl = QVBoxLayout(header_frame)
        hl.setContentsMargins(24, 18, 24, 14)
        hl.setSpacing(8)

        # Creator title & subtext
        n_tickets = len(self.tickets)
        title_text = f"Repurpose these videos ({n_tickets} video{'s' if n_tickets != 1 else ''})"
        hl.addWidget(label(title_text, name="RpTitle"))
        hl.addWidget(label("One batch, six platforms each, you approve before saving.", prop="muted"))

        # Privacy banner
        banner = QFrame(objectName="RpBanner")
        bl = QHBoxLayout(banner)
        bl.setContentsMargins(14, 10, 10, 10)
        bl.setSpacing(10)
        lock = QLabel()
        lock.setPixmap(app_icon("sparkles", 18).pixmap(18, 18))
        bl.addWidget(lock)
        bl.addWidget(label("<b>Your video text stays private until you approve it.</b>", rich=True), 1)
        self.info_btn = tip(QPushButton("ⓘ  How it works"), "What is sent, where it goes, and what stays private")
        self.info_btn.setObjectName("RpQuiet")
        self.info_btn.clicked.connect(self._toggle_disclosure)
        bl.addWidget(self.info_btn)
        hl.addWidget(banner)

        # Explicit Batch Opt-in (starts unchecked by default!)
        consent = QFrame(objectName="RpCard")
        cl = QVBoxLayout(consent)
        cl.setContentsMargins(16, 10, 16, 10)
        cl.setSpacing(2)
        self.opt_in_box = tip(
            QCheckBox("Allow Muse Spark to draft copy for this batch"),
            "Lets Reelay send text for these videos to Meta's Muse Spark when you click Generate Batch. "
            "Starts unticked every time.",
        )
        self.opt_in_box.setStyleSheet("QCheckBox { font-weight: 700; }")
        self.opt_in_box.setChecked(False)
        self.opt_in_box.toggled.connect(self._on_opt_in_toggled)
        cl.addWidget(self.opt_in_box)
        sub = label("You approve before anything is saved.", prop="faint")
        sub.setContentsMargins(26, 0, 0, 0)
        cl.addWidget(sub)
        hl.addWidget(consent)

        # Batch actions & progress row
        prog_row = QHBoxLayout()
        prog_row.setSpacing(12)
        self.batch_status_label = label("Ready to generate. Tick opt-in above and click Generate Batch.", prop="muted")
        prog_row.addWidget(self.batch_status_label, 1)

        self.cancel_batch_btn = tip(QPushButton("Cancel Batch"), "Halt running batch generation")
        self.cancel_batch_btn.setObjectName("RpQuiet")
        self.cancel_batch_btn.setEnabled(False)
        self.cancel_batch_btn.clicked.connect(self.cancel_batch)
        prog_row.addWidget(self.cancel_batch_btn)

        self.generate_batch_btn = tip(QPushButton("Generate Batch"), "Start batch copy generation via Meta Muse Spark")
        self.generate_batch_btn.setIcon(app_icon("sparkles", 16, P.on_accent))
        set_prop(self.generate_batch_btn, "primary", True)
        self.generate_batch_btn.clicked.connect(self.start_batch)
        prog_row.addWidget(self.generate_batch_btn)

        hl.addLayout(prog_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, max(1, len(self.tickets)))
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        hl.addWidget(self.progress_bar)

        outer.addWidget(header_frame)

        # Queue List Scroll Area
        self.scroll, _page, self.list_layout = scroll_page(margins=(24, 14, 24, 14), spacing=10, sideways=False)
        outer.addWidget(self.scroll, 1)

        self.row_widgets: dict[int, BatchTicketRow] = {}
        for ticket in self.tickets:
            row_w = BatchTicketRow(ticket, thumbs=self.thumbs, parent=self)
            row_w.reviewRequested.connect(self.open_ticket_review)
            row_w.retryRequested.connect(self.retry_ticket)
            row_w.cancelRequested.connect(self.cancel_ticket)
            row_w.saveRequested.connect(self.save_single_ticket)
            self.row_widgets[ticket.id] = row_w
            self.list_layout.addWidget(row_w)

        self.list_layout.addStretch()

        # Footer
        footer = QFrame(objectName="RpCard")
        footer.setStyleSheet(f"QFrame#RpCard {{ border-radius: 0; border-left: none; border-right: none; "
                             f"border-bottom: none; background: {P.bg_primary}; }}")
        bottom = QHBoxLayout(footer)
        bottom.setContentsMargins(24, 14, 24, 14)
        bottom.setSpacing(12)

        self.footer_note = label(
            "Apply populates in-memory fields only. Save commits changes to ticket. "
            "Reelay never auto-publishes or auto-posts.",
            prop="faint",
        )
        bottom.addWidget(self.footer_note, 1)

        self.apply_reviewed_btn = tip(
            QPushButton("Apply Reviewed Only (0 of 0)"),
            "Apply proposals only for videos you have opened and reviewed. Never applies unreviewed tickets.",
        )
        self.apply_reviewed_btn.setObjectName("RpQuiet")
        self.apply_reviewed_btn.clicked.connect(self.apply_reviewed_only)
        bottom.addWidget(self.apply_reviewed_btn)

        self.save_applied_btn = tip(
            QPushButton("Save Changes"),
            "Commit applied copy changes to ticket records in database",
        )
        set_prop(self.save_applied_btn, "primary", True)
        self.save_applied_btn.clicked.connect(self.save_all_applied)
        bottom.addWidget(self.save_applied_btn)

        self.close_btn = QPushButton("Close")
        self.close_btn.setObjectName("RpQuiet")
        self.close_btn.clicked.connect(self.accept)
        bottom.addWidget(self.close_btn)

        outer.addWidget(footer)

        # Technical disclosure overlay
        self.disclosure_backdrop = QFrame(self)
        self.disclosure_backdrop.setStyleSheet("background: rgba(0, 0, 0, 150);")
        self.disclosure_backdrop.hide()
        self.disclosure_overlay = QFrame(self, objectName="RpOverlay")
        ol = QVBoxLayout(self.disclosure_overlay)
        ol.setContentsMargins(20, 16, 20, 16)
        ol.setSpacing(10)
        orow = QHBoxLayout()
        orow.addWidget(label("How Muse Spark drafts work", name="RpSection"), 1)
        close = QPushButton("Got it")
        set_prop(close, "primary", True)
        close.clicked.connect(self._toggle_disclosure)
        orow.addWidget(close)
        ol.addLayout(orow)
        self.disclosure_label = label(DISCLOSURE_TEXT, prop="muted")
        self.disclosure_label.setWordWrap(True)
        ol.addWidget(self.disclosure_label)
        shadow(self.disclosure_overlay, blur=48, alpha=170, dy=12)
        self.disclosure_overlay.hide()

        self._update_batch_buttons()

    # ------------------------------------------------------------------ Overlay & UI
    def _toggle_disclosure(self) -> None:
        o = self.disclosure_overlay
        if o.isVisible():
            o.hide()
            self.disclosure_backdrop.hide()
            return
        self._place_overlay()
        self.disclosure_backdrop.show()
        self.disclosure_backdrop.raise_()
        o.show()
        o.raise_()

    def _place_overlay(self) -> None:
        o = self.disclosure_overlay
        w = min(self.width() - 48, 600)
        o.setFixedWidth(w)
        o.adjustSize()
        o.move((self.width() - w) // 2, 70)
        self.disclosure_backdrop.setGeometry(0, 0, self.width(), self.height())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if getattr(self, "disclosure_overlay", None) is not None and self.disclosure_overlay.isVisible():
            self._place_overlay()

    def _on_opt_in_toggled(self, checked: bool) -> None:
        if self.store is not None:
            try:
                set_cloud_ai_enabled(self.store, checked)
            except Exception:
                pass
        if checked:
            set_prop(self.batch_status_label, "danger", False)
            if self.batch_status_label.text().startswith("Cloud AI is disabled"):
                self.batch_status_label.setText("Ready to start batch.")

    # ------------------------------------------------------------------ Batch Execution
    def start_batch(self) -> None:
        """Starts batch generation for queued tickets."""
        if not self.opt_in_box.isChecked():
            self.batch_status_label.setText(
                "Cloud AI is disabled. Tick 'Allow Muse Spark to draft copy for this batch' first."
            )
            set_prop(self.batch_status_label, "danger", True)
            return

        if self._worker is not None and self._worker.isRunning():
            return

        set_prop(self.batch_status_label, "danger", False)
        queued_tickets = [
            t for t in self.tickets
            if t.job.status in (JOB_STATUS_QUEUED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED)
        ]
        if not queued_tickets:
            self.batch_status_label.setText("All tickets are already generated. Review and apply below.")
            return

        self.progress_bar.setRange(0, len(self.tickets))
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self.cancel_batch_btn.setEnabled(True)
        self.generate_batch_btn.setEnabled(False)
        self.batch_status_label.setText(f"Starting batch ({len(queued_tickets)} videos to generate)…")

        worker = BatchWorker(
            queued_tickets,
            generate_fn=self.generate_fn,
            variant_generator=self.variant_generator,
            store=self.store,
            parent=self,
        )
        worker.ticketStarted.connect(self._on_worker_ticket_started)
        worker.ticketSucceeded.connect(self._on_worker_ticket_succeeded)
        worker.ticketFailed.connect(self._on_worker_ticket_failed)
        worker.progressUpdated.connect(self._on_worker_progress)
        worker.batchFinished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()

    def cancel_batch(self) -> None:
        """Cancels running batch generation."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self.batch_status_label.setText("Cancelling batch…")

    def _on_worker_ticket_started(self, ticket_id: int) -> None:
        t = self.tickets_by_id.get(ticket_id)
        if t:
            t.job.mark_running()
            if ticket_id in self.row_widgets:
                self.row_widgets[ticket_id].refresh()

    def _on_worker_ticket_succeeded(self, ticket_id: int, proposal: dict[str, str], variants: Any) -> None:
        t = self.tickets_by_id.get(ticket_id)
        if t:
            t.job.mark_ready(proposal, variants=variants if isinstance(variants, dict) else None)
            t.variants_list = [dict(proposal)]
            if isinstance(variants, list):
                for v in variants:
                    if isinstance(v, dict):
                        t.variants_list.append(dict(v))
            if ticket_id in self.row_widgets:
                self.row_widgets[ticket_id].refresh()
        self._update_batch_buttons()

    def _on_worker_ticket_failed(self, ticket_id: int, error_msg: str) -> None:
        t = self.tickets_by_id.get(ticket_id)
        if t:
            t.job.mark_failed(error_msg)
            if ticket_id in self.row_widgets:
                self.row_widgets[ticket_id].refresh()
        self._update_batch_buttons()

    def _on_worker_progress(self, completed: int, total: int, ready: int, failed: int) -> None:
        self.progress_bar.setValue(completed)
        self.batch_status_label.setText(
            f"Generating {completed} of {total}… · {ready} ready · {failed} failed"
        )

    def _on_worker_finished(self, ready: int, failed: int, cancelled: bool) -> None:
        self.cancel_batch_btn.setEnabled(False)
        self.generate_batch_btn.setEnabled(True)
        if cancelled:
            # Mark remaining queued tickets as cancelled
            for t in self.tickets:
                if t.job.status in (JOB_STATUS_QUEUED, JOB_STATUS_RUNNING):
                    t.job.mark_cancelled()
                    if t.id in self.row_widgets:
                        self.row_widgets[t.id].refresh()
            self.batch_status_label.setText(f"Batch cancelled · {ready} ready · {failed} failed")
        else:
            self.batch_status_label.setText(f"Batch complete · {ready} ready · {failed} failed")
        self._update_batch_buttons()

    # ------------------------------------------------------------------ Per-ticket actions
    def open_ticket_review(self, ticket_id: int) -> None:
        """Opens proposal review dialog for a specific ticket."""
        ticket = self.tickets_by_id.get(ticket_id)
        if not ticket:
            return

        # Opening review marks as reviewed
        ticket.reviewed = True

        dlg = TicketProposalReviewDialog(
            ticket,
            store=self.store,
            pipeline=self.pipeline,
            parent=self,
            generate_fn=self.generate_fn,
        )
        if dlg.exec() == QDialog.Accepted:
            ticket.applied = True
            ticket.applied_copy = dlg.edited_copy
            ticket.job.status = JOB_STATUS_APPLIED

        if ticket_id in self.row_widgets:
            self.row_widgets[ticket_id].refresh()
        self._update_batch_buttons()

    def retry_ticket(self, ticket_id: int) -> None:
        """Retries copy generation for a failed or cancelled ticket."""
        if not self.opt_in_box.isChecked():
            self.batch_status_label.setText(
                "Cloud AI is disabled. Tick 'Allow Muse Spark to draft copy for this batch' first."
            )
            set_prop(self.batch_status_label, "danger", True)
            return

        ticket = self.tickets_by_id.get(ticket_id)
        if not ticket:
            return

        ticket.job.status = JOB_STATUS_QUEUED
        if ticket_id in self.row_widgets:
            self.row_widgets[ticket_id].refresh()

        worker = BatchWorker(
            [ticket],
            generate_fn=self.generate_fn,
            variant_generator=self.variant_generator,
            store=self.store,
            parent=self,
        )
        worker.ticketStarted.connect(self._on_worker_ticket_started)
        worker.ticketSucceeded.connect(self._on_worker_ticket_succeeded)
        worker.ticketFailed.connect(self._on_worker_ticket_failed)
        worker.batchFinished.connect(lambda _r, _f, _c: self._update_batch_buttons())
        worker.start()

    def cancel_ticket(self, ticket_id: int) -> None:
        """Cancels a specific ticket's queued or running generation."""
        ticket = self.tickets_by_id.get(ticket_id)
        if ticket and ticket.job.status in (JOB_STATUS_QUEUED, JOB_STATUS_RUNNING):
            ticket.job.mark_cancelled()
            if ticket_id in self.row_widgets:
                self.row_widgets[ticket_id].refresh()
            self._update_batch_buttons()

    def save_single_ticket(self, ticket_id: int) -> bool:
        """Commits changes for one applied ticket to database."""
        ticket = self.tickets_by_id.get(ticket_id)
        if not ticket or not ticket.applied:
            return False

        copy = ticket.active_proposal or {}
        card = ticket.card
        base_post = getattr(card, "post", None) or Post(video_row=ticket.id)
        raw_tags = copy.get("tags", "")
        tags = [s.strip().lstrip("#") for s in raw_tags.split(",") if s.strip()] if raw_tags else base_post.tags

        from dataclasses import replace
        updated_post = replace(
            base_post,
            title=copy.get("title", base_post.title),
            description=copy.get("description", base_post.description),
            tags=tags,
            tiktok_text=copy.get("tiktok_text", base_post.tiktok_text),
            instagram_text=copy.get("instagram_text", base_post.instagram_text),
            bluesky_text=copy.get("bluesky_text", base_post.bluesky_text),
            threads_text=copy.get("threads_text", base_post.threads_text),
            facebook_text=copy.get("facebook_text", base_post.facebook_text),
            hashtags=copy.get("hashtags", base_post.hashtags),
        )

        try:
            if self.pipeline is not None and hasattr(self.pipeline, "save_draft"):
                self.pipeline.save_draft(updated_post)
            elif self.store is not None and hasattr(self.store, "save_post"):
                self.store.save_post(updated_post)
            ticket.card.post = updated_post
            ticket.saved = True
            ticket.job.status = JOB_STATUS_SAVED
            if ticket_id in self.row_widgets:
                self.row_widgets[ticket_id].refresh()
            self._update_batch_buttons()
            return True
        except Exception as exc:
            log.warning("Save failed for ticket %s: %s", ticket.id, exc)
            self.footer_note.setText(f"Save failed for ticket {ticket.id}: {exc}")
            set_prop(self.footer_note, "danger", True)
            return False

    # ------------------------------------------------------------------ Batch Actions
    def apply_reviewed_only(self) -> None:
        """CRITICAL SAFETY REQUIREMENT: Applies proposals ONLY for reviewed tickets.

        Never touches or applies any ticket where reviewed == False!
        """
        count_applied = 0
        for t in self.tickets:
            # Strictly require that creator opened and reviewed the ticket!
            if t.reviewed and (t.job.proposal or t.active_proposal) and not t.applied:
                t.applied = True
                t.applied_copy = dict(t.active_proposal or {})
                t.job.status = JOB_STATUS_APPLIED
                count_applied += 1
                if t.id in self.row_widgets:
                    self.row_widgets[t.id].refresh()

        self._update_batch_buttons()
        if count_applied > 0:
            self.footer_note.setText(
                f"Applied copy for {count_applied} reviewed video{'s' if count_applied != 1 else ''}. "
                "In memory only; click Save Changes to commit."
            )
            set_prop(self.footer_note, "danger", False)

    def save_all_applied(self) -> None:
        """Commits changes for all applied, unsaved tickets to database."""
        to_save = [t for t in self.tickets if t.applied and not t.saved]
        saved_count = 0
        for t in to_save:
            if self.save_single_ticket(t.id):
                saved_count += 1

        self._update_batch_buttons()
        self.footer_note.setText(
            f"Saved {saved_count} ticket{'s' if saved_count != 1 else ''} to database. "
            "(Never auto-published or auto-posted)."
        )
        set_prop(self.footer_note, "danger", False)

    def _update_batch_buttons(self) -> None:
        total = len(self.tickets)
        reviewed_unapplied = [t for t in self.tickets if t.reviewed and (t.job.proposal or t.active_proposal) and not t.applied]
        self.apply_reviewed_btn.setText(f"Apply Reviewed Only ({len(reviewed_unapplied)} of {total})")
        self.apply_reviewed_btn.setEnabled(len(reviewed_unapplied) > 0)

        applied_unsaved = [t for t in self.tickets if t.applied and not t.saved]
        self.save_applied_btn.setText(
            f"Save Changes ({len(applied_unsaved)} ticket{'s' if len(applied_unsaved) != 1 else ''})"
            if applied_unsaved else "Save Changes"
        )
        self.save_applied_btn.setEnabled(len(applied_unsaved) > 0)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(1000)
        super().closeEvent(event)
