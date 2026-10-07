"""Download page: fetch a profile → review/select/name → download → rename."""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QProgressBar,
                               QPushButton, QSplitter, QStackedWidget, QTableView, QToolButton,
                               QVBoxLayout, QWidget)

from .. import APP_NAME
from ..downloader import QUALITY_BEST, QUALITY_LABELS
from ..library import Library
from ..models import Profile, Status, VideoItem
from ..naming import DEFAULT_TEMPLATE, PRESETS, slugify
from ..platforms import is_supported_url
from ..store import Store
from ..ytdl import COOKIE_BROWSERS, base_options, find_ffmpeg
from .detail_panel import DetailPanel
from .dialogs import RenamePreviewDialog, tokens_help_html
from .table_model import ROW_HEIGHT, Col, CoverDelegate, VideoFilterProxy, VideoTableModel
from .theme import set_prop
from .thumbnails import ThumbnailCache
from .workers import DetailsWorker, DownloadWorker, FetchWorker

log = logging.getLogger("reelay")


def fmt_speed(bps) -> str:
    if not bps:
        return ""
    for unit, div in (("GB/s", 1e9), ("MB/s", 1e6), ("KB/s", 1e3)):
        if bps >= div:
            return f"{bps / div:.1f} {unit}"
    return f"{bps:.0f} B/s"


def fmt_eta(seconds) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d} left"


def muted(text: str, prop: str = "muted") -> QLabel:
    lab = QLabel(text)
    lab.setProperty(prop, True)
    return lab


class VideoTable(QTableView):
    """Table that toggles inclusion of the selected rows with Space."""

    def __init__(self, on_space, parent=None):
        super().__init__(parent)
        self._on_space = on_space

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Space and self.state() != QTableView.EditingState:
            self._on_space()
            return
        super().keyPressEvent(event)


