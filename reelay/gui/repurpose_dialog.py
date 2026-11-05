"""Repurpose Engine GUI dialog: review and approve Meta Muse Spark copy for the ticket's chosen platforms."""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
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
from .widgets import Collapsible, label, scroll_page
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
EDITOR_HEIGHT = {"description": 96, "facebook_text": 72}
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
        self.resize(680, 780)
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

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 14, 20, 14)
        lay.setSpacing(8)

        # ---- Privacy disclosure & explicit consent (always starts unticked)
        disclosure_card = QFrame(objectName="Card")
        dl = QVBoxLayout(disclosure_card)
        dl.setContentsMargins(12, 8, 12, 8)
        dl.setSpacing(5)
        self.disclosure_label = label(DISCLOSURE_TEXT, prop="faint")
        dl.addWidget(self.disclosure_label)
        self.opt_in_box = tip(
            QCheckBox("Enable Cloud AI for Reelay: send the source text below to Meta when I click Generate"),
            "Consent authorizes sending the Source Snapshot to Meta for this request. It starts unticked "
            "every time this window opens.",
        )
        self.opt_in_box.setChecked(False)
        self.opt_in_box.toggled.connect(self._on_opt_in_toggled)
        dl.addWidget(self.opt_in_box)
        lay.addWidget(disclosure_card)

        # ---- Read-only source snapshot
        source_card = QFrame(objectName="Card")
        sl = QVBoxLayout(source_card)
        sl.setContentsMargins(12, 8, 12, 8)
        sl.setSpacing(4)
        sl.addWidget(label("<b>Source Snapshot — Read-only</b>", rich=True))
        sl.addWidget(label("This is the text that will be sent when you click Generate Proposal.", prop="faint"))
        self.source_preview = QPlainTextEdit()
        self.source_preview.setReadOnly(True)
        self.source_preview.setFixedHeight(84)
        self.source_preview.setPlainText(self._format_source_snapshot(self._source_snapshot))
        tip(self.source_preview, "Title, description, notes, transcript, and brand voice guidance for this ticket")
        sl.addWidget(self.source_preview)
        lay.addWidget(source_card)

        # ---- Destinations (from Posting to) + generate
        if self.platforms:
            names = ", ".join(platform_label(p) for p in self.platforms)
            dest_text = (f"<b>Destinations:</b> {names} (from Posting to). Muse Spark drafts every platform's "
                         "copy; only these are shown and applied.")
        else:
            dest_text = ("<b>No destinations selected.</b> Tick at least one platform under Posting to, "
                         "then open Repurpose again.")
        self.destinations_label = label(dest_text, rich=True)
        lay.addWidget(self.destinations_label)

        action_row = QHBoxLayout()
        self.status = label("", prop="muted")
        action_row.addWidget(self.status, 1)
        self.run_btn = tip(QPushButton("Generate Proposal"), "Ask Meta Muse Spark for a copy proposal")
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

        # ---- Results, grouped by platform; hidden until a proposal arrives
        self.results, _body, self.fields_layout = scroll_page(spacing=8, sideways=False)
        self.results.hide()
        lay.addWidget(self.results, 1)
        self.placeholder = label("Results appear here, grouped by destination, after you generate a proposal.",
                                 prop="faint")
        lay.addWidget(self.placeholder, 1)

        self.editors: dict[str, QLineEdit | QPlainTextEdit] = {}
        self.counters: dict[str, QLabel] = {}
        self.field_boxes: dict[str, QWidget] = {}
        self.sections: dict[str, Collapsible] = {}

        groups: list[tuple[str, str, Any, tuple[str, ...]]] = [
            (p, platform_label(p), platform_icon(p, 16), SPECS[p].copy_fields) for p in self.platforms
        ]
        if "hashtags" in self.selected_fields:
            groups.append((SHARED_KEY, "Shared hashtags", None, ("hashtags",)))
        for key, title_text, icon, fields in groups:
            section = Collapsible(title_text, icon=icon, expanded=True)
            for field in fields:
                section.add(self._make_field(field, show_name=not (key == SHARED_KEY)))
            section.hide()
            self.sections[key] = section
            self.fields_layout.addWidget(section)
        self.fields_layout.addStretch()

        # ---- Cancel / Apply to Ticket
        bottom = QHBoxLayout()
        self.apply_note = label(APPLY_NOTE, prop="faint")
        bottom.addWidget(self.apply_note, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        self.apply_btn = buttons.button(QDialogButtonBox.Ok)
        self.apply_btn.setText("Apply to Ticket")
        tip(self.apply_btn, APPLY_NOTE)
        self.apply_btn.setEnabled(False)
        buttons.accepted.connect(self._accept_proposal)
        buttons.rejected.connect(self.reject)
        bottom.addWidget(buttons)
        lay.addLayout(bottom)

        self._set_idle_status()
        if parent is not None and hasattr(parent, "destroyed"):
            parent.destroyed.connect(self._mark_closed)

    # ------------------------------------------------------------------ building
    def _make_field(self, key: str, show_name: bool = True) -> QWidget:
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 2, 0, 2)
        bl.setSpacing(4)
        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        name = QLabel(f"<b>{FIELD_NAMES[key]}</b>" if show_name else "")
        name.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        hdr.addWidget(name)
        cnt = label("", prop="faint", wrap=False)
        cnt.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        cnt.setMinimumWidth(80)
        from PySide6.QtCore import Qt
        cnt.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.counters[key] = cnt
        hdr.addWidget(cnt, 1)
        bl.addLayout(hdr)
        if key in MULTILINE:
            ed = QPlainTextEdit()
            height = EDITOR_HEIGHT.get(key, 64)
            # The app stylesheet's min-height would otherwise let the scroll area squeeze this to one line.
            ed.setStyleSheet(f"QPlainTextEdit {{ min-height: {height - 14}px; }}")
            ed.setFixedHeight(height)
            ed.textChanged.connect(lambda k=key: self._update_field_count(k))
        else:
            ed = QLineEdit()
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
            self.status.setText("Tick consent above, then click Generate Proposal.")

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
            set_prop(cnt_label, "danger", False)
        else:
            shown, bad = counter_text(key, text)
            cnt_label.setText(shown)
            set_prop(cnt_label, "danger", bad)
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

    # ------------------------------------------------------------------ generation
    def _run(self) -> None:
        if self._busy or self._worker_running():
            return
        if not self.platforms:
            self._set_idle_status()
            return
        if not self.opt_in_box.isChecked():
            self.status.setText(
                "Cloud AI is disabled. Tick 'Enable Cloud AI' above before generating a proposal."
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
            if has:
                section.set_expanded(True)
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
