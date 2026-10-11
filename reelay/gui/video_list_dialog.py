"""Downloaded Videos: every downloaded video of a profile in one searchable, sortable table, inside Reelay.

Replaces opening manifest.md in a text editor. From here you can show a file in Finder, copy the original
post's link, play it, and export the list as CSV or as the Markdown manifest.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from ..handoff import reveal_in_file_manager
from ..manifest import write_manifest
from ..models import VideoItem
from . import icons
from .logos import platform_icon
from .table_model import fmt_count, fmt_date, fmt_duration
from .theme import set_prop
from .tips import tip

REVEAL = "Show in Finder" if sys.platform == "darwin" else "Show in Explorer"
THUMB_W, THUMB_H = 36, 64
COLUMNS = ("DATE", "", "TITLE", "PLATFORM", "LENGTH", "VIEWS", "SIZE", "FILE NAME")
DATE, THUMB, TITLE, PLATFORM, LENGTH, VIEWS, SIZE, FILE = range(len(COLUMNS))


class _Item(QTableWidgetItem):
    """Sorts by a hidden key (dates, numbers) instead of the text shown."""

    def __init__(self, text: str, key=None):
        super().__init__(text)
        self.key = text.lower() if key is None else key
        self.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)

    def __lt__(self, other) -> bool:
        try:
            return self.key < other.key
        except TypeError:
            return str(self.key) < str(other.key)


def file_size_text(n: int) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.2f} GB"
    if n >= 1e6:
        return f"{n / 1e6:.1f} MB"
    return f"{n / 1e3:.0f} KB" if n else "—"


class VideoListDialog(QDialog):
    def __init__(self, items: list[VideoItem], folder: Path, thumbs=None, manifest_path: Path | None = None,
                 sources=(), failures=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle("Downloaded Videos")
        self.resize(1080, 660)
        self.folder, self.thumbs = Path(folder), thumbs
        self.manifest_path = manifest_path or self.folder / "manifest.md"
        self.sources, self.failures = list(sources), list(failures)
        self.items = [i for i in items if i.is_downloaded]
        self._popout = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 14)
        lay.setSpacing(10)
        head = QHBoxLayout()
        title = QLabel("Downloaded Videos", objectName="DetailTitle")
        head.addWidget(title)
        self.count = QLabel(objectName="PageCount")
        head.addWidget(self.count)
        head.addStretch()
        self.search = QLineEdit(objectName="SearchField")
        self.search.setPlaceholderText("Search titles, names and files")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(300)
        self.search.addAction(icons.icon("search", 16), QLineEdit.LeadingPosition)
        tip(self.search, "Show only videos whose title, your name for it, platform or file name match")
        self.search.textChanged.connect(self._filter)
        head.addWidget(self.search)
        lay.addLayout(head)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(THUMB_H + 8)
        self.table.setIconSize(QSize(THUMB_W, THUMB_H))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setAttribute(Qt.WA_MacShowFocusRect, False)
        hh = self.table.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hh.setHighlightSections(False)
        for col, width in ((DATE, 104), (THUMB, THUMB_W + 14), (PLATFORM, 120), (LENGTH, 76), (VIEWS, 72),
                           (SIZE, 84)):
            hh.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, width)
        hh.setSectionResizeMode(TITLE, QHeaderView.Stretch)
        hh.setSectionResizeMode(FILE, QHeaderView.Interactive)
        self.table.setColumnWidth(FILE, 230)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        self.table.itemDoubleClicked.connect(lambda _item: self.play())
        lay.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.reveal_btn = tip(QPushButton(REVEAL), "Show the selected video's file in the folder")
        self.reveal_btn.clicked.connect(self.reveal)
        self.copy_btn = tip(QPushButton("Copy Video Link"), "Copy the link to the original post(s)")
        self.copy_btn.clicked.connect(self.copy_links)
        self.play_btn = tip(QPushButton("Play Preview"), "Watch the selected video in its own window")
        set_prop(self.play_btn, "primary")
        self.play_btn.clicked.connect(self.play)
        for b in (self.reveal_btn, self.copy_btn, self.play_btn):
            actions.addWidget(b)
        actions.addStretch()
        self.csv_btn = tip(QPushButton("Export as CSV…"), "Save this list as a spreadsheet file")
        self.csv_btn.clicked.connect(lambda: self.export_csv())
        self.md_btn = tip(QPushButton("Export Markdown Manifest…"),
                          "Save the list as manifest.md (the table Reelay keeps in the video folder)")
        self.md_btn.clicked.connect(lambda: self.export_markdown())
        actions.addWidget(self.csv_btn)
        actions.addWidget(self.md_btn)
        lay.addLayout(actions)
        self.status = QLabel()
        self.status.setProperty("faint", True)
        lay.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        tip(buttons.button(QDialogButtonBox.Close), "Close this list")
        lay.addWidget(buttons)

        self._fill()
        if thumbs is not None:
            thumbs.loaded.connect(self._cover_loaded)

    # ------------------------------------------------------------ data
    def _path(self, item: VideoItem) -> Path | None:
        return item.path(self.folder)

    def _size(self, item: VideoItem) -> int:
        path = self._path(item)
        try:
            return path.stat().st_size if path else 0
        except OSError:
            return 0

    def _fill(self) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.items))
        for row, item in enumerate(self.items):
            size = self._size(item)
            cells = {
                DATE: _Item(fmt_date(item.upload_date), item.upload_date or ""),
                THUMB: _Item("", 0),
                TITLE: _Item(item.custom_name or item.hook),
                PLATFORM: _Item(item.platform),
                LENGTH: _Item(fmt_duration(item.duration), float(item.duration or 0)),
                VIEWS: _Item(fmt_count(item.view_count), item.view_count or 0),
                SIZE: _Item(file_size_text(size), size),
                FILE: _Item(item.filename),
            }
            cells[DATE].setData(Qt.UserRole, row)              # which item this row is, after sorting
            cells[TITLE].setToolTip(item.hook)
            cells[PLATFORM].setIcon(platform_icon(item.platform.lower(), 16))
            cells[FILE].setToolTip(str(self._path(item) or item.filename))
            for col in (LENGTH, VIEWS, SIZE):
                cells[col].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._set_thumb(cells[THUMB], item)
            for col, cell in cells.items():
                self.table.setItem(row, col, cell)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(DATE, Qt.DescendingOrder)
        self._filter()
        self._sync_buttons()

    def _set_thumb(self, cell: QTableWidgetItem, item: VideoItem) -> None:
        cover = self.thumbs.thumb(item, THUMB_W, THUMB_H, self.devicePixelRatioF()) if self.thumbs else None
        if cover is not None:
            cell.setIcon(QIcon(cover))

    def _cover_loaded(self, key: str) -> None:
        for row in range(self.table.rowCount()):
            item = self.item_at(row)
            if item.key == key:
                self._set_thumb(self.table.item(row, THUMB), item)

    def item_at(self, row: int) -> VideoItem:
        return self.items[self.table.item(row, DATE).data(Qt.UserRole)]

    def selected(self) -> list[VideoItem]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.item_at(r) for r in rows]

    def _filter(self) -> None:
        words = self.search.text().lower().split()
        shown = 0
        for row in range(self.table.rowCount()):
            item = self.item_at(row)
            hay = " ".join([item.hook, item.custom_name, item.filename, item.platform]).lower()
            hide = bool(words) and not all(w in hay for w in words)
            self.table.setRowHidden(row, hide)
            shown += not hide
        total = len(self.items)
        self.count.setText(f"{total} video{'s' if total != 1 else ''}" + (f" · {shown} shown" if words else ""))

    def _sync_buttons(self) -> None:
        chosen = self.selected()
        self.reveal_btn.setEnabled(len(chosen) == 1)
        self.play_btn.setEnabled(len(chosen) == 1)
        self.copy_btn.setEnabled(any(i.url for i in chosen))

    # ------------------------------------------------------------ actions
    def reveal(self) -> None:
        chosen = self.selected()
        if chosen and not reveal_in_file_manager(self._path(chosen[0]) or self.folder):
            self.status.setText("That file isn't in the folder any more.")

    def copy_links(self) -> None:
        links = [i.url for i in self.selected() if i.url]
        if links:
            QApplication.clipboard().setText("\n".join(links))
            self.status.setText(f"Copied {len(links)} link{'s' if len(links) != 1 else ''}.")

    def play(self) -> None:
        chosen = self.selected()
        path = self._path(chosen[0]) if chosen else None
        if not path or not path.is_file():
            self.status.setText("That file isn't in the folder any more.")
            return
        from .player import PopoutPlayer
        cover = self.thumbs.cover(chosen[0]) if self.thumbs else None
        self._popout = PopoutPlayer(path, cover or QPixmap(), 0, self)
        self._popout.show()

    def visible_items(self) -> list[VideoItem]:
        return [self.item_at(r) for r in range(self.table.rowCount()) if not self.table.isRowHidden(r)]

    def export_csv(self, path: str | None = None) -> Path | None:
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "Export as CSV", str(self.folder / "videos.csv"),
                                                  "CSV files (*.csv)")
        if not path:
            return None
        target = Path(path)
        try:
            with target.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["Date", "Title", "Your name", "Platform", "Length (seconds)", "Views",
                                 "Size (bytes)", "File name", "Original link"])
                for item in self.visible_items():
                    writer.writerow([item.upload_date, item.hook, item.custom_name, item.platform,
                                     int(item.duration or 0), item.view_count or "", self._size(item), item.filename,
                                     item.url])
        except OSError as exc:
            QMessageBox.warning(self, "Export as CSV", f"Couldn't save the file: {exc}")
            return None
        self.status.setText(f"Saved {target.name}.")
        return target

    def export_markdown(self, path: str | None = None) -> Path | None:
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "Export Markdown Manifest", str(self.manifest_path),
                                                  "Markdown (*.md)")
        if not path:
            return None
        target = Path(path)
        try:
            write_manifest(target, self.items, self.sources, self.failures)
        except OSError as exc:
            QMessageBox.warning(self, "Export Markdown Manifest", f"Couldn't save the file: {exc}")
            return None
        self.status.setText(f"Saved {target.name}.")
        return target
