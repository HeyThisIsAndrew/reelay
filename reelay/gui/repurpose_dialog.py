"""Repurpose Engine GUI dialog: review and approve Meta Muse Spark copy for the ticket's chosen platforms."""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import assist
from ..assist import AssistError
from ..models import Platform
from ..platform_specs import FIELD_LIMITS, SPECS, TICKET, count
from ..platform_specs import label as platform_label
from ..privacy import (
    DISCLOSURE_TEXT,
    PrivacyOptInRequiredError,
    set_cloud_ai_enabled,
)
from ..repurpose_engine import (
    RepurposeError,
    generate_repurpose_proposal,
)
from .logos import platform_icon
from .theme import set_prop
from .tips import tip
from .widgets import label, scroll_page
from .workers import CallWorker

# Short field names (the platform is the section header) and the unit each count is in.
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
EDITOR_HEIGHT = {"description": 150, "facebook_text": 110, "tiktok_text": 110, "instagram_text": 110}
# Destinations whose posts use the shared hashtag set (Threads keeps hashtags out of its body).
HASHTAG_PLATFORMS = {Platform.TIKTOK, Platform.INSTAGRAM, Platform.FACEBOOK}
SHARED_KEY = "_shared"

APPLY_NOTE = "Updates the ticket editor only. Click Save Changes to save."


def counter_text(key: str, text: str) -> tuple[str, bool]:
    """'74/100 characters · PASS' and whether it fails (over the limit, or # in Threads)."""
    if key == "hashtags":
        n = len(text.split())
        return f"{n} hashtag{'s' if n != 1 else ''} · PASS", False
    lim = FIELD_LIMITS[key]
    used = lim.measure(text)
    _, over = count(key, text)
    shown = f"{used:,}/{lim.limit:,} {FIELD_UNITS[key]}"
    if key == "threads_text" and "#" in text:
        return f"{shown} · ⚠ remove # (Threads posts keep hashtags out of the text)", True
    return f"{shown} · {'⚠ OVER LIMIT' if over else 'PASS'}", over


