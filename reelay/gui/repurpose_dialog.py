"""Repurpose Engine GUI dialog: review and approve 6-platform copy proposals from Meta Muse Spark."""

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
    QPushButton,
    QVBoxLayout,
)

from .. import assist
from ..assist import AssistError
from ..platform_specs import count
from ..privacy import (
    DISCLOSURE_TEXT,
    PrivacyOptInRequiredError,
    is_cloud_ai_enabled,
    set_cloud_ai_enabled,
)
from ..repurpose_engine import (
    SCHEMA_FIELDS,
    RepurposeError,
    generate_repurpose_proposal,
)
from .theme import set_prop
from .tips import tip
from .widgets import label, scroll_page
from .workers import CallWorker

FIELD_SPECS: tuple[tuple[str, str, bool, int], ...] = (
    ("title", "YouTube Title (≤ 100 chars)", False, 0),
    ("description", "YouTube Description (≤ 5,000 UTF-8 bytes)", True, 72),
    ("tags", "YouTube Tags (comma-separated, no #, ≤ 500 chars)", False, 0),
    ("tiktok_text", "TikTok Caption (≤ 4,000 chars)", True, 64),
    ("instagram_text", "Instagram Reels Caption (≤ 2,200 chars)", True, 64),
    ("bluesky_text", "Bluesky Post (≤ 300 graphemes)", True, 56),
    ("threads_text", "Threads Post (≤ 500 chars, zero # in body)", True, 56),
    ("facebook_text", "Facebook Post (≤ 63,206 chars)", True, 64),
    ("hashtags", "Shared Hashtags (space-separated #tags)", False, 0),
)


