"""Download page: fetch a profile → review/select/name → download → rename."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu,
                               QMessageBox, QProgressBar, QPushButton, QScrollArea, QSplitter, QStackedWidget,
                               QTableView, QToolButton, QVBoxLayout, QWidget)

from .. import APP_NAME
from ..downloader import QUALITY_BEST, QUALITY_LABELS
from ..library import Library
from ..models import Profile, Status, VideoItem
from ..naming import DEFAULT_TEMPLATE, slugify
from ..accounts import AccountService
from ..platforms import is_supported_url
from ..sources import (SourceError, SourceService, SourceView, channel_url, default_name, downloadable_by_default,
                       normalize_url, youtube_items)
from ..store import Store
from ..tokens import MemoryTokenStore
from ..youtube import YouTubeError
from ..ytdl import COOKIE_BROWSERS, base_options, find_ffmpeg
from .bridge import change_bridge
from .detail_panel import DetailPanel
from .dialogs import RenamePreviewDialog, tokens_help_html
from .table_model import ROW_HEIGHT, Col, CoverDelegate, VideoFilterProxy, VideoTableModel
from .theme import set_prop
from .tips import tip
from .widgets import GrowingSearch, NamingPicker
from .thumbnails import ThumbnailCache
from .workers import CallWorker, DetailsWorker, DownloadWorker, FetchWorker, ProductsWorker

log = logging.getLogger("reelay")
OWN_CONTENT = "Own content"


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


class SourceChip(QFrame):
    """One source under "Bring videos in": name, platform, one-click List Videos, ⋯ menu."""
    fetchRequested = Signal(object)
    renameRequested = Signal(object)
    mineToggled = Signal(object)
    removeRequested = Signal(object)
    manageRequested = Signal()

    def __init__(self, view: SourceView, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.view = view
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 4, 4)
        lay.setSpacing(6)
        platform = {"tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube"}.get(view.platform, view.platform)
        tag = "connected" if view.account else ("mine" if view.mine else "")
        text = QLabel(f"<b>{html.escape(view.name)}</b>&nbsp;&nbsp;<span style='opacity:.6'>{platform}"
                      f"{' · ' + tag if tag else ''}</span>")
        text.setToolTip((view.url or "") + (f"\nSaved {view.duplicates + 1} times" if view.duplicates else ""))
        lay.addWidget(text)
        self.fetch_btn = QPushButton("List Videos")
        tip(self.fetch_btn, f"Show every video from {view.name} below, so you can choose which to download. "
                            "Nothing is downloaded yet." + ("" if view.account else f"\nLink: {view.url}"))
        self.fetch_btn.clicked.connect(lambda: self.fetchRequested.emit(self.view))
        lay.addWidget(self.fetch_btn)
        more = QToolButton(text="⋯")
        more.setPopupMode(QToolButton.InstantPopup)
        tip(more, "More for this source: rename it, mark it as yours, or remove it")
        menu = QMenu(more)
        if view.account:
            menu.addAction("Manage This Channel in Accounts", self.manageRequested.emit)
        else:
            menu.addAction("Rename This Source…", lambda: self.renameRequested.emit(self.view))
            menu.addAction("Not one of my accounts" if view.mine else "This is one of my accounts",
                           lambda: self.mineToggled.emit(self.view))
            menu.addSeparator()
            menu.addAction("Remove This Source", lambda: self.removeRequested.emit(self.view))
        more.setMenu(menu)
        lay.addWidget(more)

    def set_busy(self, busy: bool) -> None:
        self.fetch_btn.setEnabled(not busy)


class SourceDialog(QDialog):
    """Name a pasted link and say whether it's one of the profile's own accounts."""

    def __init__(self, default: str, profile_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Save This Link")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 14)
        lay.addWidget(muted("Name it so you'll recognize it (it shows as a button on the Download page)"))
        self.name_edit = QLineEdit(default)
        self.name_edit.selectAll()
        lay.addWidget(self.name_edit)
        self.mine_box = QCheckBox(f"This is one of {profile_name}'s own accounts")
        lay.addWidget(self.mine_box)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Save)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def name(self) -> str:
        return self.name_edit.text().strip()

    def mine(self) -> bool:
        return self.mine_box.isChecked()


class DownloadPage(QWidget):
    videosChanged = Signal()        # downloads finished / files renamed (board should reload)
    busyChanged = Signal(bool)
    openAccounts = Signal()

    def __init__(self, store: Store, thumbs: ThumbnailCache, accounts: AccountService | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.store = store
        self.thumbs = thumbs
        self.accounts = accounts or AccountService(store, MemoryTokenStore())
        self.source_service = SourceService(store, self.accounts)
        self.account_fetch: CallWorker | None = None
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
        self.products_worker: ProductsWorker | None = None
        self.download_worker: DownloadWorker | None = None
        self._dl_total = self._dl_index = 0
        self._loading_profile = False
        self._save_timer = QTimer(self, singleShot=True, interval=700, timeout=self._persist_names)

        self._build_ui()
        self._connect()
        self._bridge = change_bridge(store)
        self._bridge.changed.connect(self._on_store_change)

    # ================================================================ UI
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 12)
        outer.setSpacing(12)

        # ---- "Bring videos in": the IN door (Accounts is the OUT door)
        intake = QFrame(objectName="Card")
        il = QVBoxLayout(intake)
        il.setContentsMargins(16, 14, 16, 14)
        il.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(12)
        title = QLabel("Bring videos in")
        title.setObjectName("DetailTitle")
        tip(title, "This page copies videos into Reelay from TikTok, Instagram or YouTube. "
                   "To post videos out, connect accounts under Accounts.")
        head.addWidget(title)
        self.doors_note = muted("Import media you own or have permission to manage. Downloading pulls videos in "
                                "from these sources; posting pushes videos out to the accounts you connect "
                                "under Accounts.")
        self.doors_note.setWordWrap(True)
        head.addWidget(self.doors_note, 1)
        il.addLayout(head)

        self.chip_area = QScrollArea()
        self.chip_area.setWidgetResizable(True)
        self.chip_area.setFrameShape(QFrame.NoFrame)
        self.chip_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.chip_area.setFixedHeight(54)
        tip(self.chip_area, "Your saved sources. Click List Videos on one to see its videos below.")
        chip_body = QWidget(objectName="CardBody")
        self.chip_row = QHBoxLayout(chip_body)
        self.chip_row.setContentsMargins(0, 0, 0, 0)
        self.chip_row.setSpacing(8)
        self.chip_area.setWidget(chip_body)
        il.addWidget(self.chip_area)

        self.paste_label = muted("Or paste a profile link (yours, or an account you manage), or several video links.")
        il.addWidget(self.paste_label)
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.url = QLineEdit(objectName="UrlField")
        self.url.setPlaceholderText("https://www.tiktok.com/@creator")
        self.url.setClearButtonEnabled(True)
        self.url.setAttribute(Qt.WA_MacShowFocusRect, False)
        tip(self.url, "A TikTok, Instagram or YouTube profile link, or one or more video links separated by spaces.")
        self.fetch_btn = QPushButton("List Videos")
        set_prop(self.fetch_btn, "primary")
        self.fetch_btn.setMinimumHeight(38)
        tip(self.fetch_btn, "Show every video from this link below. Nothing is downloaded until you click Download.")
        self.save_source_btn = QPushButton("Save This Link…")
        tip(self.save_source_btn, "Keep this link as a one-click source for this profile, so next time you "
                                  "don't have to paste it")
        self.save_source_btn.setMinimumHeight(38)
        row1.addWidget(self.url, 1)
        row1.addWidget(self.save_source_btn)
        row1.addWidget(self.fetch_btn)
        il.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row2.addWidget(tip(muted("Save videos to"), "The folder on this Mac where downloaded videos go"))
        self.folder_btn = QPushButton()
        set_prop(self.folder_btn, "flat")
        row2.addWidget(self.folder_btn)
        self.open_folder_btn = QPushButton("Show in Finder")
        tip(self.open_folder_btn, "Open the folder where this profile's videos are saved")
        row2.addWidget(self.open_folder_btn)
        row2.addStretch()
        row2.addWidget(tip(muted("Quality"), "Video quality to download"))
        self.quality = QComboBox()
        for key, label in QUALITY_LABELS.items():
            self.quality.addItem(label, key)
        tip(self.quality, "Video quality to download. Best is the original quality the platform keeps.")
        self.quality.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        row2.addWidget(self.quality)
        row2.addSpacing(12)
        row2.addWidget(tip(muted("Use browser sign-in"), "Borrow your sign-in from a web browser"))
        self.cookies = QComboBox()
        self.cookies.addItem("Don't use", "")
        for b in COOKIE_BROWSERS:
            self.cookies.addItem(b.capitalize(), b)
        tip(self.cookies, "Instagram, and private or scheduled YouTube videos, need you to be signed in. "
                          "Reelay borrows the sign-in from the browser you pick; make sure you're signed in there. "
                          "Your password is never seen or stored.")
        row2.addWidget(self.cookies)
        il.addLayout(row2)
        outer.addWidget(intake)

        list_area = QWidget(objectName="ListArea")
        ll = QVBoxLayout(list_area)
        ll.setContentsMargins(12, 12, 12, 0)
        ll.setSpacing(10)
        bar = QHBoxLayout()
        bar.setSpacing(10)
        self.search = GrowingSearch("Search captions, hashtags, tagged products, your names, file names")
        self.search.setMinimumWidth(220)
        tip(self.search, "Filter the list. Matches captions (hashtags included), the names you've given videos, "
                         "and file names.")
        bar.addWidget(self.search, 2)
        self.tag_filter = tip(QComboBox(), "Show only videos with one hashtag, like a category. The number is how "
                                           "many videos have it. Tip: click a hashtag in the side panel too.")
        self.tag_filter.setMinimumWidth(170)
        self.tag_filter.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.tag_filter.setMinimumContentsLength(14)
        self.tag_filter.addItem("All hashtags", "")
        bar.addWidget(self.tag_filter, 0, Qt.AlignTop)
        self.product_filter = tip(QComboBox(), "Show only videos tagged with one product (TikTok Shop), even when "
                                               "the captions and hashtags are different. The number is how many "
                                               "videos have it. Reelay looks up tagged products in the background.")
        self.product_filter.setMinimumWidth(170)
        self.product_filter.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.product_filter.setMinimumContentsLength(14)
        self.product_filter.addItem("All products", "")
        bar.addWidget(self.product_filter, 0, Qt.AlignTop)
        ll.addLayout(bar)

        # ---- selection strip: what will download, and the buttons to change it
        strip = QFrame(objectName="SelectionStrip")
        sl = QHBoxLayout(strip)
        sl.setContentsMargins(12, 6, 8, 6)
        sl.setSpacing(10)
        self.selection_hint = QLabel()
        self.selection_hint.setWordWrap(True)
        tip(self.selection_hint, "Ticked videos download when you click Download (bottom right)")
        sl.addWidget(self.selection_hint, 1)
        self.select_all_btn = QPushButton("Select All")
        tip(self.select_all_btn, "Tick every video in the list for downloading (when you've searched or picked a "
                                 "hashtag, only the videos shown). Videos already downloaded are skipped.")
        sl.addWidget(self.select_all_btn)
        self.unselect_all_btn = QPushButton("Unselect All")
        tip(self.unselect_all_btn, "Untick every video, so nothing will download until you choose again")
        sl.addWidget(self.unselect_all_btn)
        self.select_btn = QToolButton(text="More  ▾")
        self.select_btn.setPopupMode(QToolButton.InstantPopup)
        tip(self.select_btn, "More ways to select: only the newest few, only what your search shows, or the "
                             "highlighted rows")
        sl.addWidget(self.select_btn)
        ll.addWidget(strip)

        bar2 = QHBoxLayout()
        bar2.setSpacing(10)
        name_label = tip(muted("Name files"), "How downloaded video files are named")
        name_label.setMinimumHeight(32)
        bar2.addWidget(name_label, 0, Qt.AlignTop)
        self.naming = NamingPicker()
        bar2.addWidget(self.naming, 1)
        self.autoname_btn = QToolButton(text="Suggest Names  ▾")
        self.autoname_btn.setPopupMode(QToolButton.InstantPopup)
        tip(self.autoname_btn, "Fill in a short name for videos from the first words of their captions")
        bar2.addWidget(self.autoname_btn, 0, Qt.AlignTop)
        self.apply_btn = QPushButton("Rename Files…")
        tip(self.apply_btn, "Rename videos you've already downloaded so they match the naming choice. "
                            "You'll see every change before anything is renamed.")
        bar2.addWidget(self.apply_btn, 0, Qt.AlignTop)
        ll.addLayout(bar2)

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
        widths = {Col.CHECK: 34, Col.COVER: 50, Col.DATE: 86, Col.LENGTH: 72, Col.VIEWS: 64, Col.STATUS: 132}
        for col, w in widths.items():
            hh.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, w)
        hh.setSectionResizeMode(Col.FILENAME, QHeaderView.Interactive)
        self.table.setColumnWidth(Col.FILENAME, 150)
        hh.setContextMenuPolicy(Qt.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._column_menu)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._row_menu)
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
        tip(self.progress, "Progress of the current list or download")
        bl.addWidget(self.progress)
        brow = QHBoxLayout()
        self.status = muted("")
        tip(self.status, "What Reelay is doing, and what to do next")
        brow.addWidget(self.status, 1)
        self.manifest_btn = QPushButton("Open Video List")
        tip(self.manifest_btn, "Open manifest.md: a table of every downloaded video with its date, caption and file")
        brow.addWidget(self.manifest_btn)
        brow.addSpacing(8)
        self.count_label = muted("")
        tip(self.count_label, "How many videos are selected for downloading, out of those not downloaded yet")
        brow.addWidget(self.count_label)
        self.cancel_btn = QPushButton("Stop")
        tip(self.cancel_btn, "Stop after the current video. Finished downloads are kept.")
        set_prop(self.cancel_btn, "danger")
        self.cancel_btn.setVisible(False)
        brow.addWidget(self.cancel_btn)
        self.download_btn = QPushButton("Download")
        set_prop(self.download_btn, "primary")
        self.download_btn.setMinimumHeight(36)
        self.download_btn.setMinimumWidth(190)
        tip(self.download_btn, "Download the selected videos into the folder above. They appear on the Board, in "
                               "Backlog. Nothing is selected until you choose: use Select All or tick boxes.")
        brow.addWidget(self.download_btn)
        bl.addLayout(brow)
        outer.addLayout(bl)

        sel = QMenu(self)
        sel.addAction("Select Only the Newest…", self._check_newest)
        sel.addAction("Select Only What the Search Shows", self._check_visible)
        sel.addSeparator()
        sel.addAction("Select Highlighted Rows", lambda: self._check_selected(True))
        sel.addAction("Unselect Highlighted Rows", lambda: self._check_selected(False))
        self.select_btn.setMenu(sel)

        auto = QMenu(self)
        auto.addAction("Suggest Names for Highlighted Videos", lambda: self._auto_name(selected_only=True))
        auto.addAction("Suggest Names for All Unnamed Videos", lambda: self._auto_name(selected_only=False))
        auto.addSeparator()
        auto.addAction("Clear Names of Highlighted Videos", self._clear_names)
        auto.addSeparator()
        auto.addAction("Custom Naming Patterns (Advanced)…", self.show_tokens_help)
        self.autoname_btn.setMenu(auto)

    def _column_menu(self, pos) -> None:
        """Right-click the header to show or hide optional columns."""
        menu = QMenu(self)
        for col, label in ((Col.LENGTH, "Length"), (Col.VIEWS, "Views"), (Col.CUSTOM, "Your Name"),
                           (Col.FILENAME, "File Name"), (Col.STATUS, "Status")):
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(not self.table.isColumnHidden(col))
            act.toggled.connect(lambda on, c=col: self._set_column_visible(c, on))
        menu.exec(self.table.horizontalHeader().mapToGlobal(pos))

    def _row_menu(self, pos) -> None:
        """Right-click a video (or highlighted videos)."""
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        if not self.table.selectionModel().isRowSelected(index.row(), index.parent()):
            self.table.selectRow(index.row())
        items = self._selected_items()
        if not items:
            return
        n = len(items)
        plural = "s" if n > 1 else ""
        menu = QMenu(self)
        menu.addAction("Select for Download", lambda: self._check_selected(True))
        menu.addAction("Unselect", lambda: self._check_selected(False))
        if n == 1 and items[0].url:
            menu.addAction("Open Original Post", lambda: QDesktopServices.openUrl(QUrl(items[0].url)))
        menu.addSeparator()
        menu.addAction(f"Archive {n} Video{plural}…" if n > 1 else "Archive…", lambda: self._archive(items))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _archive(self, items: list[VideoItem]) -> None:
        """Hide videos from this list and the Board (nothing is deleted)."""
        if not self.profile:
            return
        n = len(items)
        if QMessageBox.question(self, "Archive", f"Archive {n} video{'s' if n > 1 else ''}? "
                                f"{'They' if n > 1 else 'It'} disappear{'' if n > 1 else 's'} from this list and the "
                                "Board. Files and tickets are kept; bring them back from Board → Archived….") \
                != QMessageBox.Yes:
            return
        from ..models import DispatchStatus
        self.store.save_videos(self.profile.id, items)   # videos only listed so far get a row first
        ids = [self.store.video_row_id(self.profile.id, i.platform, i.video_id) for i in items]
        ids = [i for i in ids if i is not None]
        if any(d.status in DispatchStatus.ACTIVE for i in ids for d in self.store.dispatches_for(i)):
            QMessageBox.warning(self, APP_NAME, "Videos that are scheduled or uploading can't be archived; "
                                                "cancel them on the Board first.")
            return
        self.store.set_archived(ids, True)
        self._set_status(f"Archived {n} video{'s' if n > 1 else ''}. Board → Archived… brings them back.")
        self.reload()

    def _set_column_visible(self, col: int, visible: bool) -> None:
        self.table.setColumnHidden(col, not visible)
        hidden = [c for c in range(len(Col)) if self.table.isColumnHidden(c)]
        QSettings().setValue("hidden_columns", hidden)

    def _build_empty_state(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addStretch()
        t = QLabel("Pick a source, or paste a link", objectName="EmptyTitle")
        t.setAlignment(Qt.AlignCenter)
        s = muted("Click List Videos on a source above, or paste a TikTok, Instagram or YouTube link.\n"
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
        self.save_source_btn.clicked.connect(self._save_pasted_source)
        self.folder_btn.clicked.connect(self.choose_folder)
        self.open_folder_btn.clicked.connect(self.open_output_folder)
        self.manifest_btn.clicked.connect(self.open_manifest)
        self.download_btn.clicked.connect(self._start_download)
        self.select_all_btn.clicked.connect(self.select_all)
        self.unselect_all_btn.clicked.connect(self.unselect_all)
        self.cancel_btn.clicked.connect(self._cancel_download)
        self.apply_btn.clicked.connect(self._apply_names)
        self.search.textChangedTo.connect(lambda text: (self.proxy.set_query(text), self._update_counts()))
        self.tag_filter.currentIndexChanged.connect(self._on_tag_filter)
        self.product_filter.currentIndexChanged.connect(self._on_product_filter)
        self.detail.productClicked.connect(self.filter_by_product)
        self.detail.tagClicked.connect(self.filter_by_tag)
        self._tags_timer = QTimer(self, singleShot=True, interval=400, timeout=self._refresh_tags)
        self._products_timer = QTimer(self, singleShot=True, interval=600, timeout=self._refresh_products)
        self.naming.templateChanged.connect(self._on_template)
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
        if change.entity in ("source", "account") and self.profile and change.profile_id in (None, self.profile.id):
            self.refresh_sources()
            return
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
        self.naming.set_template(profile.template or DEFAULT_TEMPLATE)
        self.model.set_template(profile.template or DEFAULT_TEMPLATE)
        self.url.setText(profile.last_source)
        self._loading_profile = False
        self._open_folder(self.folder)
        self.refresh_sources()
        self._set_status("")
        self._start_details()

    def _save_profile_prefs(self) -> None:
        if self._loading_profile or not self.profile:
            return
        self.profile.quality = self.quality.currentData()
        self.profile.cookies_browser = self.cookies.currentData() or ""
        self.profile.template = self.naming.template() or DEFAULT_TEMPLATE
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
        pending = len(items) - done
        parts = [f"{checked} of {pending} selected" if checked else
                 (f"None of {pending} selected" if pending else "Nothing left to download")]
        self.select_all_btn.setEnabled(pending > 0 and checked < pending)
        self.unselect_all_btn.setEnabled(checked > 0)
        shown = self.proxy.rowCount()
        narrowed = f" ({shown} of {len(items)} shown)" if self.proxy.filtering else ""
        if not pending:
            hint = "Every video here is already downloaded." if items else ""
        elif not checked:
            hint = (f"<b>Nothing selected yet.</b> Tick the videos you want, or click <b>Select All</b>.{narrowed}")
        else:
            hint = (f"<b>{checked} selected</b> to download.{narrowed} Click <b>Download {checked} "
                    f"Video{'s' if checked != 1 else ''}</b> at the bottom right.")
        self.selection_hint.setText(hint)
        set_prop(self.select_all_btn, "primary", pending > 0 and not checked)   # the obvious next step
        set_prop(self.download_btn, "primary", checked > 0)
        self._tags_timer.start()
        self._products_timer.start()
        if done:
            parts.append(f"{done} downloaded")
        self.count_label.setText(" · ".join(parts) if items else "")
        self.download_btn.setText(f"Download {checked} Video{'s' if checked != 1 else ''}" if checked else "Download")
        self.download_btn.setEnabled(checked > 0 and not self.busy)
        self.apply_btn.setEnabled(done > 0 and not self.busy)

    def _refresh_detail(self, *_args) -> None:
        items = self._selected_items()
        self.naming.set_example_item(items[0] if items else None)
        if len(items) == 1:
            self.detail.show_item(items[0], self.model.planned_filename(items[0]))
        elif len(items) > 1:
            self.detail.show_multi(items)
        else:
            self.detail.show_empty()

    def show_tokens_help(self) -> None:
        QMessageBox.information(self, "Custom Naming Patterns", tokens_help_html())

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
        self.folder_btn.setToolTip(f"Videos are saved in {folder}. Click to choose a different folder.")
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
        previous = {i.key: i.checked for i in self.model.items()}   # what the person selected so far
        by_key: dict[str, VideoItem] = {}
        archived: set[str] = set()
        for row in self.store.load_videos(pid, archived=None):
            if row.archived_at:
                archived.add(row.item.key)   # hidden, and never re-imported from the folder
                continue
            row.item.refresh_download_status(self.folder)
            by_key[row.item.key] = row.item

        imported = []
        names = self.library.load_custom_names()
        for item in self.library.load():
            if item.key not in by_key and item.key not in archived:
                item.folder = str(self.folder)
                item.custom_name = item.custom_name or names.get(item.key, "")
                by_key[item.key] = item
                imported.append(item)
        if imported:
            self.store.save_videos(pid, imported)

        merged: list[VideoItem] = []
        for fetched in self.fetched:
            if fetched.key in archived:
                continue
            existing = by_key.pop(fetched.key, None)
            if existing is not None:
                existing.merge_metadata(fetched)
                merged.append(existing)
            else:
                merged.append(replace(fetched))
        merged.extend(sorted(by_key.values(), key=lambda i: i.upload_date, reverse=True))
        for item in merged:   # new videos arrive unselected; choices already made are kept
            item.checked = previous.get(item.key, False) and not item.is_downloaded
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
    @property
    def fetching(self) -> bool:
        return bool((self.fetch_worker and self.fetch_worker.isRunning()) or
                    (self.account_fetch and self.account_fetch.isRunning()))

    def _fetch(self) -> None:
        """Fetch from the pasted link(s), exactly as before sources existed."""
        if self.fetching or not self.profile:
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
        self._fetch_urls(urls)

    def _fetch_urls(self, urls: list[str], source_id: int | None = None) -> None:
        self._begin_fetch("Listing videos…")
        self.fetch_worker = FetchWorker(urls, self._opts(), self)
        self.fetch_worker.sourceStarted.connect(lambda u: self._set_status(f"Listing videos from {u} …"))
        self.fetch_worker.fetched.connect(lambda items, errs: self._on_fetched(urls, items, errs, source_id))
        self.fetch_worker.start()

    def _begin_fetch(self, message: str) -> None:
        self._stop_details()
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText("Listing…")
        for chip in self._chips():
            chip.set_busy(True)
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        self._set_status(message)

    # ================================================================ sources
    def _chips(self) -> list["SourceChip"]:
        return [self.chip_row.itemAt(i).widget() for i in range(self.chip_row.count())
                if isinstance(self.chip_row.itemAt(i).widget(), SourceChip)]

    def refresh_sources(self) -> None:
        """Rebuild the "Download from" chips: own accounts first, then saved profiles."""
        while self.chip_row.count():
            item = self.chip_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not self.profile:
            return
        views = self.source_service.display_sources(self.profile.id)
        for view in views:
            chip = SourceChip(view, self)
            chip.fetchRequested.connect(self.fetch_source)
            chip.renameRequested.connect(self._rename_source)
            chip.mineToggled.connect(self._toggle_mine)
            chip.removeRequested.connect(self._remove_source)
            chip.manageRequested.connect(self.openAccounts.emit)
            chip.set_busy(self.fetching)
            self.chip_row.addWidget(chip)
        if not views:
            hint = muted("No sources yet. Paste a profile link below and click “Save This Link…”, or connect a "
                         "YouTube channel under Accounts.")
            hint.setProperty("faint", True)
            self.chip_row.addWidget(hint)
        self.chip_row.addStretch()

    def fetch_source(self, view: SourceView) -> None:
        """One click: fetch from a saved source's URL or a connected channel."""
        if self.fetching or not self.profile:
            return
        if view.account is not None:
            self._fetch_account(view)
            return
        self.url.setText(view.url)
        self.profile.last_source = view.url
        self.store.update_profile(self.profile)
        self._fetch_urls([view.url], source_id=view.source_id)

    def _fetch_account(self, view: SourceView) -> None:
        try:
            client = self.accounts.client_for(view.account)
        except YouTubeError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        url = channel_url(view.account)
        self._begin_fetch(f"Listing {view.name}'s videos from YouTube…")
        self.account_fetch = CallWorker(lambda: youtube_items(client.list_channel_videos()), parent=self)
        self.account_fetch.succeeded.connect(lambda items: self._on_fetched([url], items, [],
                                                                            authorization=OWN_CONTENT))
        self.account_fetch.failed.connect(lambda message: self._on_fetched([url], [], [(view.name, message)]))
        self.account_fetch.start()

    def _save_pasted_source(self) -> None:
        if not self.profile:
            return
        url = self.url.text().strip()
        if not url or len(re.split(r"[\s,]+", url)) > 1:
            QMessageBox.information(self, APP_NAME, "Paste one profile link to save it as a source.")
            return
        dialog = SourceDialog(default_name(url), self.profile.name, self)
        if dialog.exec() != SourceDialog.Accepted:
            return
        try:
            self.source_service.add(self.profile.id, url, dialog.name(), dialog.mine())
        except SourceError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        self._set_status(f"Saved “{dialog.name() or default_name(url)}”. Next time it's one click above.")

    def _rename_source(self, view: SourceView) -> None:
        name, ok = QInputDialog.getText(self, "Rename This Source", "New name for this source:", text=view.name)
        if ok:
            try:
                self.source_service.rename(view.source_id, name)
            except SourceError as exc:
                QMessageBox.warning(self, APP_NAME, str(exc))

    def _toggle_mine(self, view: SourceView) -> None:
        self.source_service.set_mine(view.source_id, not view.mine)

    def _remove_source(self, view: SourceView) -> None:
        extra = f" (and {view.duplicates} duplicate{'s' if view.duplicates != 1 else ''})" if view.duplicates else ""
        if QMessageBox.question(self, "Remove Source", f"Remove “{view.name}”{extra} from this profile's sources?\n\n"
                                "Videos already fetched or downloaded stay.") != QMessageBox.Yes:
            return
        target = normalize_url(view.url)
        for other in self.source_service.all_sources(self.profile.id):
            if other.kind == "saved" and normalize_url(other.url) == target:
                self.source_service.remove(other.source_id)

    def _authorization_for(self, urls: list[str]) -> str | None:
        """Provenance: links to this profile's own accounts (saved as "mine", or a
        connected channel) are recorded as own content. Anything else is left
        unrecorded for the person to fill in on the ticket."""
        own = {normalize_url(v.url) for v in self.source_service.display_sources(self.profile.id)
               if (v.mine or v.account is not None) and v.url}
        return OWN_CONTENT if urls and all(normalize_url(u) in own for u in urls) else None

    def _on_fetched(self, urls: list[str], items: list[VideoItem], errors: list[tuple[str, str]],
                    source_id: int | None = None, authorization: str | None = None) -> None:
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("List Videos")
        for chip in self._chips():
            chip.set_busy(False)
        self.progress.setRange(0, 1000)
        self.progress.setVisible(False)
        if source_id is not None and items:
            self.source_service.mark_fetched(source_id)
        if errors:
            self.failures.extend(errors)
            QMessageBox.warning(self, "Couldn't fetch everything",
                                "\n\n".join(f"{u}\n{reason}" for u, reason in errors))
        if not items:
            self._set_status("No videos found." if not errors else "Couldn't list the videos.")
            return
        self.fetched = items
        self.sources = urls
        self.reload()
        held = [i for i in self.model.items() if not i.is_downloaded and not downloadable_by_default(i)]
        if held:  # private / scheduled YouTube videos need the owner's login cookies: never preselected
            self.model.set_checked([i.key for i in held], False)
        self.store.save_videos(self.profile.id, self.model.items(),
                               authorization=authorization or self._authorization_for(urls))
        new = sum(1 for i in self.model.items() if not i.is_downloaded)
        note = (f" {len(held)} private or scheduled video{'s' if len(held) != 1 else ''} can't download yet: "
                "downloading those needs “Use browser sign-in” set to a browser signed into that channel.") if held else ""
        self._set_status(f"Found {len(items)} videos · {new} not downloaded yet. "
                         "Nothing is selected yet: click Select All, or tick the ones you want, then click Download." + note)
        self._start_details()

    def _start_details(self) -> None:
        self._start_products()
        needing = [i for i in self.model.items() if not i.has_details and not i.is_downloaded]
        if not needing or (self.details_worker and self.details_worker.isRunning()):
            return
        self.details_worker = DetailsWorker(needing, self._opts(), self)
        self.details_worker.loaded.connect(self._on_details)
        self.details_worker.start()

    def _stop_details(self) -> None:
        if self.details_worker and self.details_worker.isRunning():
            self.details_worker.stop()
        if self.products_worker and self.products_worker.isRunning():
            self.products_worker.stop()

    # ================================================================ tagged products (TikTok Shop)
    def _start_products(self) -> None:
        needing = [i for i in self.model.items() if i.platform == "TikTok" and i.products is None and i.url]
        if not needing or (self.products_worker and self.products_worker.isRunning()):
            return
        self.products_worker = ProductsWorker(needing, self._opts(), parent=self)
        self.products_worker.loaded.connect(self._on_products)
        self.products_worker.finishedAll.connect(self._products_done)
        self.products_worker.start()

    def _on_products(self, key: str, records: list) -> None:
        item = self.model.find(key)
        if item is None or not self.profile:
            return
        item.products = records
        self.store.save_video(self.profile.id, item)
        self._products_timer.start()
        if records and self.detail.item is not None and self.detail.item.key == key:
            self._refresh_detail()

    def _products_done(self, tagged: int) -> None:
        self._refresh_products()
        if tagged:
            self._set_status(f"Found tagged products on {tagged} video{'s' if tagged != 1 else ''}. Use "
                             "“All products” above the list to see every video for one product.")

    def _refresh_products(self) -> None:
        """Rebuild the product list from the videos on the page, keeping the choice."""
        from collections import Counter

        from ..products import video_products
        current = self.product_filter.currentData() or ""
        products, count = {}, Counter()
        for item in self.model.items():
            for p in video_products(item):
                products.setdefault(p.id, p)
                count[p.id] += 1
        ordered = sorted(products.values(), key=lambda p: (-count[p.id], p.label.lower()))
        wanted = [("", "All products")] + [(p.id, f"{p.label}  ({count[p.id]})") for p in ordered]
        if [(self.product_filter.itemData(i), self.product_filter.itemText(i))
                for i in range(self.product_filter.count())] == wanted:
            return
        self.product_filter.blockSignals(True)
        self.product_filter.clear()
        for pid, label in wanted:
            self.product_filter.addItem(label, pid)
            self.product_filter.setItemData(self.product_filter.count() - 1,
                                            products[pid].title if pid else "Every video", Qt.ToolTipRole)
        index = self.product_filter.findData(current)
        self.product_filter.setCurrentIndex(max(index, 0))
        self.product_filter.blockSignals(False)
        if index < 0 and current:
            self._on_product_filter()

    def _on_product_filter(self) -> None:
        self.proxy.set_product(self.product_filter.currentData())
        self._update_counts()

    def filter_by_product(self, product_id: str) -> None:
        self._refresh_products()
        index = self.product_filter.findData(product_id)
        if index >= 0:
            self.product_filter.setCurrentIndex(index)

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

    def _shown_items(self) -> list[VideoItem]:
        return [self.model.item(self.proxy.mapToSource(self.proxy.index(r, 0)).row())
                for r in range(self.proxy.rowCount())]

    def select_all(self) -> None:
        """Select every video the list shows (all of them unless a search is active)."""
        shown = self._shown_items()
        self.model.set_checked([i.key for i in shown], True)
        n = sum(1 for i in shown if not i.is_downloaded)
        self._set_status(f"Selected {n} video{'s' if n != 1 else ''}" +
                         (" (the ones shown by your search or hashtag)." if self.proxy.filtering else ".")
                         if n else "Every video shown is already downloaded.")

    def unselect_all(self) -> None:
        self._check_all(False)
        self._set_status("Nothing selected.")

    # ================================================================ hashtags (categories)
    def _refresh_tags(self) -> None:
        """Rebuild the hashtag list from the videos on the page, keeping the choice."""
        from ..tags import counts
        current = self.tag_filter.currentData() or ""
        found = counts(i.hashtags for i in self.model.items())
        wanted = [("", "All hashtags")] + [(t, f"#{t}  ({n})") for t, n in found]
        if [(self.tag_filter.itemData(i), self.tag_filter.itemText(i))
                for i in range(self.tag_filter.count())] == wanted:
            return
        self.tag_filter.blockSignals(True)
        self.tag_filter.clear()
        for tag, label in wanted:
            self.tag_filter.addItem(label, tag)
        index = self.tag_filter.findData(current)
        self.tag_filter.setCurrentIndex(max(index, 0))
        self.tag_filter.blockSignals(False)
        if index < 0 and current:
            self._on_tag_filter()

    def _on_tag_filter(self) -> None:
        self.proxy.set_tag(self.tag_filter.currentData())
        self._update_counts()

    def filter_by_tag(self, tag: str) -> None:
        """Show only videos with this hashtag (e.g. a hashtag clicked in the side panel)."""
        self._refresh_tags()
        index = self.tag_filter.findData(tag.lstrip("#").lower())
        if index >= 0:
            self.tag_filter.setCurrentIndex(index)

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
        if "{custom}" not in self.naming.template():
            self._set_status(f"Named {len(targets)} videos. To use these names in the file names, set "
                             "Name files to “Date and your name”.")
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
            msg += f", {failed} failed (hover the Status column to see why)"
        self._set_status(msg + ". Downloaded videos are in the Board's Backlog.")
        self.videosChanged.emit()
        self._start_details()
        QApplication.alert(self.window())

    # ================================================================ shutdown
    def shutdown(self, timeout_ms: int = 8000) -> None:
        if getattr(self, "_bridge", None) is not None:
            self._bridge.changed.disconnect(self._on_store_change)
            self._bridge = None
        if self.busy:
            self.download_worker.cancel()
            self.download_worker.wait(timeout_ms)
        self._stop_details()
        if self.details_worker:
            self.details_worker.wait(3000)
        if self.products_worker:
            self.products_worker.wait(3000)
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._persist_names()
