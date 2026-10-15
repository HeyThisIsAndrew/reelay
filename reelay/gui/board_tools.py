"""Board tools: the bulk editor (several cards selected), the spreadsheet
import, and the archive."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidgetItem, QPushButton, QTableWidgetItem,
                               QVBoxLayout)

from ..models import ContentType
from ..sheet_import import FIELDS, LABELS, MATCH_KEYS, SheetError, fetch_google_sheet, google_export_url, read_file
from ..text import local_text, short_title
from . import theme
from .theme import set_prop
from .ticket_widgets import AUTHORIZATION_PRESETS
from .tips import tip
from .workers import CallWorker
from .widgets import label as _label, scroll_page, table


def _section(text: str, hint: str) -> QLabel:
    from .theme import heading
    lab = QLabel(heading(text))
    lab.setProperty("section", True)
    return tip(lab, hint)


# ====================================================================== bulk editor
class BulkPanel(QFrame):
    """Shown instead of the ticket when several cards are selected."""
    applyRequested = Signal(dict)
    fillRequested = Signal()
    transcriptsRequested = Signal()
    archiveRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DetailFrame")
        self.setMinimumWidth(330)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll, body, lay = scroll_page((18, 18, 18, 18), 8, name="DetailScroll", body_name="DetailBody",
                                        sideways=False)
        outer.addWidget(scroll)

        self.title = QLabel(objectName="DetailTitle")
        lay.addWidget(self.title)
        self.summary = _label(prop="muted")
        lay.addWidget(self.summary)
        lay.addSpacing(6)

        lay.addWidget(_section("QUICK FILL", "Fill tickets from what's already known about each video"))
        self.fill_btn = tip(QPushButton("Fill Empty Copy from Original Posts"),
                            "Each selected ticket's empty captions, title, description, tags and hashtags are "
                            "filled from its original caption, within every platform's limits. Nothing already "
                            "written is changed, and nothing is shortened.")
        self.fill_btn.clicked.connect(self.fillRequested.emit)
        lay.addWidget(self.fill_btn)
        self.transcripts_btn = tip(QPushButton("Get Transcripts from Original Posts"),
                                   "Download each post's captions as its transcript (where the platform has "
                                   "captions). Tickets that already have a transcript are skipped. Uses the "
                                   "network; takes a few seconds per video.")
        self.transcripts_btn.clicked.connect(self.transcriptsRequested.emit)
        lay.addWidget(self.transcripts_btn)

        lay.addSpacing(8)
        lay.addWidget(_section("SET ON EVERY SELECTED TICKET", "Leave a field blank to keep each ticket's own value"))
        self.permission = QComboBox()
        self.permission.setEditable(True)
        self.permission.addItem("")
        self.permission.addItems(AUTHORIZATION_PRESETS)
        self.permission.lineEdit().setPlaceholderText("Permission: leave as is")
        tip(self.permission, "Your right to use these videos, e.g. Own content")
        self.content_type = tip(QComboBox(), "What kind of videos these are")
        self.content_type.addItem("Type: leave as is", None)
        for key in ContentType.ALL:
            self.content_type.addItem(ContentType.LABELS[key], key)
        self.inputs: dict[str, QLineEdit] = {}
        lay.addWidget(self.permission)
        lay.addWidget(self.content_type)
        for key, placeholder, hint in (("topic", "Topic: leave as is", "What the videos are about"),
                                       ("keywords", "Keywords: leave as is", "Search words, separated by commas"),
                                       ("creator", "Creator: leave as is", "Brand or person name"),
                                       ("hashtags", "Hashtags: leave as is", "One hashtag block for every platform")):
            edit = tip(QLineEdit(), hint)
            edit.setPlaceholderText(placeholder)
            edit.setAttribute(Qt.WA_MacShowFocusRect, False)
            self.inputs[key] = edit
            lay.addWidget(edit)
        self.apply_btn = tip(QPushButton("Apply"), "Set the filled-in fields on every selected ticket at once. "
                                                   "If any ticket can't take a value, nothing changes.")
        set_prop(self.apply_btn, "primary")
        self.apply_btn.clicked.connect(self._apply)
        lay.addWidget(self.apply_btn)
        self.errors = _label(prop="danger")
        lay.addWidget(self.errors)

        lay.addSpacing(8)
        self.archive_btn = tip(QPushButton("Archive"), "Hide these videos from the Board and the Download page. "
                                                       "Files, tickets and history are kept; bring them back from "
                                                       "Archived…")
        set_prop(self.archive_btn, "flat")
        self.archive_btn.clicked.connect(self.archiveRequested.emit)
        lay.addWidget(self.archive_btn, 0, Qt.AlignLeft)
        lay.addStretch()

    def show_cards(self, cards: list) -> None:
        n = len(cards)
        self.title.setText(f"{n} tickets selected")
        ready = sum(1 for c in cards if c.readiness is None or c.readiness.ready)
        self.summary.setText(f"{ready} ready · {n - ready} not ready. Tip: ⌘-click or Shift-click cards in a "
                             "column to change the selection.")
        self.apply_btn.setText(f"Apply to {n} Tickets")
        self.archive_btn.setText(f"Archive {n} Videos")
        self.errors.setText("")

    def changes(self) -> dict:
        out = {}
        permission = self.permission.currentText().strip()
        if permission:
            out["authorization"] = permission
        if self.content_type.currentData():
            out["content_type"] = self.content_type.currentData()
        for key, edit in self.inputs.items():
            if edit.text().strip():
                out[key] = edit.text().strip()
        return out

    def clear_inputs(self) -> None:
        self.permission.setCurrentIndex(0)
        self.permission.setEditText("")
        self.content_type.setCurrentIndex(0)
        for edit in self.inputs.values():
            edit.clear()

    def _apply(self) -> None:
        changes = self.changes()
        if not changes:
            self.errors.setText("Fill in at least one field to set.")
            return
        self.applyRequested.emit(changes)

    def show_errors(self, problems: list[str]) -> None:
        self.errors.setText("\n".join(f"• {p}" for p in problems))


# ====================================================================== spreadsheet import
TARGETS = [(None, "Ignore this column")] + [(k, LABELS[k]) for k in MATCH_KEYS] + [(k, LABELS[k]) for k in FIELDS]


class ImportDialog(QDialog):
    """Pick a CSV (or a Google Sheets link), check how the columns map, preview
    every row, then import in one go."""

    def __init__(self, pipeline, profile, parent=None, session=None):
        super().__init__(parent)
        self.setWindowTitle("Import from a Spreadsheet")
        self.resize(980, 680)
        self.pipeline, self.profile = pipeline, profile
        self.session = session
        self.headers: list[str] = []
        self.rows: list[list[str]] = []
        self.plan = None
        self.applied = 0
        self.worker: CallWorker | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(10)
        lay.addWidget(_label("Fill tickets from a sheet with one row per video. Each row is matched to a video on "
                             f"{profile.name}'s board by its link, video ID, file name or your name for it. "
                             "Nothing is written until you click Import, and empty cells never erase anything.",
                             "muted"))
        src = QHBoxLayout()
        self.file_btn = tip(QPushButton("Choose CSV File…"), "In Google Sheets: File → Download → "
                                                             "Comma-separated values (.csv). Excel and Numbers can "
                                                             "export CSV too.")
        self.file_btn.clicked.connect(self._choose_file)
        src.addWidget(self.file_btn)
        src.addWidget(_label("or", "faint"))
        self.link = tip(QLineEdit(), "A Google Sheets link. The sheet must be shared as “Anyone with the link” "
                                     "(Viewer). The tab in the link is the one imported.")
        self.link.setPlaceholderText("https://docs.google.com/spreadsheets/d/…")
        self.link.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.link.returnPressed.connect(self._load_link)
        src.addWidget(self.link, 1)
        self.link_btn = tip(QPushButton("Load Sheet"), "Download the sheet from Google")
        self.link_btn.clicked.connect(self._load_link)
        src.addWidget(self.link_btn)
        lay.addLayout(src)
        self.source_label = _label(prop="faint")
        lay.addWidget(self.source_label)

        body = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(_section("COLUMNS", "Which ticket field each column of the sheet fills"))
        self.mapping_table = table(["SHEET COLUMN", "GOES TO"], select="none")
        left.addWidget(self.mapping_table, 1)
        self.overwrite = tip(QCheckBox("Replace what's already filled in"),
                             "Off: only empty fields are filled (safest). On: the sheet's values replace what's "
                             "on the tickets. Copy of scheduled or published videos is never changed.")
        self.overwrite.toggled.connect(self._replan)
        left.addWidget(self.overwrite)
        body.addLayout(left, 2)

        right = QVBoxLayout()
        right.addWidget(_section("PREVIEW", "What happens to each row of the sheet"))
        self.preview = table(["ROW", "VIDEO", "WILL SET", "NOTES"], fit=(0,))
        self.preview.setWordWrap(True)
        right.addWidget(self.preview, 1)
        body.addLayout(right, 3)
        lay.addLayout(body, 1)

        self.summary = _label(prop="muted")
        lay.addWidget(self.summary)
        self.error = _label(prop="danger")
        lay.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.import_btn = tip(QPushButton("Import"), "Write every changing row at once. Each change is logged in "
                                                     "the ticket's Change History.")
        set_prop(self.import_btn, "primary")
        self.import_btn.setEnabled(False)
        self.import_btn.clicked.connect(self._import)
        buttons.addButton(self.import_btn, QDialogButtonBox.AcceptRole)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    # ---------------------------------------------------------------- loading
    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a CSV File", "", "Spreadsheets (*.csv *.tsv *.txt)")
        if path:
            self.load_file(path)

    def load_file(self, path: str) -> None:
        try:
            headers, rows = read_file(path)
        except (SheetError, OSError) as exc:
            self.error.setText(str(exc))
            return
        self._loaded(headers, rows, path)

    def _load_link(self) -> None:
        link = self.link.text().strip()
        if not google_export_url(link):
            self.error.setText("Paste a Google Sheets link (docs.google.com/spreadsheets/…).")
            return
        if self.worker and self.worker.isRunning():
            return
        import requests
        session = self.session or requests.Session()
        self.error.setText("")
        self.source_label.setText("Loading the sheet from Google…")
        self.link_btn.setEnabled(False)
        self.worker = CallWorker(self._fetch, link, session, parent=self)
        self.worker.succeeded.connect(lambda result: self._loaded(*result, "Google Sheet"))
        self.worker.failed.connect(lambda msg: (self.error.setText(msg), self.source_label.setText("")))
        self.worker.finished.connect(lambda: self.link_btn.setEnabled(True))
        self.worker.start()

    @staticmethod
    def _fetch(link, session):
        return fetch_google_sheet(link, session)

    def _loaded(self, headers: list[str], rows: list[list[str]], source: str) -> None:
        from ..sheet_import import guess_mapping
        self.headers, self.rows = headers, rows
        self.error.setText("")
        self.source_label.setText(f"{source}: {len(rows)} rows, {len(headers)} columns.")
        mapping = guess_mapping(headers)
        self.mapping_table.setRowCount(len(headers))
        for i, header in enumerate(headers):
            sample = next((r[i] for r in rows if r[i].strip()), "")
            item = QTableWidgetItem(header + (f"\n“{short_title(sample, 40)}”" if sample else ""))
            item.setFlags(Qt.ItemIsEnabled)
            self.mapping_table.setItem(i, 0, item)
            combo = QComboBox()
            for key, label in TARGETS:
                combo.addItem(label, key)
            combo.setCurrentIndex(max(combo.findData(mapping.get(header)), 0))
            combo.currentIndexChanged.connect(self._replan)
            self.mapping_table.setCellWidget(i, 1, combo)
        self.mapping_table.resizeRowsToContents()
        self._replan()

    def mapping(self) -> dict[str, str | None]:
        return {h: self.mapping_table.cellWidget(i, 1).currentData() for i, h in enumerate(self.headers)}

    # ---------------------------------------------------------------- preview
    def _replan(self, *_args) -> None:
        if not self.headers:
            return
        mapping = self.mapping()
        targets = [t for t in mapping.values() if t]
        dupes = sorted({LABELS[t] for t in targets if targets.count(t) > 1})
        if dupes:
            self.error.setText(f"Two columns go to the same field: {', '.join(dupes)}. Pick one.")
            self.import_btn.setEnabled(False)
            return
        self.error.setText("")
        self.plan = self.pipeline.plan_sheet(self.profile.id, self.headers, self.rows, mapping,
                                             self.overwrite.isChecked())
        self.preview.setRowCount(len(self.plan.rows))
        for i, row in enumerate(self.plan.rows):
            video = f"{row.title}\n(by {row.matched_by})" if row.video_id else "No match"
            sets = ", ".join(LABELS.get(f, f) for f in row.fields) if row.fields and not row.problems else "—"
            notes = "\n".join(row.problems) or ("Kept: " + "; ".join(row.kept) if row.kept else "")
            for col, text in enumerate((str(row.number), video, sets, notes)):
                item = QTableWidgetItem(text)
                if row.problems and col in (1, 3):
                    item.setForeground(QColor(theme.P.danger))
                item.setToolTip(text)
                self.preview.setItem(i, col, item)
        self.preview.resizeRowsToContents()
        n = len(self.plan.changing)
        self.summary.setText(self.plan.summary())
        self.import_btn.setText(f"Import {n} Ticket{'s' if n != 1 else ''}" if n else "Import")
        self.import_btn.setEnabled(n > 0)

    def _import(self) -> None:
        if not self.plan or not self.plan.changing:
            return
        store = self.pipeline.store
        try:
            with store.origin("import"):
                self.applied = self.pipeline.apply_sheet(self.plan)
        except Exception as exc:  # noqa: BLE001 — the transaction rolled back; say so
            self.error.setText(f"Nothing was imported: {exc}")
            return
        self.accept()


# ====================================================================== archive
class ArchivedDialog(QDialog):
    def __init__(self, pipeline, profile, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Archived Videos")
        self.resize(640, 440)
        self.pipeline, self.profile = pipeline, profile
        self.restored = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 12)
        lay.addWidget(_label("Archived videos are hidden from the Board and the Library. Their files, "
                             "tickets and history are kept.", "muted"))
        self.table = table(["VIDEO", "ARCHIVED"], fit=(1,))
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.restore_btn = tip(QPushButton("Bring Back"), "Put the highlighted videos back where they were")
        set_prop(self.restore_btn, "primary")
        self.restore_btn.clicked.connect(self._restore)
        row.addWidget(self.restore_btn)
        row.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        self.refresh()

    def refresh(self) -> None:
        rows = self.pipeline.archived(self.profile.id)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            item = QTableWidgetItem(short_title(r.item.hook, 90))
            item.setData(Qt.UserRole, r.id)
            self.table.setItem(i, 0, item)
            self.table.setItem(i, 1, QTableWidgetItem(local_text(r.archived_at) or ""))
        self.restore_btn.setEnabled(bool(rows))

    def selected_ids(self) -> list[int]:
        return [self.table.item(i.row(), 0).data(Qt.UserRole) for i in self.table.selectionModel().selectedRows()]

    def _restore(self) -> None:
        ids = self.selected_ids()
        if ids:
            self.pipeline.archive(ids, archived=False)
            self.restored += len(ids)
            self.refresh()


# ====================================================================== columns
class ColumnsDialog(QDialog):
    """Rename, add, remove and reorder the Board's columns."""

    def __init__(self, columns, counts: dict[str, int], parent=None):
        super().__init__(parent)
        from dataclasses import replace

        from PySide6.QtWidgets import QListWidget
        self.setWindowTitle("Edit Columns")
        self.resize(520, 460)
        self.columns = [replace(c, stages=list(c.stages)) for c in columns]
        self.counts = counts
        self.moves: list[tuple[str, str]] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(10)
        lay.addWidget(_label("Name your columns however you like, drag to reorder, add your own or remove ones you "
                             "don't need. Reelay's own steps (Backlog, Staging, Scheduled, Published) always live in "
                             "some column: removing a column hands them to its neighbour, and cards keep moving "
                             "automatically when they're scheduled or posted.", "muted"))
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed |
                                  QAbstractItemView.SelectedClicked)
        tip(self.list, "Double-click a name to rename it. Drag to change the order.")
        self.list.itemChanged.connect(self._renamed)
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        add = tip(QPushButton("Add Column"), "Add your own column after the highlighted one")
        add.clicked.connect(self._add)
        remove = tip(QPushButton("Remove Column"), "Remove the highlighted column (its cards move to the neighbour)")
        remove.clicked.connect(self._remove)
        rename = tip(QPushButton("Rename"), "Rename the highlighted column")
        rename.clicked.connect(lambda: self.list.currentItem() and self.list.editItem(self.list.currentItem()))
        reset = tip(QPushButton("Reset to Default"), "Back to Backlog, Staging, Scheduled, Published")
        set_prop(reset, "flat")
        reset.clicked.connect(self._reset)
        for b in (add, remove, rename):
            row.addWidget(b)
        row.addStretch()
        row.addWidget(reset)
        lay.addLayout(row)
        self.error = _label(prop="danger")
        lay.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Save)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._fill()

    def _holds(self, col) -> str:
        from ..models import Stage
        steps = [Stage.LABELS[s] for s in col.builtin]
        n = sum(self.counts.get(s, 0) for s in col.stages)
        cards = f" · {n} card{'s' if n != 1 else ''}" if n else ""
        return (f"Holds Reelay's {', '.join(steps)} step{'s' if len(steps) != 1 else ''}" if steps
                else "Your own column") + cards

    def _fill(self, select: str | None = None) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for col in self.columns:
            item = QListWidgetItem(col.name)
            item.setData(Qt.UserRole, col.id)
            item.setToolTip(self._holds(col))
            item.setFlags(item.flags() | Qt.ItemIsEditable | Qt.ItemIsDragEnabled)
            self.list.addItem(item)
            hint = QListWidgetItem("      " + self._holds(col))
            hint.setFlags(Qt.NoItemFlags)
            hint.setData(Qt.UserRole, None)
            hint.setForeground(QColor(theme.P.text_muted))
            self.list.addItem(hint)
            if col.id == select:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)

    def _sync_order(self) -> None:
        order = [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]
        by_id = {c.id: c for c in self.columns}
        self.columns = [by_id[i] for i in order if i in by_id]

    def _current(self):
        item = self.list.currentItem()
        cid = item.data(Qt.UserRole) if item else None
        return next((c for c in self.columns if c.id == cid), None)

    def _renamed(self, item) -> None:
        cid = item.data(Qt.UserRole)
        col = next((c for c in self.columns if c.id == cid), None)
        if col is not None:
            col.name = item.text().strip() or col.name

    def _add(self) -> None:
        from ..columns import ColumnError, add
        self._sync_order()
        current = self._current()
        index = self.columns.index(current) + 1 if current else len(self.columns)
        try:
            col = add(self.columns, "New column", index)
        except ColumnError as exc:
            self.error.setText(str(exc))
            return
        self._fill(select=col.id)
        self.list.editItem(self.list.currentItem())

    def _remove(self) -> None:
        from ..columns import ColumnError, is_custom, remove
        self._sync_order()
        current = self._current()
        if current is None:
            self.error.setText("Highlight a column to remove.")
            return
        try:
            removed, neighbour = remove(self.columns, current.id)
        except ColumnError as exc:
            self.error.setText(str(exc))
            return
        self.moves += [(s, neighbour.primary) for s in removed.stages if is_custom(s)]
        self.error.setText("")
        self._fill(select=neighbour.id)

    def _reset(self) -> None:
        from ..columns import default_columns, is_custom
        self.moves += [(s, "staging") for c in self.columns for s in c.stages if is_custom(s)]
        self.columns = default_columns()
        self._fill()

    def _save(self) -> None:
        from ..columns import ColumnError, validate
        self._sync_order()
        try:
            validate(self.columns)
        except ColumnError as exc:
            self.error.setText(str(exc))
            return
        self.accept()

    def result_columns(self):
        return self.columns, self.moves