class RepurposeDialog(QDialog):
    """Modal proposal review dialog for Meta Muse Spark (muse-spark-1.3).

    Displays the exact source snapshot and privacy disclosure before transmission,
    generates 9 platform copy fields in a background thread without blocking close/cancel,
    validates every field against FIELD_LIMITS without mechanical truncation, and lets
    the creator review and edit the proposal before applying it to in-memory ticket fields.
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
    ):
        super().__init__(parent)
        self.setWindowTitle("Repurpose with Meta Muse Spark")
        self.resize(700, 760)
        self.card = card
        self.store = store
        self.proposal: dict[str, str] | None = None
        self.worker: CallWorker | None = None
        self._busy: bool = False
        self._closed: bool = False
        self._active_request_id: int = 0
        self._generate_fn = generate_fn or generate_repurpose_proposal

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

        # ---- Privacy disclosure & explicit opt-in gate
        disclosure_card = QFrame(objectName="Card")
        dl = QVBoxLayout(disclosure_card)
        dl.setContentsMargins(12, 8, 12, 8)
        dl.setSpacing(5)
        self.disclosure_label = label(DISCLOSURE_TEXT, prop="faint")
        dl.addWidget(self.disclosure_label)
        self.opt_in_box = tip(
            QCheckBox("Enable Cloud AI for Reelay (Meta Model API: muse-spark-1.3, non-contributor tier)"),
            "Explicitly opt in to sending this ticket's title, description, notes, and transcript "
            "to Meta Muse Spark for copy generation.",
        )
        self.opt_in_box.setChecked(is_cloud_ai_enabled(store))
        self.opt_in_box.toggled.connect(self._on_opt_in_toggled)
        dl.addWidget(self.opt_in_box)
        lay.addWidget(disclosure_card)

        # ---- Compact read-only source preview
        source_card = QFrame(objectName="Card")
        sl = QVBoxLayout(source_card)
        sl.setContentsMargins(12, 8, 12, 8)
        sl.setSpacing(4)
        sl.addWidget(label("<b>Source Snapshot Sent to Meta Muse Spark (Read-Only)</b>", rich=True))
        self.source_preview = QPlainTextEdit()
        self.source_preview.setReadOnly(True)
        self.source_preview.setFixedHeight(84)
        self.source_preview.setPlainText(self._format_source_snapshot(self._source_snapshot))
        tip(
            self.source_preview,
            "Exact title, description, notes, transcript, and brand voice guidance sent when you click Generate Proposal",
        )
        sl.addWidget(self.source_preview)
        lay.addWidget(source_card)

        # ---- Action bar
        action_row = QHBoxLayout()
        self.status = label(
            "Review source details above and click Generate Proposal. Ticket copy is not saved until you click Save Changes.",
            prop="muted",
        )
        action_row.addWidget(self.status, 1)
        self.run_btn = tip(
            QPushButton("Generate Proposal"),
            "Ask Meta Muse Spark (muse-spark-1.3) for a 6-platform copy proposal",
        )
        set_prop(self.run_btn, "primary")
        self.run_btn.clicked.connect(self._run)
        action_row.addWidget(self.run_btn)
        lay.addLayout(action_row)

        # ---- Scrollable 9-field proposal review area
        scroll, _body, self.fields_layout = scroll_page(spacing=8, sideways=False)
        lay.addWidget(scroll, 1)

        self.editors: dict[str, QLineEdit | QPlainTextEdit] = {}
        self.counters: dict[str, QLabel] = {}

        for key, field_title, multiline, height in FIELD_SPECS:
            box = QFrame()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, 2, 0, 2)
            bl.setSpacing(4)
            hdr = QHBoxLayout()
            hdr.addWidget(label(f"<b>{field_title}</b>", rich=True))
            hdr.addStretch()
            cnt = label("empty", prop="faint", wrap=False)
            self.counters[key] = cnt
            hdr.addWidget(cnt)
            bl.addLayout(hdr)

            if multiline:
                ed = QPlainTextEdit()
                ed.setFixedHeight(height)
                ed.textChanged.connect(lambda k=key: self._update_field_count(k))
            else:
                ed = QLineEdit()
                ed.textChanged.connect(lambda _text, k=key: self._update_field_count(k))
            self.editors[key] = ed
            bl.addWidget(ed)
            self.fields_layout.addWidget(box)

        self.fields_layout.addStretch()

        # ---- Dialog buttons (Cancel / Apply to Ticket)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        self.apply_btn = buttons.button(QDialogButtonBox.Ok)
        self.apply_btn.setText("Apply to Ticket")
        self.apply_btn.setEnabled(False)
        buttons.accepted.connect(self._accept_proposal)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        if parent is not None and hasattr(parent, "destroyed"):
            parent.destroyed.connect(self._mark_closed)

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

    def _on_opt_in_toggled(self, checked: bool) -> None:
        if self.store is not None:
            try:
                set_cloud_ai_enabled(self.store, checked)
            except Exception:  # noqa: BLE001
                pass

    def _get_field_text(self, key: str) -> str:
        ed = self.editors[key]
        if isinstance(ed, QPlainTextEdit):
            return ed.toPlainText().strip()
        return ed.text().strip()

    def _set_field_text(self, key: str, value: str) -> None:
        ed = self.editors[key]
        if isinstance(ed, QPlainTextEdit):
            ed.setPlainText(value)
        else:
            ed.setText(value)

    def _set_editors_read_only(self, read_only: bool) -> None:
        for ed in self.editors.values():
            ed.setReadOnly(read_only)

    def _update_field_count(self, key: str) -> None:
        text = self._get_field_text(key)
        cnt_label = self.counters[key]
        if not text and key not in ("hashtags",):
            cnt_label.setText("empty")
            set_prop(cnt_label, "danger", False)
            self._sync_apply_enabled()
            return

        count_str, over = count(key, text)
        extra = ""
        if key == "threads_text" and "#" in text:
            over = True
            extra = " · ⚠ no # allowed in Threads"
        badge = "⚠ OVER LIMIT" if over else "PASS"
        cnt_label.setText(f"{count_str}{extra} — {badge}")
        set_prop(cnt_label, "danger", over)
        self._sync_apply_enabled()

    def _can_apply(self) -> bool:
        if self._busy or self._worker_running():
            return False
        has_any = any(bool(self._get_field_text(k)) for k in SCHEMA_FIELDS)
        if not has_any:
            return False
        for key in SCHEMA_FIELDS:
            text = self._get_field_text(key)
            _, over = count(key, text)
            if over:
                return False
            if key == "threads_text" and "#" in text:
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

    def _run(self) -> None:
        if self._busy or self._worker_running():
            return
        if not self.opt_in_box.isChecked():
            self._on_opt_in_toggled(False)
            self.status.setText(
                "Cloud AI is disabled. Tick 'Enable Cloud AI' above before generating a proposal."
            )
            set_prop(self.status, "danger", True)
            self._sync_apply_enabled()
            return

        self._on_opt_in_toggled(True)
        self._busy = True
        self._closed = False
        self._active_request_id += 1
        req_id = self._active_request_id
        snapshot = dict(self._source_snapshot)

        self.run_btn.setEnabled(False)
        self._set_editors_read_only(True)
        self._sync_apply_enabled()
        set_prop(self.status, "danger", False)
        self.status.setText("Asking Meta Muse Spark (muse-spark-1.3) for 6-platform copy…")

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
        """Fills the 9 review editors with the generated proposal dictionary."""
        self._busy = False
        self.run_btn.setEnabled(True)
        self._set_editors_read_only(False)
        set_prop(self.status, "danger", False)
        for key in SCHEMA_FIELDS:
            self._set_field_text(key, str(result.get(key, "") or ""))
            self._update_field_count(key)
        self.status.setText(
            "Proposal ready! Review or edit any field below, then click Apply to Ticket."
        )
        self._sync_apply_enabled()

    def _on_failed(self, message: str) -> None:
        self._busy = False
        self.run_btn.setEnabled(True)
        self._set_editors_read_only(False)
        self.status.setText(message)
        set_prop(self.status, "danger", True)
        self._sync_apply_enabled()

    def _accept_proposal(self) -> None:
        if self._busy or self._worker_running() or not self._can_apply():
            return
        self.proposal = {key: self._get_field_text(key) for key in SCHEMA_FIELDS}
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