class RepurposeDialog(QDialog):
    """Modal proposal review dialog for Meta Muse Spark (muse-spark-1.3).

    Shows the disclosure and a read-only source snapshot, generates in a background thread
    without blocking close/cancel, and shows results grouped by the platforms ticked in the
    ticket's "Posting to" row. Only those platforms' non-empty fields are offered for Apply;
    everything else in the ticket is left exactly as it was.
    """

    _in_flight_workers: set[CallWorker] = set()

    def __init__(
        self,
        card: Any = None,
        store: Any = None,
        parent=None,
        *,
        title: str = "",
        description: str = "",
        notes: str = "",
        transcript: str = "",
        brand_voice: str = "",
        generate_fn=None,
        platforms: list[str] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Repurpose with Muse Spark")
        self.card = card
        self.store = store
        self.proposal: dict[str, str] | None = None
        self.worker: CallWorker | None = None
        self._busy: bool = False
        self._closed: bool = False
        self._has_result: bool = False
        self._active_request_id: int = 0
        self._generate_fn = generate_fn or generate_repurpose_proposal

        known = [spec.key for spec in TICKET]
        chosen = known if platforms is None else [p for p in known if p in set(platforms)]
        self.platforms: tuple[str, ...] = tuple(chosen)          # captured for this review session
        self.selected_fields: tuple[str, ...] = tuple(
            f for p in self.platforms for f in SPECS[p].copy_fields
        ) + (("hashtags",) if HASHTAG_PLATFORMS & set(self.platforms) else ())

        row = getattr(card, "row", None) if card is not None else None
        post = getattr(card, "post", None) if card is not None else None
        item = getattr(row, "item", None) if row is not None else None
        ticket = getattr(row, "ticket", None) if row is not None else None

        self.source_title = (
            title.strip()
            or (getattr(post, "title", "") or "").strip()
            or (getattr(item, "title", "") or "").strip()
        )
        self.source_description = (
            description.strip()
            or (getattr(post, "description", "") or "").strip()
            or (getattr(item, "description", "") or "").strip()
        )
        self.source_transcript = (
            transcript.strip()
            or (getattr(ticket, "transcript", "") or "").strip()
        )
        if notes.strip():
            self.source_notes = notes.strip()
        else:
            parts = [
                p.strip()
                for p in (
                    getattr(ticket, "topic", "") or "",
                    getattr(ticket, "keywords", "") or "",
                )
                if p and p.strip()
            ]
            self.source_notes = " · ".join(parts)

        if brand_voice.strip():
            self.brand_voice = brand_voice.strip()
        elif store is not None and row is not None and hasattr(store, "active_brand_profile"):
            brand = store.active_brand_profile(row.profile_id)
            self.brand_voice = assist.system_prompt(brand) if brand else ""
        else:
            self.brand_voice = ""

        self._source_snapshot: dict[str, str] = {
            "title": self.source_title,
            "description": self.source_description,
            "notes": self.source_notes,
            "transcript": self.source_transcript,
            "brand_voice": self.brand_voice,
        }

        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QGraphicsDropShadowEffect, QToolButton

        from . import theme as _theme
        from .icons import icon as _icon
        from .logos import platform_pixmap

        pal = _theme.P
        self._pal = pal
        self.setMinimumSize(800, 600)
        self.resize(820, 880)
        # Dialog-scoped styling built only from the app's own theme tokens (no new colours).
        self.setStyleSheet(f"""
            QFrame#RpCard {{ background: {pal.bg_surface}; border: 1px solid {pal.divider}; border-radius: 14px; }}
            QFrame#RpField {{ background: {pal.bg_raised}; border: 1px solid {pal.divider}; border-radius: 12px; }}
            QFrame#RpBanner {{ background: {pal.accent_soft}; border: 1px solid {pal.accent_ring}; border-radius: 12px; }}
            QFrame#RpOverlay {{ background: {pal.bg_surface}; border: 1px solid {pal.accent_ring}; border-radius: 16px; }}
            QFrame#RpEmpty {{ background: {pal.bg_surface}; border: 1px dashed {pal.border}; border-radius: 14px; }}
            QLabel#RpTitle {{ font-size: {_theme.BASE_PT + 10}pt; font-weight: 800; }}
            QLabel#RpVideo {{ font-size: {_theme.BASE_PT + 2}pt; font-weight: 600; color: {pal.text_secondary}; }}
            QLabel#RpSection {{ font-size: {_theme.BASE_PT + 3}pt; font-weight: 700; }}
            QLabel#RpFieldName {{ font-size: {_theme.BASE_PT + 1}pt; font-weight: 700; }}
            QLabel#RpPill {{ border-radius: 11px; padding: 3px 10px; font-weight: 700;
                             background: {pal.bg_hover}; color: {pal.text_secondary}; }}
            QLabel#RpPill[state="pass"] {{ background: {pal.success_soft}; color: {pal.success_ink}; }}
            QLabel#RpPill[state="fail"] {{ background: {pal.danger_soft}; color: {pal.danger_ink}; }}
            QFrame#RpField QPlainTextEdit, QFrame#RpField QLineEdit {{
                font-size: {_theme.BASE_PT + 1}pt; padding: 10px 12px; border-radius: 10px;
                background: {pal.bg_surface}; border: 1px solid {pal.border}; }}
            QFrame#RpField QPlainTextEdit:focus, QFrame#RpField QLineEdit:focus {{
                border: 2px solid {pal.accent_color}; }}
            QToolButton#RpChip {{ border: 1px solid {pal.divider}; border-radius: 16px; padding: 5px 9px;
                                  background: transparent; color: {pal.text_muted}; font-weight: 600; }}
            QToolButton#RpChip:enabled {{ color: {pal.text_secondary}; border-color: {pal.border}; }}
            QToolButton#RpChip:checked {{ background: {pal.accent_soft}; color: {pal.text_primary};
                                          border: 2px solid {pal.accent_ring}; }}
            QPushButton#RpQuiet {{ background: transparent; border: 1px solid {pal.divider}; color: {pal.text_secondary};
                                   border-radius: 10px; padding: 8px 16px; }}
            QPushButton#RpQuiet:hover {{ color: {pal.text_primary}; border-color: {pal.border}; }}
            QPushButton[primary="true"] {{ border-radius: 10px; padding: 9px 20px; font-weight: 700; }}
        """)

        def shadow(widget, blur=28, alpha=110, dy=6):
            effect = QGraphicsDropShadowEffect(widget)
            effect.setBlurRadius(blur)
            effect.setOffset(0, dy)
            from PySide6.QtGui import QColor
            effect.setColor(QColor(0, 0, 0, alpha))
            widget.setGraphicsEffect(effect)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Everything above the footer scrolls together, so nothing is ever cut off.
        self.scroll, _page, lay = scroll_page(margins=(24, 20, 24, 12), spacing=12, sideways=False)
        outer.addWidget(self.scroll, 1)
        self.results_scroll = self.scroll

        # ---- Creator header
        head = label("Repurpose this video", name="RpTitle")
        lay.addWidget(head)
        video = label(self.source_title or "Untitled video", name="RpVideo")
        lay.addWidget(video)
        lay.addWidget(label("One video, six platforms, you approve before saving.", prop="muted"))

        # ---- Privacy banner (details in a pop-over)
        banner = QFrame(objectName="RpBanner")
        bl = QHBoxLayout(banner)
        bl.setContentsMargins(14, 10, 10, 10)
        bl.setSpacing(10)
        lock = QLabel()
        lock.setPixmap(_icon("sparkles", 18).pixmap(18, 18))
        bl.addWidget(lock)
        bl.addWidget(label("<b>Your video text stays private until you approve it.</b>", rich=True), 1)
        self.info_btn = tip(QPushButton("ⓘ  How it works"), "What is sent, where it goes, and what stays private")
        self.info_btn.setObjectName("RpQuiet")
        self.info_btn.clicked.connect(self._toggle_disclosure)
        bl.addWidget(self.info_btn)
        lay.addWidget(banner)

        # ---- Consent (plain language; same gate underneath)
        consent = QFrame(objectName="RpCard")
        cl = QVBoxLayout(consent)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.setSpacing(2)
        self.opt_in_box = tip(
            QCheckBox("Allow Muse Spark to draft copy for this video"),
            "Lets Reelay send this video's text (shown below) to Meta's Muse Spark when you click Generate. "
            "Starts unticked every time.",
        )
        self.opt_in_box.setStyleSheet("QCheckBox { font-weight: 700; }")
        self.opt_in_box.setChecked(False)
        self.opt_in_box.toggled.connect(self._on_opt_in_toggled)
        cl.addWidget(self.opt_in_box)
        sub = label("You approve before anything is saved.", prop="faint")
        sub.setContentsMargins(26, 0, 0, 0)
        cl.addWidget(sub)
        lay.addWidget(consent)

        # ---- Source snapshot (expandable, wraps, never clipped sideways)
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
        sl.addWidget(label("This is the text Muse Spark will read when you click Generate Proposal.", prop="faint"))
        self.source_preview = QPlainTextEdit()
        self.source_preview.setReadOnly(True)
        self.source_preview.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.source_preview.setStyleSheet("QPlainTextEdit { min-height: 100px; }")
        self.source_preview.setFixedHeight(120)
        self.source_preview.setPlainText(self._format_source_snapshot(self._source_snapshot))
        tip(self.source_preview, "Title, description, notes, transcript, and brand voice guidance for this ticket")
        sl.addWidget(self.source_preview)
        lay.addWidget(source_card)

        # ---- Destinations + generate
        if self.platforms:
            names = ", ".join(platform_label(p) for p in self.platforms)
            dest_text = (f"<b>Drafting for:</b> {names} <span>(from Posting to)</span>. Muse Spark writes copy for "
                         "every platform; only these are shown and applied.")
        else:
            dest_text = ("<b>No destinations selected.</b> Tick at least one platform under Posting to, "
                         "then open Repurpose again.")
        self.destinations_label = label(dest_text, rich=True, prop="muted")
        lay.addWidget(self.destinations_label)

        action_row = QHBoxLayout()
        action_row.setSpacing(12)
        self.status = label("", prop="muted")
        action_row.addWidget(self.status, 1)
        self.run_btn = tip(QPushButton("Generate Proposal"), "Ask Meta Muse Spark for a copy proposal")
        self.run_btn.setIcon(_icon("sparkles", 16, pal.on_accent))
        set_prop(self.run_btn, "primary")
        self.run_btn.clicked.connect(self._run)
        action_row.addWidget(self.run_btn)
        lay.addLayout(action_row)
        self.busy_bar = QProgressBar()
        self.busy_bar.setRange(0, 0)            # indeterminate while a request is running
        self.busy_bar.setTextVisible(False)
        self.busy_bar.setFixedHeight(4)
        self.busy_bar.hide()
        lay.addWidget(self.busy_bar)

        # ---- Six platform chips, always visible (selected = drafting for it; click to jump)
        chip_row = QHBoxLayout()
        chip_row.setSpacing(6)
        self.platform_chips: dict[str, QToolButton] = {}
        for spec in TICKET:
            chip = QToolButton(objectName="RpChip")
            chip.setText(platform_label(spec.key))
            chip.setIcon(platform_icon(spec.key, 18))
            from PySide6.QtCore import QSize
            chip.setIconSize(QSize(18, 18))
            chip.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            chip.setCheckable(True)
            chip.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)     # full names, never elided
            bold = chip.font()
            bold.setBold(True)
            from PySide6.QtGui import QFontMetrics
            chip.setMinimumWidth(QFontMetrics(bold).horizontalAdvance(chip.text()) + 18 + 6 + 2 * 9 + 18)
            on = spec.key in self.platforms
            chip.setChecked(on)
            chip.setEnabled(on)
            chip.setToolTip(f"Drafting for {platform_label(spec.key)}. Click to jump to it." if on else
                            f"Not in Posting to. Tick {platform_label(spec.key)} on the ticket to include it.")
            chip.clicked.connect(lambda _c=False, k=spec.key: self._jump_to(k))
            self.platform_chips[spec.key] = chip
            chip_row.addWidget(chip)
        chip_row.addStretch()
        lay.addLayout(chip_row)

        # ---- Empty state (intentional, not blank)
        self.placeholder = QFrame(objectName="RpEmpty")
        el = QVBoxLayout(self.placeholder)
        el.setContentsMargins(20, 26, 20, 26)
        el.setSpacing(10)
        logos = QHBoxLayout()
        logos.addStretch()
        for spec in TICKET:
            lg = QLabel()
            lg.setPixmap(platform_pixmap(spec.key, 28, faded=spec.key not in self.platforms))
            logos.addWidget(lg)
        logos.addStretch()
        el.addLayout(logos)
        empty_text = ("Your six drafts will appear here after you generate." if len(self.platforms) == len(TICKET)
                      else "Your drafts will appear here after you generate.")
        msg = label(empty_text, prop="muted")
        msg.setAlignment(Qt.AlignCenter)
        el.addWidget(msg)
        lay.addWidget(self.placeholder)

        # ---- Results: one card per platform, one copy card per field
        self.results = QWidget()
        self.fields_layout = QVBoxLayout(self.results)
        self.fields_layout.setContentsMargins(0, 0, 0, 0)
        self.fields_layout.setSpacing(12)
        self.results.hide()
        lay.addWidget(self.results)
        lay.addStretch()

        self.editors: dict[str, QLineEdit | QPlainTextEdit] = {}
        self.counters: dict[str, QLabel] = {}
        self.field_boxes: dict[str, QWidget] = {}
        self.sections: dict[str, QWidget] = {}

        groups: list[tuple[str, str, tuple[str, ...]]] = [
            (p, platform_label(p), SPECS[p].copy_fields) for p in self.platforms
        ]
        if "hashtags" in self.selected_fields:
            groups.append((SHARED_KEY, "Shared hashtags", ("hashtags",)))
        for key, title_text, fields in groups:
            section = QFrame(objectName="RpCard")
            sec = QVBoxLayout(section)
            sec.setContentsMargins(16, 14, 16, 16)
            sec.setSpacing(10)
            hrow = QHBoxLayout()
            hrow.setSpacing(10)
            if key != SHARED_KEY:
                logo = QLabel()
                logo.setPixmap(platform_pixmap(key, 24))
                hrow.addWidget(logo)
            hrow.addWidget(label(title_text, name="RpSection"))
            hrow.addStretch()
            sec.addLayout(hrow)
            for field in fields:
                sec.addWidget(self._make_field(field, platform=None if key == SHARED_KEY else key))
            shadow(section, blur=22, alpha=70, dy=4)
            section.hide()
            self.sections[key] = section
            self.fields_layout.addWidget(section)

        # ---- Footer: note + quiet Cancel + primary Apply
        footer = QFrame(objectName="RpCard")
        footer.setStyleSheet(f"QFrame#RpCard {{ border-radius: 0; border-left: none; border-right: none; "
                             f"border-bottom: none; background: {pal.bg_primary}; }}")
        bottom = QHBoxLayout(footer)
        bottom.setContentsMargins(24, 12, 24, 14)
        bottom.setSpacing(10)
        self.apply_note = label(APPLY_NOTE, prop="faint")
        bottom.addWidget(self.apply_note, 1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("RpQuiet")
        self.cancel_btn.clicked.connect(self.reject)
        bottom.addWidget(self.cancel_btn)
        self.apply_btn = tip(QPushButton("Apply to Ticket"), APPLY_NOTE)
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._accept_proposal)
        bottom.addWidget(self.apply_btn)
        outer.addWidget(footer)

        # ---- Disclosure pop-over (full technical details), floats above a dimmed dialog with a shadow
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
        set_prop(close, "primary")
        close.clicked.connect(self._toggle_disclosure)
        orow.addWidget(close)
        ol.addLayout(orow)
        self.disclosure_label = label(DISCLOSURE_TEXT, prop="muted")
        self.disclosure_label.setWordWrap(True)
        ol.addWidget(self.disclosure_label)
        shadow(self.disclosure_overlay, blur=48, alpha=170, dy=12)
        self.disclosure_overlay.hide()

        self._set_idle_status()
        if parent is not None and hasattr(parent, "destroyed"):
            parent.destroyed.connect(self._mark_closed)

    # ------------------------------------------------------------------ building
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
        width = min(self.width() - 48, 600)
        o.setFixedWidth(width)
        o.adjustSize()
        o.move((self.width() - width) // 2, 90)
        self.disclosure_backdrop.setGeometry(0, 0, self.width(), self.height())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if getattr(self, "disclosure_overlay", None) is not None and self.disclosure_overlay.isVisible():
            self._place_overlay()

    def _toggle_snapshot(self) -> None:
        expanded = self.source_preview.height() > 130
        h = 120 if expanded else 280
        self.source_preview.setStyleSheet(f"QPlainTextEdit {{ min-height: {h - 20}px; }}")
        self.source_preview.setFixedHeight(h)
        self.snapshot_toggle.setText("Show all" if expanded else "Show less")

    def _jump_to(self, platform: str) -> None:
        for key, chip in self.platform_chips.items():
            chip.setChecked(key in self.platforms)
        section = self.sections.get(platform)
        if section is not None and not section.isHidden():
            self.scroll.ensureWidgetVisible(section, 0, 40)

    def _make_field(self, key: str, show_name: bool = True, platform: str | None = None) -> QWidget:
        from PySide6.QtCore import Qt

        from .logos import platform_pixmap
        box = QFrame(objectName="RpField")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(14, 12, 14, 14)
        bl.setSpacing(8)
        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        if platform:
            logo = QLabel()
            logo.setPixmap(platform_pixmap(platform, 16))
            hdr.addWidget(logo)
        name = QLabel(FIELD_NAMES[key], objectName="RpFieldName")
        name.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        hdr.addWidget(name)
        hdr.addStretch()
        cnt = QLabel("", objectName="RpPill")
        cnt.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.counters[key] = cnt
        hdr.addWidget(cnt)
        bl.addLayout(hdr)
        if key in MULTILINE:
            ed = QPlainTextEdit()
            height = EDITOR_HEIGHT.get(key, 96)
            # The app stylesheet's min-height would otherwise let the scroll area squeeze this to one line.
            ed.setStyleSheet(f"QPlainTextEdit {{ min-height: {height - 24}px; }}")
            ed.setFixedHeight(height)
            ed.textChanged.connect(lambda k=key: self._update_field_count(k))
        else:
            ed = QLineEdit()
            ed.setMinimumHeight(40)
            ed.textChanged.connect(lambda _text, k=key: self._update_field_count(k))
        self.editors[key] = ed
        bl.addWidget(ed)
        self.field_boxes[key] = box
        return box

    @staticmethod
    def _format_source_snapshot(snapshot: dict[str, str]) -> str:
        return "\n".join(
            [
                f"Title: {snapshot.get('title') or '(none)'}",
                f"Description: {snapshot.get('description') or '(none)'}",
                f"Notes (Topic / Keywords): {snapshot.get('notes') or '(none)'}",
                f"Transcript: {snapshot.get('transcript') or '(none)'}",
                f"Brand Voice Guidance: {snapshot.get('brand_voice') or '(none)'}",
            ]
        )

    # ------------------------------------------------------------------ state helpers
    def _set_idle_status(self) -> None:
        set_prop(self.status, "danger", False)
        if not self.platforms:
            self.status.setText("Select at least one destination before generating.")
            self.run_btn.setEnabled(False)
        elif self._has_result:
            self.status.setText("Proposal ready. Review or edit, then click Apply to Ticket.")
        else:
            self.status.setText("Tick “Allow Muse Spark” above, then click Generate Proposal.")

    def _on_opt_in_toggled(self, checked: bool) -> None:
        if self.store is not None:
            try:
                set_cloud_ai_enabled(self.store, checked)
            except Exception:  # noqa: BLE001
                pass

    def _get_field_text(self, key: str) -> str:
        ed = self.editors.get(key)
        if ed is None:
            return ""
        if isinstance(ed, QPlainTextEdit):
            return ed.toPlainText().strip()
        return ed.text().strip()

    def _set_field_text(self, key: str, value: str) -> None:
        ed = self.editors.get(key)
        if ed is None:
            return
        if isinstance(ed, QPlainTextEdit):
            ed.setPlainText(value)
        else:
            ed.setText(value)

    def _set_editors_read_only(self, read_only: bool) -> None:
        for ed in self.editors.values():
            ed.setReadOnly(read_only)

    def _visible_fields(self) -> list[str]:
        """Selected fields that are currently shown (they were returned non-empty)."""
        return [k for k in self.selected_fields if k in self.field_boxes and not self.field_boxes[k].isHidden()]

    def _update_field_count(self, key: str) -> None:
        text = self._get_field_text(key)
        cnt_label = self.counters[key]
        if not text:
            cnt_label.setText("empty · not applied")
            set_prop(cnt_label, "state", "empty")
        else:
            shown, bad = counter_text(key, text)
            cnt_label.setText(shown)
            set_prop(cnt_label, "state", "fail" if bad else "pass")
        self._sync_apply_enabled()

    def _can_apply(self) -> bool:
        if self._busy or self._worker_running() or not self._has_result:
            return False
        fields = self._visible_fields()
        if not any(self._get_field_text(k) for k in fields):
            return False
        for key in fields:
            text = self._get_field_text(key)
            if not text:
                continue
            _, bad = counter_text(key, text)
            if bad:
                return False
        return True

    def _sync_apply_enabled(self) -> None:
        if hasattr(self, "apply_btn"):
            self.apply_btn.setEnabled(self._can_apply())

    def _worker_running(self) -> bool:
        """True while self.worker is alive and running; drops a worker Qt has already deleted."""
        worker = self.worker
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:  # the C++ CallWorker was destroyed by deleteLater()
            self.worker = None
            return False

    def _set_busy_ui(self, busy: bool) -> None:
        self.busy_bar.setVisible(busy)
        self.run_btn.setEnabled(not busy and bool(self.platforms))
        self.run_btn.setText("Generating…" if busy else ("Regenerate" if self._has_result else "Generate Proposal"))
        self._set_editors_read_only(busy)
        # Button hierarchy: Generate is primary until a proposal exists, then Apply takes over.
        ready = self._has_result and not busy
        set_prop(self.run_btn, "primary", not ready)
        set_prop(self.apply_btn, "primary", ready)

    # ------------------------------------------------------------------ generation
    def _run(self) -> None:
        if self._busy or self._worker_running():
            return
        if not self.platforms:
            self._set_idle_status()
            return
        if not self.opt_in_box.isChecked():
            self.status.setText(
                "Cloud AI is disabled. Tick 'Allow Muse Spark to draft copy for this video' first."
            )
            set_prop(self.status, "danger", True)
            self._sync_apply_enabled()
            return

        self._busy = True
        self._closed = False
        self._active_request_id += 1
        req_id = self._active_request_id
        snapshot = dict(self._source_snapshot)

        self._set_busy_ui(True)
        self._sync_apply_enabled()
        set_prop(self.status, "danger", False)
        self.status.setText(
            "Asking Meta Muse Spark… the previous proposal stays here until the new one arrives."
            if self._has_result else "Asking Meta Muse Spark… this can take up to a minute."
        )

        def work() -> dict[str, str]:
            try:
                return self._generate_fn(
                    title=snapshot["title"],
                    description=snapshot["description"],
                    notes=snapshot["notes"],
                    transcript=snapshot["transcript"],
                    brand_voice=snapshot["brand_voice"],
                    store=self.store,
                    opt_in=True,
                )
            except (RepurposeError, PrivacyOptInRequiredError) as exc:
                raise AssistError(str(exc)) from None

        worker = CallWorker(work, parent=None)
        RepurposeDialog._in_flight_workers.add(worker)
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        worker.succeeded.connect(lambda res, r=req_id: self._on_worker_succeeded(r, res))
        worker.failed.connect(lambda msg, r=req_id: self._on_worker_failed(r, msg))
        self.worker = worker
        self._sync_apply_enabled()
        worker.start()

    def _on_worker_finished(self, worker: CallWorker) -> None:
        RepurposeDialog._in_flight_workers.discard(worker)
        if self.worker is worker:
            self.worker = None
        worker.deleteLater()

    def _on_worker_succeeded(self, req_id: int, result: dict[str, str]) -> None:
        if self._closed or req_id != self._active_request_id:
            return
        self.worker = None
        self.populate_proposal(result)

    def _on_worker_failed(self, req_id: int, message: str) -> None:
        if self._closed or req_id != self._active_request_id:
            return
        self.worker = None
        self._on_failed(message)

    def populate_proposal(self, result: dict[str, str]) -> None:
        """Shows the selected platforms' returned copy; empty or unselected fields stay hidden."""
        self._busy = False
        shown_any = False
        for key in self.selected_fields:
            value = str(result.get(key, "") or "").strip()
            self._set_field_text(key, value)
            self.field_boxes[key].setVisible(bool(value))
            self._update_field_count(key)
            shown_any = shown_any or bool(value)
        for group, section in self.sections.items():
            fields = ("hashtags",) if group == SHARED_KEY else SPECS[group].copy_fields
            has = any(not self.field_boxes[f].isHidden() for f in fields)
            section.setVisible(has)
        self._has_result = shown_any
        self.results.setVisible(shown_any)
        self.placeholder.setVisible(not shown_any)
        self._set_busy_ui(False)
        if shown_any:
            self._set_idle_status()
        else:
            self.status.setText("Muse Spark returned no copy for the selected destinations. Try Generate again.")
            set_prop(self.status, "danger", True)
        self._sync_apply_enabled()

    def _on_failed(self, message: str) -> None:
        self._busy = False
        self._set_busy_ui(False)
        self.status.setText(message + (" The previous proposal is unchanged." if self._has_result else ""))
        set_prop(self.status, "danger", True)
        self._sync_apply_enabled()

    def _accept_proposal(self) -> None:
        if self._busy or self._worker_running() or not self._can_apply():
            return
        self.proposal = {
            key: self._get_field_text(key) for key in self._visible_fields() if self._get_field_text(key)
        }
        self.accept()

    def _mark_closed(self) -> None:
        self._closed = True
        self._busy = False
        self._active_request_id += 1
        self.proposal = None

    def reject(self) -> None:
        self._mark_closed()
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._mark_closed()
        super().closeEvent(event)