class DownloadPage(QWidget):
    videosChanged = Signal()        # downloads finished / files renamed (board should reload)
    busyChanged = Signal(bool)

    def __init__(self, store: Store, thumbs: ThumbnailCache, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.store = store
        self.thumbs = thumbs
        self.profile: Profile | None = None
        self.library: Library | None = None
        self.model = VideoTableModel(thumbs, self)
        self.proxy = VideoFilterProxy(self)
        self.proxy.setSourceModel(self.model)

        self.fetched: list[VideoItem] = []
        self.sources: list[str] = []
        self.failures: list[tuple[str, str]] = []
        self.fetch_worker: FetchWorker | None = None
        self.details_worker: DetailsWorker | None = None
        self.download_worker: DownloadWorker | None = None
        self._dl_total = self._dl_index = 0
        self._loading_profile = False
        self._save_timer = QTimer(self, singleShot=True, interval=700, timeout=self._persist_names)

        self._build_ui()
        self._connect()
        self._unsubscribe = store.subscribe(self._on_store_change)

    # ================================================================ UI
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 12)
        outer.setSpacing(12)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.url = QLineEdit(objectName="UrlField")
        self.url.setPlaceholderText("Paste a TikTok, Instagram or YouTube profile link (or several video links)")
        self.url.setClearButtonEnabled(True)
        self.url.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.fetch_btn = QPushButton("Fetch Videos")
        set_prop(self.fetch_btn, "primary")
        self.fetch_btn.setMinimumHeight(38)
        row1.addWidget(self.url, 1)
        row1.addWidget(self.fetch_btn)
        outer.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row2.addWidget(muted("Save to"))
        self.folder_btn = QPushButton()
        set_prop(self.folder_btn, "flat")
        self.folder_btn.setToolTip("Choose where this profile's videos, metadata and manifest.md are saved")
        row2.addWidget(self.folder_btn)
        self.open_folder_btn = QPushButton("Open")
        row2.addWidget(self.open_folder_btn)
        row2.addStretch()
        row2.addWidget(muted("Quality"))
        self.quality = QComboBox()
        for key, label in QUALITY_LABELS.items():
            self.quality.addItem(label, key)
        row2.addWidget(self.quality)
        row2.addSpacing(10)
        row2.addWidget(muted("Login cookies"))
        self.cookies = QComboBox()
        self.cookies.addItem("None", "")
        for b in COOKIE_BROWSERS:
            self.cookies.addItem(b.capitalize(), b)
        self.cookies.setToolTip("Instagram usually needs this. Uses the login from that browser; "
                                "make sure you're signed in there.")
        row2.addWidget(self.cookies)
        outer.addLayout(row2)

        list_area = QWidget(objectName="ListArea")
        ll = QVBoxLayout(list_area)
        ll.setContentsMargins(12, 12, 12, 0)
        ll.setSpacing(10)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search captions and names")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(140)
        self.search.setMaximumWidth(220)
        self.search.setAttribute(Qt.WA_MacShowFocusRect, False)
        bar.addWidget(self.search)
        self.select_btn = QToolButton(text="Select  ▾")
        self.select_btn.setPopupMode(QToolButton.InstantPopup)
        bar.addWidget(self.select_btn)
        bar.addStretch()
        bar.addWidget(muted("Naming"))
        self.template = QComboBox()
        self.template.setEditable(True)
        self.template.setInsertPolicy(QComboBox.NoInsert)
        self.template.setMinimumWidth(190)
        for label, tmpl in PRESETS:
            self.template.addItem(tmpl)
            self.template.setItemData(self.template.count() - 1, label, Qt.ToolTipRole)
        self.template.setToolTip(tokens_help_html())
        self.template.lineEdit().setAttribute(Qt.WA_MacShowFocusRect, False)
        bar.addWidget(self.template)
        self.autoname_btn = QToolButton(text="Auto-Name  ▾")
        self.autoname_btn.setPopupMode(QToolButton.InstantPopup)
        bar.addWidget(self.autoname_btn)
        self.apply_btn = QPushButton("Apply Names")
        self.apply_btn.setToolTip("Rename videos you've already downloaded to match the naming template")
        bar.addWidget(self.apply_btn)
        ll.addLayout(bar)

        self.table = VideoTable(self._toggle_selected)
        self.table.setModel(self.proxy)
        self.table.setItemDelegateForColumn(Col.COVER, CoverDelegate(self.model, self.proxy, self.table))
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.ExtendedSelection)
        self.table.setEditTriggers(QTableView.DoubleClicked | QTableView.EditKeyPressed)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.setSortingEnabled(True)
        self.table.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.table.setVerticalScrollMode(QTableView.ScrollPerPixel)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hh.setStretchLastSection(False)
        # Fixed-width data columns; the three text columns share the rest.
        widths = {Col.CHECK: 34, Col.COVER: 50, Col.DATE: 86, Col.LENGTH: 58, Col.VIEWS: 62, Col.STATUS: 112}
        for col, w in widths.items():
            hh.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, w)
        hh.setSectionResizeMode(Col.FILENAME, QHeaderView.Interactive)
        self.table.setColumnWidth(Col.FILENAME, 150)
        hh.setContextMenuPolicy(Qt.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._column_menu)
        hidden = QSettings().value("hidden_columns", []) or []
        for col in hidden:
            if int(col) in (Col.LENGTH, Col.VIEWS, Col.CUSTOM, Col.FILENAME, Col.STATUS):
                self.table.setColumnHidden(int(col), True)
        for col in (Col.HOOK, Col.CUSTOM):
            hh.setSectionResizeMode(col, QHeaderView.Stretch)
        hh.setMinimumSectionSize(34)

        self.list_stack = QStackedWidget()
        self.list_stack.addWidget(self._build_empty_state())
        self.list_stack.addWidget(self.table)
        ll.addWidget(self.list_stack, 1)

        self.detail = DetailPanel(self.thumbs)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(list_area)
        split.addWidget(self.detail)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([1000, 300])
        split.setChildrenCollapsible(False)
        outer.addWidget(split, 1)

        bl = QVBoxLayout()
        bl.setSpacing(8)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1000)
        self.progress.setVisible(False)
        bl.addWidget(self.progress)
        brow = QHBoxLayout()
        self.status = muted("")
        brow.addWidget(self.status, 1)
        self.manifest_btn = QPushButton("Open Manifest")
        brow.addWidget(self.manifest_btn)
        brow.addSpacing(8)
        self.count_label = muted("")
        brow.addWidget(self.count_label)
        self.cancel_btn = QPushButton("Stop")
        set_prop(self.cancel_btn, "danger")
        self.cancel_btn.setVisible(False)
        brow.addWidget(self.cancel_btn)
        self.download_btn = QPushButton("Download")
        set_prop(self.download_btn, "primary")
        self.download_btn.setMinimumHeight(36)
        self.download_btn.setMinimumWidth(190)
        brow.addWidget(self.download_btn)
        bl.addLayout(brow)
        outer.addLayout(bl)

        sel = QMenu(self)
        sel.addAction("Include All", lambda: self._check_all(True))
        sel.addAction("Exclude All", lambda: self._check_all(False))
        sel.addSeparator()
        sel.addAction("Include Selected Rows", lambda: self._check_selected(True))
        sel.addAction("Exclude Selected Rows", lambda: self._check_selected(False))
        sel.addSeparator()
        sel.addAction("Include Only the Newest…", self._check_newest)
        sel.addAction("Include Only Search Results", self._check_visible)
        self.select_btn.setMenu(sel)

        auto = QMenu(self)
        auto.addAction("Name Selected from Captions", lambda: self._auto_name(selected_only=True))
        auto.addAction("Name All Unnamed from Captions", lambda: self._auto_name(selected_only=False))
        auto.addSeparator()
        auto.addAction("Clear Names of Selected", self._clear_names)
        auto.addSeparator()
        auto.addAction("Naming Tokens Help", self.show_tokens_help)
        self.autoname_btn.setMenu(auto)

    def _column_menu(self, pos) -> None:
        """Right-click the header to show or hide optional columns."""
        menu = QMenu(self)
        for col, label in ((Col.LENGTH, "Length"), (Col.VIEWS, "Views"), (Col.CUSTOM, "Custom Name"),
                           (Col.FILENAME, "File Name"), (Col.STATUS, "Status")):
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(not self.table.isColumnHidden(col))
            act.toggled.connect(lambda on, c=col: self._set_column_visible(c, on))
        menu.exec(self.table.horizontalHeader().mapToGlobal(pos))

    def _set_column_visible(self, col: int, visible: bool) -> None:
        self.table.setColumnHidden(col, not visible)
        hidden = [c for c in range(len(Col)) if self.table.isColumnHidden(c)]
        QSettings().setValue("hidden_columns", hidden)

    def _build_empty_state(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addStretch()
        t = QLabel("Start with a profile link", objectName="EmptyTitle")
        t.setAlignment(Qt.AlignCenter)
        s = muted("Paste a creator's TikTok, Instagram or YouTube link above and click Fetch Videos.\n"
                  "You'll see every video here first, then choose which ones to download and how to name them.")
        s.setAlignment(Qt.AlignCenter)
        s.setWordWrap(True)
        lay.addWidget(t)
        lay.addSpacing(6)
        lay.addWidget(s)
        lay.addStretch()
        return w

    def _connect(self) -> None:
        self.fetch_btn.clicked.connect(self._fetch)
        self.url.returnPressed.connect(self._fetch)
        self.folder_btn.clicked.connect(self.choose_folder)
        self.open_folder_btn.clicked.connect(self.open_output_folder)
        self.manifest_btn.clicked.connect(self.open_manifest)
        self.download_btn.clicked.connect(self._start_download)
        self.cancel_btn.clicked.connect(self._cancel_download)
        self.apply_btn.clicked.connect(self._apply_names)
        self.search.textChanged.connect(self.proxy.set_query)
        self.template.editTextChanged.connect(self._on_template)
        self.quality.currentIndexChanged.connect(self._save_profile_prefs)
        self.cookies.currentIndexChanged.connect(self._save_profile_prefs)
        self.model.checksChanged.connect(self._update_counts)
        self.model.customNameEdited.connect(lambda _key: self._names_edited())
        self.model.namesChanged.connect(self._refresh_detail)
        self.table.selectionModel().selectionChanged.connect(self._refresh_detail)
        self.detail.customNameEdited.connect(self._on_detail_custom)
        self.detail.checkSelected.connect(self._check_selected)
        self.detail.autoNameSelected.connect(lambda: self._auto_name(selected_only=True))

    def _on_store_change(self, change) -> None:
        """Pick up custom-name edits made outside this page (e.g. the companion
        app). This page's own writes are "local" and already on screen."""
        if change.origin == "local" or change.entity != "video" or not self.profile \
                or change.profile_id != self.profile.id:
            return
        for row_id in change.ids:
            row = self.store.get_video(row_id)
            item = self.model.find(row.item.key) if row else None
            if item is not None and item.custom_name != row.item.custom_name:
                item.custom_name = row.item.custom_name
                self.model.refresh(item.key)
        self.model.refresh_all()
        self._refresh_detail()

    # ================================================================ profile
    @property
    def folder(self) -> Path:
        return Path(self.profile.output_folder)

    def set_profile(self, profile: Profile) -> None:
        if self.busy:
            return
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._persist_names()
        self._stop_details()
        self._loading_profile = True
        self.profile = profile
        self.fetched, self.sources, self.failures = [], [], []
        self.search.clear()
        idx = self.quality.findData(profile.quality or QUALITY_BEST)
        self.quality.setCurrentIndex(max(idx, 0))
        idx = self.cookies.findData(profile.cookies_browser)
        self.cookies.setCurrentIndex(max(idx, 0))
        self.template.setEditText(profile.template or DEFAULT_TEMPLATE)
        self.model.set_template(profile.template or DEFAULT_TEMPLATE)
        self.url.setText(profile.last_source)
        self._loading_profile = False
        self._open_folder(self.folder)
        self._set_status("")
        self._start_details()

    def _save_profile_prefs(self) -> None:
        if self._loading_profile or not self.profile:
            return
        self.profile.quality = self.quality.currentData()
        self.profile.cookies_browser = self.cookies.currentData() or ""
        self.profile.template = self.template.currentText().strip() or DEFAULT_TEMPLATE
        self.store.update_profile(self.profile)

    # ================================================================ helpers
    @property
    def busy(self) -> bool:
        return bool(self.download_worker and self.download_worker.isRunning())

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    def _opts(self) -> dict:
        return base_options(self.cookies.currentData() or None)

    def _selected_items(self) -> list[VideoItem]:
        rows = {self.proxy.mapToSource(i).row() for i in self.table.selectionModel().selectedRows()}
        return [self.model.item(r) for r in sorted(rows)]

    def _in_folder(self, item: VideoItem) -> bool:
        return Path(item.folder or self.folder) == self.folder

    def _set_downloading(self, on: bool) -> None:
        for w in (self.fetch_btn, self.url, self.folder_btn, self.quality, self.cookies, self.apply_btn):
            w.setEnabled(not on)
        self.download_btn.setVisible(not on)
        self.cancel_btn.setVisible(on)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("Stop")
        self.progress.setVisible(on)
        self.busyChanged.emit(on)

    def _update_counts(self) -> None:
        items = self.model.items()
        self.list_stack.setCurrentIndex(1 if items else 0)
        checked = len(self.model.checked_items())
        done = sum(1 for i in items if i.is_downloaded)
        parts = [f"{checked} of {len(items) - done} selected"]
        if done:
            parts.append(f"{done} downloaded")
        self.count_label.setText(" · ".join(parts) if items else "")
        self.download_btn.setText(f"Download {checked} Video{'s' if checked != 1 else ''}" if checked else "Download")
        self.download_btn.setEnabled(checked > 0 and not self.busy)
        self.apply_btn.setEnabled(done > 0 and not self.busy)

    def _refresh_detail(self, *_args) -> None:
        items = self._selected_items()
        if len(items) == 1:
            self.detail.show_item(items[0], self.model.planned_filename(items[0]))
        elif len(items) > 1:
            self.detail.show_multi(items)
        else:
            self.detail.show_empty()

    def show_tokens_help(self) -> None:
        QMessageBox.information(self, "Naming Tokens", tokens_help_html())

    # ================================================================ folder
    def choose_folder(self) -> None:
        if self.busy or not self.profile:
            return
        chosen = QFileDialog.getExistingDirectory(self, "Choose Output Folder", str(self.folder))
        if chosen:
            self.profile.output_folder = chosen
            self.store.update_profile(self.profile)
            self._open_folder(Path(chosen))

    def _open_folder(self, folder: Path) -> None:
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, APP_NAME, f"Can't use the folder {folder}:\n{exc}")
        self.library = Library(folder)
        self.thumbs.folder = folder
        self.detail.folder = folder
        self.folder_btn.setText(self.folder_btn.fontMetrics().elidedText(
            self._short_path(folder), Qt.ElideMiddle, 380))
        self.folder_btn.setToolTip(str(folder))
        self.reload()

    @staticmethod
    def _short_path(folder: Path) -> str:
        try:
            return "~/" + str(folder.relative_to(Path.home()))
        except ValueError:
            return str(folder)

    def reload(self) -> None:
        """Merge the profile's saved videos, files found in the folder (first
        run / older versions) and the latest fetch."""
        if not self.profile or not self.library:
            return
        pid = self.profile.id
        by_key: dict[str, VideoItem] = {}
        for row in self.store.load_videos(pid):
            row.item.refresh_download_status(self.folder)
            by_key[row.item.key] = row.item

        imported = []
        names = self.library.load_custom_names()
        for item in self.library.load():
            if item.key not in by_key:
                item.folder = str(self.folder)
                item.custom_name = item.custom_name or names.get(item.key, "")
                by_key[item.key] = item
                imported.append(item)
        if imported:
            self.store.save_videos(pid, imported)

        merged: list[VideoItem] = []
        for fetched in self.fetched:
            existing = by_key.pop(fetched.key, None)
            if existing is not None:
                existing.merge_metadata(fetched)
                merged.append(existing)
            else:
                merged.append(replace(fetched))
        merged.extend(sorted(by_key.values(), key=lambda i: i.upload_date, reverse=True))
        self.model.set_items(merged)
        self.table.sortByColumn(Col.DATE, Qt.DescendingOrder)
        self._update_counts()
        self._refresh_detail()

    def open_output_folder(self) -> None:
        if self.profile:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.folder)))

    def open_manifest(self) -> None:
        if not self.library:
            return
        if not self.library.manifest_path.is_file():
            self._write_manifest()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.library.manifest_path)))

    def _write_manifest(self) -> None:
        try:
            self.library.write_manifest([i for i in self.model.items() if self._in_folder(i)],
                                        self.sources, self.failures)
        except OSError as exc:
            log.warning("Couldn't write manifest: %s", exc)

    # ================================================================ fetch
    def _fetch(self) -> None:
        if (self.fetch_worker and self.fetch_worker.isRunning()) or not self.profile:
            return
        urls = [u for u in re.split(r"[\s,]+", self.url.text().strip()) if u]
        if not urls:
            self.url.setFocus()
            return
        urls = [u if u.startswith("http") else "https://" + u for u in urls]
        bad = [u for u in urls if not is_supported_url(u)]
        if bad:
            QMessageBox.warning(self, APP_NAME, "These links aren't TikTok, Instagram or YouTube:\n\n" + "\n".join(bad))
            return
        self.profile.last_source = self.url.text().strip()
        self.store.update_profile(self.profile)
        self._stop_details()
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText("Fetching…")
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        self._set_status("Fetching video list…")
        self.fetch_worker = FetchWorker(urls, self._opts(), self)
        self.fetch_worker.sourceStarted.connect(lambda u: self._set_status(f"Fetching video list from {u} …"))
        self.fetch_worker.fetched.connect(lambda items, errs: self._on_fetched(urls, items, errs))
        self.fetch_worker.start()

    def _on_fetched(self, urls: list[str], items: list[VideoItem], errors: list[tuple[str, str]]) -> None:
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("Fetch Videos")
        self.progress.setRange(0, 1000)
        self.progress.setVisible(False)
        if errors:
            self.failures.extend(errors)
            QMessageBox.warning(self, "Couldn't fetch everything",
                                "\n\n".join(f"{u}\n{reason}" for u, reason in errors))
        if not items:
            self._set_status("No videos found." if not errors else "Fetch failed.")
            return
        self.fetched = items
        self.sources = urls
        self.reload()
        self.store.save_videos(self.profile.id, self.model.items())
        new = sum(1 for i in self.model.items() if not i.is_downloaded)
        self._set_status(f"Found {len(items)} videos · {new} not downloaded yet. "
                         "Choose which to include, name them, then click Download.")
        self._start_details()

    def _start_details(self) -> None:
        needing = [i for i in self.model.items() if not i.has_details and not i.is_downloaded]
        if not needing or (self.details_worker and self.details_worker.isRunning()):
            return
        self.details_worker = DetailsWorker(needing, self._opts(), self)
        self.details_worker.loaded.connect(self._on_details)
        self.details_worker.start()

    def _stop_details(self) -> None:
        if self.details_worker and self.details_worker.isRunning():
            self.details_worker.stop()

    def _on_details(self, key: str, fresh: VideoItem) -> None:
        item = self.model.find(key)
        if item is None or not self.profile:
            return
        item.merge_metadata(fresh)
        self.store.save_video(self.profile.id, item)
        self.model.refresh_all()
        if self.detail.item is not None and self.detail.item.key == key:
            self._refresh_detail()

    # ================================================================ selection & names
    def _check_all(self, on: bool) -> None:
        self.model.set_checked([i.key for i in self.model.items()], on)

    def _check_selected(self, on: bool) -> None:
        self.model.set_checked([i.key for i in self._selected_items()], on)

    def _toggle_selected(self) -> None:
        items = [i for i in self._selected_items() if not i.is_downloaded]
        if items:
            self.model.set_checked([i.key for i in items], not all(i.checked for i in items))

    def _check_newest(self) -> None:
        pending = sorted((i for i in self.model.items() if not i.is_downloaded),
                         key=lambda i: i.upload_date, reverse=True)
        if not pending:
            return
        n, ok = QInputDialog.getInt(self, "Include Newest", "How many of the newest videos?",
                                    min(10, len(pending)), 1, len(pending))
        if ok:
            keep = {i.key for i in pending[:n]}
            self.model.set_checked([i.key for i in pending if i.key in keep], True)
            self.model.set_checked([i.key for i in pending if i.key not in keep], False)

    def _check_visible(self) -> None:
        visible = {self.model.item(self.proxy.mapToSource(self.proxy.index(r, 0)).row()).key
                   for r in range(self.proxy.rowCount())}
        self.model.set_checked(list(visible), True)
        self.model.set_checked([i.key for i in self.model.items() if i.key not in visible], False)

    def _auto_name(self, selected_only: bool) -> None:
        targets = self._selected_items() if selected_only else [i for i in self.model.items() if not i.custom_name]
        if not targets:
            self._set_status("Select one or more videos first." if selected_only else "Every video already has a name.")
            return
        self.model.set_custom_names({i.key: slugify(i.hook) for i in targets if slugify(i.hook)})
        if "{custom}" not in self.template.currentText():
            self._set_status(f"Named {len(targets)} videos. Switch Naming to a template with {{custom}}, "
                             "like {date}_{custom}, to use these names in the filenames.")
        else:
            self._set_status(f"Named {len(targets)} videos from their captions.")

    def _clear_names(self) -> None:
        self.model.set_custom_names({i.key: "" for i in self._selected_items()})

    def _on_detail_custom(self, key: str, text: str) -> None:
        item = self.model.find(key)
        if item is None:
            return
        item.custom_name = text.strip()
        self.model._names_changed()
        self.detail.update_filename(self.model.planned_filename(item))
        self._names_edited()

    def _on_template(self, text: str) -> None:
        self.model.set_template(text.strip())
        self._save_profile_prefs()
        self._push_naming_to_worker()

    def _names_edited(self) -> None:
        self._push_naming_to_worker()
        self._save_timer.start()

    def _push_naming_to_worker(self) -> None:
        if self.busy:
            self.download_worker.set_naming(self.model.template,
                                            {i.key: i.custom_name for i in self.model.items()})

    def _persist_names(self) -> None:
        if not self.profile or not self.library:
            return
        try:
            self.store.save_videos(self.profile.id, self.model.items())
            for item in self.model.items():
                if item.is_downloaded and self._in_folder(item):
                    self.library.save(item)
            self._write_manifest()
        except OSError as exc:
            log.warning("Couldn't save names: %s", exc)
            self._set_status(f"Couldn't save names: {exc}")

    # ================================================================ rename on disk
    def _apply_names(self) -> None:
        if not self.library or self.busy:
            return
        here = [i for i in self.model.items() if self._in_folder(i)]
        steps = self.library.plan_renames(here, self.model.template)
        if not steps:
            QMessageBox.information(self, APP_NAME, "All downloaded files already match the naming template.")
            return
        if RenamePreviewDialog(steps, self).exec() != RenamePreviewDialog.Accepted:
            return
        errors = self.library.apply_renames(steps)
        self.store.save_videos(self.profile.id, [s.item for s in steps])
        self._write_manifest()
        self.model.refresh_all()
        self._refresh_detail()
        self.videosChanged.emit()
        renamed = len(steps) - len(errors)
        self._set_status(f"Renamed {renamed} file{'s' if renamed != 1 else ''}.")
        if errors:
            QMessageBox.warning(self, "Some files weren't renamed", "\n".join(errors[:30]))

    # ================================================================ download
    def _start_download(self) -> None:
        if self.busy or not self.library:
            return
        if not find_ffmpeg():
            self._set_status("ffmpeg wasn't found, so YouTube/Instagram videos can't be merged.")
        queue = [i for i in self.model.items() if i.checked and not i.is_downloaded]
        if not queue:
            return
        self._stop_details()
        self.library.ensure()
        positions = {i.key: n for n, i in enumerate(self.model.items(), 1)}
        for item in queue:
            item.status, item.progress, item.error = Status.QUEUED, 0.0, ""
        self.model.refresh_all()
        self._dl_total, self._dl_index = len(queue), 0
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self._set_downloading(True)
        w = DownloadWorker(queue, self.folder, self.quality.currentData(), self._opts(),
                           self.model.template, positions, parent=self)
        w.itemStarted.connect(self._on_item_started)
        w.itemProgress.connect(self._on_item_progress)
        w.itemDone.connect(self._on_item_done)
        w.itemFailed.connect(self._on_item_failed)
        w.batchDone.connect(self._on_batch_done)
        self.download_worker = w
        w.start()
        self._update_counts()

    def _cancel_download(self) -> None:
        if self.busy:
            self.download_worker.cancel()
            self.cancel_btn.setText("Stopping…")
            self.cancel_btn.setEnabled(False)
            self._set_status("Stopping after the current step…")

    def _on_item_started(self, key: str, n: int, total: int) -> None:
        self._dl_index = n
        item = self.model.find(key)
        if item:
            item.status, item.progress = Status.DOWNLOADING, 0.0
            self.model.refresh(key)
        self._set_status(f"Downloading {n} of {total}…")
        self.progress.setValue(int((n - 1) / total * 1000))

    def _on_item_progress(self, key: str, p) -> None:
        item = self.model.find(key)
        if item is None:
            return
        pct = p.percent or 0.0
        item.progress = pct
        self.model.refresh(key, (Col.STATUS,))
        n, total = self._dl_index, self._dl_total
        self.progress.setValue(int(((n - 1) + pct / 100) / max(total, 1) * 1000))
        if p.stage == "Finalizing":
            self._set_status(f"Downloading {n} of {total} · finalizing file…")
        else:
            extra = " · ".join(x for x in (f"{pct:.0f}%", fmt_speed(p.speed), fmt_eta(p.eta)) if x)
            self._set_status(f"Downloading {n} of {total} · {extra}")

    def _on_item_done(self, key: str, done: VideoItem) -> None:
        item = self.model.find(key)
        if item is None or not self.library or not self.profile:
            return
        custom = item.custom_name  # may have been edited mid-download
        item.merge_metadata(done)
        item.filename, item.format_id, item.folder = done.filename, done.format_id, str(self.folder)
        item.watermark_free, item.downloaded_at = done.watermark_free, done.downloaded_at
        item.status, item.progress, item.checked, item.custom_name = Status.DOWNLOADED, 100.0, False, custom
        try:
            self.store.save_video(self.profile.id, item)
            self.library.save(item)
            self._write_manifest()
        except OSError as exc:
            log.warning("Couldn't write metadata: %s", exc)
            self._set_status(f"Couldn't write metadata: {exc}")
        self.thumbs.retry(key)
        self.model.refresh_all()
        if self.detail.item is not None and self.detail.item.key == key:
            self._refresh_detail()

    def _on_item_failed(self, key: str, reason: str) -> None:
        item = self.model.find(key)
        if item is None:
            return
        item.status, item.error, item.progress = Status.FAILED, reason, 0.0
        self.failures.append((item.url, reason))
        self.model.refresh(key)
        self._write_manifest()
        if self.detail.item is not None and self.detail.item.key == key:
            self._refresh_detail()

    def _on_batch_done(self, ok: int, failed: int, cancelled: bool) -> None:
        for item in self.model.items():
            if item.status in (Status.QUEUED, Status.DOWNLOADING):
                item.status, item.progress = Status.NEW, 0.0
        self.download_worker = None
        self._set_downloading(False)
        self.model.refresh_all()
        self._update_counts()
        msg = f"{'Stopped' if cancelled else 'Done'}: {ok} downloaded"
        if failed:
            msg += f", {failed} failed (hover the Status column for the reason)"
        self._set_status(msg + ". Downloaded videos are in the Board's Backlog.")
        self.videosChanged.emit()
        self._start_details()
        QApplication.alert(self.window())

    # ================================================================ shutdown
    def shutdown(self, timeout_ms: int = 8000) -> None:
        self._unsubscribe()
        if self.busy:
            self.download_worker.cancel()
            self.download_worker.wait(timeout_ms)
        self._stop_details()
        if self.details_worker:
            self.details_worker.wait(3000)
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._persist_names()
