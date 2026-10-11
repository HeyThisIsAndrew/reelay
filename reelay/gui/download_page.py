"""Download page: fetch a profile → review/select/name → download → rename."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu,
                               QMessageBox, QProgressBar, QPushButton, QScrollArea, QSlider, QStackedWidget,
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
from . import icons
from .bridge import change_bridge
from .chrome import FilterButton, Popover, ViewToggle, icon_button
from .detail_panel import DetailPanel
from .inspector import ContextualSplitter, Inspector, MediaPane
from .player import PlayerWidget
from .scrub import ScrubCache
from .dialogs import RenamePreviewDialog, tokens_help_html
from .cards import CARD_MIN_WIDTH, TILE_MAX, TILE_MIN, TILE_SIZES, LibraryCardDelegate, MediaGrid
from .table_model import ROW_HEIGHT, Col, CoverDelegate, PageProxy, VideoFilterProxy, VideoTableModel
from .theme import set_prop
from .tips import tip
from .widgets import GrowingSearch, NamingPicker, ProductFilter
from .thumbnails import ThumbnailCache
from .workers import CallWorker, DetailsWorker, DownloadWorker, FetchWorker, ProductsWorker

log = logging.getLogger("reelay")
OWN_CONTENT = "Own content"
PAGE_SIZES = (24, 48, 96, 0)        # 0 = all on one page
DEFAULT_PAGE_SIZE = 48
SORTS = (("newest", "Newest first"), ("oldest", "Oldest first"), ("views", "Most viewed"),
         ("longest", "Longest first"), ("shortest", "Shortest first"), ("title", "Caption A to Z"))


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


class DownloadSettingsDialog(QDialog):
    """Where videos are saved, quality, browser sign-in and file naming: set once, rarely changed,
    so they live here instead of crowding the Library."""

    def __init__(self, page: "DownloadPage"):
        super().__init__(page)
        self.page = page
        self.setWindowTitle("Download Settings")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 16)
        lay.setSpacing(8)

        def section(title: str, hint: str = "") -> None:
            label = QLabel(title, objectName="DetailTitle")
            lay.addSpacing(6)
            lay.addWidget(label)
            if hint:
                lay.addWidget(muted(hint))

        section("Where videos go")
        row = QHBoxLayout()
        row.addWidget(page.folder_btn, 1)
        row.addWidget(page.open_folder_btn)
        lay.addLayout(row)
        section("Quality")
        lay.addWidget(page.quality)
        section("Browser sign-in", "Instagram, and private or scheduled YouTube videos, need you to be signed in. "
                                   "Reelay borrows the sign-in from a browser; your password is never seen.")
        lay.addWidget(page.cookies)
        section("File names", "How downloaded video files are named.")
        lay.addWidget(page.naming)
        row = QHBoxLayout()
        row.addWidget(page.autoname_btn)
        row.addWidget(page.apply_btn)
        row.addStretch()
        lay.addLayout(row)
        lay.addSpacing(8)
        lay.addWidget(page.doors_note)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.accept)
        done = buttons.button(QDialogButtonBox.Close)
        done.setText("Done")
        set_prop(done, "primary")
        tip(done, "Close Download Settings. Changes are saved as you make them.")
        lay.addWidget(buttons)

    def open_for_profile(self) -> None:
        name = self.page.profile.name if self.page.profile else ""
        self.setWindowTitle(f"Download Settings · {name}" if name else "Download Settings")
        self.open()


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
        self.proxy = VideoFilterProxy(self)          # filtering, sorting and counts
        self.proxy.setSourceModel(self.model)
        self.pager = PageProxy(self)                  # the page the grid and list show
        self.pager.setSourceModel(self.proxy)

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
        self._file_cache: dict[str, tuple[float | None, bool]] = {}
        self._library_cache: tuple | None = None
        self._dir_mtimes: dict[Path, float | None] = {}

        self._build_ui()
        self._connect()
        saved = self.store.get_setting("ui.library.page_size", str(DEFAULT_PAGE_SIZE))
        index = self.page_size.findData(int(saved) if saved.isdigit() else DEFAULT_PAGE_SIZE)
        self.page_size.blockSignals(True)
        self.page_size.setCurrentIndex(index if index >= 0 else self.page_size.findData(DEFAULT_PAGE_SIZE))
        self.page_size.blockSignals(False)
        self.pager.set_size(self.page_size.currentData())
        saved_tile = self.store.get_setting("ui.library.tile_size", str(CARD_MIN_WIDTH))
        self.set_tile_size(int(saved_tile) if saved_tile.isdigit() else CARD_MIN_WIDTH)
        self.tile_controls.setVisible(self.view_toggle.mode == "grid")
        self._bridge = change_bridge(store)
        self._bridge.changed.connect(self._on_store_change)

    # ================================================================ UI
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 12)
        outer.setSpacing(12)

        # ---- header: what this is, how many, and the few controls that matter
        head = QHBoxLayout()
        head.setSpacing(10)
        title = QLabel("Library", objectName="PageTitle")
        tip(title, "Your videos: bring them in from TikTok, Instagram or YouTube, then choose what to download. "
                   "To post videos out, connect accounts under Accounts.")
        head.addWidget(title, 0, Qt.AlignBottom)
        self.page_count = QLabel(objectName="PageCount")
        head.addWidget(self.page_count, 0, Qt.AlignBottom)
        head.addStretch()
        self.search = GrowingSearch("Search captions, hashtags, products, names")
        self.search.setFixedWidth(340)
        tip(self.search, "Filter the list. Matches captions (hashtags included), the names you've given videos, "
                         "and file names.")
        head.addWidget(self.search, 0, Qt.AlignVCenter)
        self.filter_popover = Popover(self)
        self.filter_btn = FilterButton(self.filter_popover)
        head.addWidget(self.filter_btn)
        # tile size (grid only): zoom out, slider, zoom in; ⌘− / ⌘+ step through the presets
        self.tile_controls = QWidget()
        tl = QHBoxLayout(self.tile_controls)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(2)
        self.zoom_out_btn = icon_button("zoom_out", "Smaller tiles (⌘−)")
        self.tile_slider = QSlider(Qt.Horizontal, objectName="TileSlider")
        self.tile_slider.setRange(TILE_MIN, TILE_MAX)
        self.tile_slider.setSingleStep(20)
        self.tile_slider.setPageStep(80)
        self.tile_slider.setFixedWidth(110)
        tip(self.tile_slider, "Tile size: drag for smaller or bigger thumbnails")
        self.zoom_in_btn = icon_button("zoom_in", "Bigger tiles (⌘+)")
        self.zoom_out_btn.clicked.connect(lambda: self.step_tile_size(-1))
        self.zoom_in_btn.clicked.connect(lambda: self.step_tile_size(1))
        self.tile_slider.valueChanged.connect(self.set_tile_size)
        for w in (self.zoom_out_btn, self.tile_slider, self.zoom_in_btn):
            tl.addWidget(w)
        head.addWidget(self.tile_controls)
        self.view_toggle = ViewToggle(self.store, "library")
        self.view_toggle.changed.connect(self._set_view)
        head.addWidget(self.view_toggle)
        self.settings_btn = icon_button("gear", "Download settings: where videos are saved, quality, browser "
                                                "sign-in and how files are named")
        head.addWidget(self.settings_btn)
        outer.addLayout(head)

        # ---- sources: the IN door (Accounts is the OUT door)
        intake = QFrame(objectName="SourceBar")
        il = QHBoxLayout(intake)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(8)
        self.chip_area = QScrollArea()
        self.chip_area.setWidgetResizable(True)
        self.chip_area.setFrameShape(QFrame.NoFrame)
        self.chip_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.chip_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.chip_area.setFixedHeight(46)
        tip(self.chip_area, "Your saved sources. Click List Videos on one to see its videos below.")
        chip_body = QWidget(objectName="CardBody")
        self.chip_row = QHBoxLayout(chip_body)
        self.chip_row.setContentsMargins(0, 0, 0, 0)
        self.chip_row.setSpacing(8)
        self.chip_area.setWidget(chip_body)
        il.addWidget(self.chip_area, 3)
        self.url = QLineEdit(objectName="UrlField")
        self.url.setPlaceholderText("Paste a profile or video link")
        self.url.setClearButtonEnabled(True)
        self.url.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.url.addAction(icons.icon("link", 16), QLineEdit.LeadingPosition)
        tip(self.url, "A TikTok, Instagram or YouTube profile link (yours, or an account you manage), or one or more "
                      "video links separated by spaces (Snapchat Spotlight links too).")
        il.addWidget(self.url, 2)
        self.save_source_btn = QPushButton("Save This Link…")
        tip(self.save_source_btn, "Keep this link as a one-click source for this profile, so next time you "
                                  "don't have to paste it")
        il.addWidget(self.save_source_btn)
        self.fetch_btn = QPushButton("List Videos")
        set_prop(self.fetch_btn, "primary")
        tip(self.fetch_btn, "Show every video from this link below. Nothing is downloaded until you click Download.")
        il.addWidget(self.fetch_btn)
        outer.addWidget(intake)
        self.doors_note = muted("Only import media you own or have permission to manage.", "faint")
        tip(self.doors_note, "Downloading pulls videos in from these sources; posting pushes videos out to the "
                             "accounts you connect under Accounts.")

        # ---- Download settings (a dialog): folder, quality, browser sign-in, file names
        self.folder_btn = QPushButton()
        set_prop(self.folder_btn, "flat")
        self.open_folder_btn = QPushButton("Show in Finder")
        tip(self.open_folder_btn, "Open the folder where this profile's videos are saved")
        self.quality = QComboBox()
        for key, label in QUALITY_LABELS.items():
            self.quality.addItem(label, key)
        tip(self.quality, "Video quality to download. Best is the original quality the platform keeps.")
        self.cookies = QComboBox()
        self.cookies.addItem("Don't use", "")
        for b in COOKIE_BROWSERS:
            self.cookies.addItem(b.capitalize(), b)
        tip(self.cookies, "Instagram, and private or scheduled YouTube videos, need you to be signed in. "
                          "Reelay borrows the sign-in from the browser you pick; make sure you're signed in there. "
                          "Your password is never seen or stored.")
        self.naming = NamingPicker()
        self.autoname_btn = QToolButton(text="Suggest Names  ▾")
        self.autoname_btn.setPopupMode(QToolButton.InstantPopup)
        tip(self.autoname_btn, "Fill in a short name for videos from the first words of their captions")
        self.apply_btn = QPushButton("Rename Files…")
        tip(self.apply_btn, "Rename videos you've already downloaded so they match the naming choice. "
                            "You'll see every change before anything is renamed.")
        self.settings_dialog = DownloadSettingsDialog(self)
        self.settings_btn.clicked.connect(self.settings_dialog.open_for_profile)

        list_area = QWidget(objectName="ListArea")
        ll = QVBoxLayout(list_area)
        ll.setContentsMargins(12, 12, 12, 0)
        ll.setSpacing(10)
        # Filter & Sort popover: hashtag, brand / product, what to show, order
        self.tag_filter = tip(QComboBox(), "Show only videos with one hashtag, like a category. The number is how "
                                           "many videos have it. Tip: click a hashtag in the side panel too.")
        self.tag_filter.setMinimumWidth(260)
        self.tag_filter.addItem("All hashtags", "")
        self.products_filter = ProductFilter(self.store)
        self.product_filter = self.products_filter.product     # the product menu (brand menu is .brand)
        self.show_filter = tip(QComboBox(), "Show every video, only those not downloaded yet, or only downloaded ones")
        for key, label in (("", "All videos"), ("pending", "Not downloaded yet"), ("downloaded", "Downloaded")):
            self.show_filter.addItem(label, key)
        self.sort_box = tip(QComboBox(), "The order videos are shown in")
        for key, label in SORTS:
            self.sort_box.addItem(label, key)
        self.filter_popover.add_section("Show", "Which videos the Library shows", self.show_filter)
        self.filter_popover.add_section("Hashtag", "Only videos with this hashtag", self.tag_filter)
        self.filter_popover.add_section("Brand and product", "Only videos tagged with a brand's products, or one "
                                        "product (TikTok Shop)", self.products_filter)
        self.filter_popover.add_section("Sort by", "The order videos are shown in", self.sort_box)

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
                             "highlighted videos; and renaming files")
        sl.addWidget(self.select_btn)
        ll.addWidget(strip)

        self.table = VideoTable(self._toggle_selected)
        self.table.setModel(self.pager)
        self.table.setItemDelegateForColumn(Col.COVER, CoverDelegate(self.model, self.pager, self.table))
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
        self.grid = self._build_grid()
        self.list_stack.addWidget(self.grid)
        ll.addWidget(self.list_stack, 1)

        self.detail = DetailPanel(self.thumbs)
        self.detail.cover.setVisible(False)          # the player above shows the video
        self.detail.title.setObjectName("CommandTitle")
        # Details only take room while a video is selected; otherwise the list gets the whole width.
        # The video plays on top; its details are under it.
        self.player = PlayerWidget()
        self.player.message.connect(self._set_status)
        self._player_key: str | None = None
        self.media_pane = MediaPane(self.player, self.detail)
        self.inspector = Inspector(self.media_pane, "Video")
        self.inspector.closeRequested.connect(self.close_inspector)
        self.split = ContextualSplitter(list_area, self.inspector, self.store, "download", default_width=340,
                                        min_width=300)
        outer.addWidget(self.split, 1)
        esc = QShortcut(QKeySequence(Qt.Key_Escape), self, context=Qt.WidgetWithChildrenShortcut)
        esc.activated.connect(self.close_inspector)

        bl = QVBoxLayout()
        bl.setSpacing(8)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1000)
        self.progress.setVisible(False)
        tip(self.progress, "Progress of the current list or download")
        bl.addWidget(self.progress)
        brow = QHBoxLayout()
        # pages: how many videos per page, and which page
        self.page_size = tip(QComboBox(), "How many videos to show per page")
        for n in PAGE_SIZES:
            self.page_size.addItem("All" if not n else f"{n} per page", n)
        self.prev_btn = icon_button("chevron_left", "Previous page")
        self.page_label = tip(muted(""), "Which page of videos you're looking at")
        self.next_btn = icon_button("chevron_right", "Next page")
        self.prev_btn.clicked.connect(lambda: self.pager.set_page(self.pager.page - 1))
        self.next_btn.clicked.connect(lambda: self.pager.set_page(self.pager.page + 1))
        self.page_size.currentIndexChanged.connect(self._on_page_size)
        self.pager.pageChanged.connect(self._sync_pages)
        for w in (self.page_size, self.prev_btn, self.page_label, self.next_btn):
            brow.addWidget(w)
        brow.addSpacing(10)
        self.status = muted("")
        tip(self.status, "What Reelay is doing, and what to do next")
        brow.addWidget(self.status, 1)
        self.manifest_btn = QPushButton("Open Video List")
        tip(self.manifest_btn, "Every downloaded video in a table: search it, show files in Finder, copy links, "
                               "play them, or export the list as CSV or Markdown")
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
        sel.addAction("Select Highlighted Videos", lambda: self._check_selected(True))
        sel.addAction("Unselect Highlighted Videos", lambda: self._check_selected(False))
        sel.addSeparator()
        sel.addAction("Suggest Names for Highlighted Videos", lambda: self._auto_name(selected_only=True))
        sel.addAction("Rename Downloaded Files…", self._apply_names)
        sel.addAction("Download Settings…", self.settings_dialog.open_for_profile)
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

    def _row_menu(self, pos, view=None) -> None:
        """Right-click a video (or highlighted videos), in the grid or the list."""
        view = view or self.table
        index = view.indexAt(pos)
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
        menu.exec(view.viewport().mapToGlobal(pos))

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

    # ================================================================ grid / list
    def _build_grid(self) -> MediaGrid:
        grid = MediaGrid()
        grid.setModel(self.pager)
        grid.setSelectionModel(self.table.selectionModel())   # one selection, whichever view is showing
        self.card_delegate = LibraryCardDelegate(self, grid)
        self.scrub = ScrubCache(self)                 # hover a downloaded video's thumbnail to flip through it
        self.scrub.loaded.connect(lambda _key: grid.viewport().update())
        self.card_delegate.scrub = self.scrub
        grid.setItemDelegate(self.card_delegate)
        grid.checkToggled.connect(self._toggle_check_at)
        grid.setContextMenuPolicy(Qt.CustomContextMenu)
        grid.customContextMenuRequested.connect(lambda pos: self._row_menu(pos, grid))
        grid.doubleClicked.connect(self._toggle_check_at)
        space = QShortcut(QKeySequence(Qt.Key_Space), grid, context=Qt.WidgetShortcut)
        space.activated.connect(self._toggle_selected)
        for keys, step in ((QKeySequence.ZoomIn, 1), (QKeySequence.ZoomOut, -1), ("Ctrl+=", 1)):
            QShortcut(QKeySequence(keys), self, context=Qt.WidgetWithChildrenShortcut).activated.connect(
                lambda s=step: self.view_toggle.mode == "grid" and self.step_tile_size(s))
        return grid

    def set_tile_size(self, width: int) -> None:
        """Tile width in points (160–480), remembered."""
        width = max(TILE_MIN, min(TILE_MAX, int(width)))
        if self.tile_slider.value() != width:
            self.tile_slider.blockSignals(True)
            self.tile_slider.setValue(width)
            self.tile_slider.blockSignals(False)
        self.grid.set_card_min_width(width)
        self.store.set_setting("ui.library.tile_size", str(width))

    def step_tile_size(self, direction: int) -> None:
        """Jump to the next preset (Small 180, Medium 240, Large 320, XL 400) up or down."""
        current = self.grid.card_min_width
        presets = sorted(TILE_SIZES.values())
        bigger = [p for p in presets if p > current]
        smaller = [p for p in presets if p < current]
        target = (bigger[0] if bigger else presets[-1]) if direction > 0 else (smaller[-1] if smaller else presets[0])
        self.set_tile_size(target)

    def _view_index(self) -> int:
        return 2 if self.view_toggle.mode == "grid" else 1

    def _set_view(self, mode: str) -> None:
        self.tile_controls.setVisible(mode == "grid")
        if self.model.items():
            self.list_stack.setCurrentIndex(self._view_index())
        current = self.table.selectionModel().currentIndex()
        if current.isValid():
            (self.grid if mode == "grid" else self.table).scrollTo(current)

    def _toggle_check_at(self, index) -> None:
        item = self.model.item(self.pager.to_model_row(index))
        if not item.is_downloaded:
            self.model.set_checked([item.key], not item.checked)

    def _on_show_filter(self) -> None:
        self.proxy.set_state(self.show_filter.currentData())
        self.pager.set_page(0)
        self._update_counts()

    def _apply_sort(self) -> None:
        column, order = {"newest": (Col.DATE, Qt.DescendingOrder), "oldest": (Col.DATE, Qt.AscendingOrder),
                         "views": (Col.VIEWS, Qt.DescendingOrder), "longest": (Col.LENGTH, Qt.DescendingOrder),
                         "shortest": (Col.LENGTH, Qt.AscendingOrder),
                         "title": (Col.HOOK, Qt.AscendingOrder)}.get(self.sort_box.currentData(),
                                                                    (Col.DATE, Qt.DescendingOrder))
        self.table.sortByColumn(column, order)

    def _on_page_size(self) -> None:
        size = self.page_size.currentData()
        self.pager.set_size(size)
        self.store.set_setting("ui.library.page_size", str(size))

    def _sync_pages(self) -> None:
        pages = self.pager.pages
        self.page_label.setText(f"Page {self.pager.page + 1} of {pages}")
        for w in (self.prev_btn, self.page_label, self.next_btn):
            w.setVisible(pages > 1)
        self.prev_btn.setEnabled(self.pager.page > 0)
        self.next_btn.setEnabled(self.pager.page < pages - 1)

    def _sync_filter_count(self) -> None:
        brand, product = self.products_filter.brand.currentData(), self.products_filter.product.currentData()
        n = sum(bool(x) for x in (self.tag_filter.currentData(), self.show_filter.currentData(),
                                  brand or product))
        self.filter_btn.set_count(n)

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
        self.search.textChangedTo.connect(lambda text: (self.proxy.set_query(text), self.pager.set_page(0),
                                                        self._update_counts()))
        self.tag_filter.currentIndexChanged.connect(self._on_tag_filter)
        self.products_filter.changed.connect(self._on_product_filter)
        self.show_filter.currentIndexChanged.connect(self._on_show_filter)
        self.sort_box.currentIndexChanged.connect(self._apply_sort)
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
        rows = {self.pager.to_model_row(i) for i in self.table.selectionModel().selectedIndexes()}
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
        self.list_stack.setCurrentIndex(self._view_index() if items else 0)
        self.page_count.setText(f"{len(items)} video{'s' if len(items) != 1 else ''}"
                                + (f" · {sum(1 for i in items if i.is_downloaded)} downloaded" if items else "")
                                if items else "")
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
        self._sync_filter_count()
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
            self.inspector.set_title("Video")
            self._load_player(items[0])
            self.split.reveal()
        elif len(items) > 1:
            self.detail.show_multi(items)
            self.inspector.set_title(f"{len(items)} selected")
            self._load_player(None)
            self.split.reveal()
        else:
            self.detail.show_empty()
            self._load_player(None)
            self.split.dismiss()

    def _load_player(self, item: VideoItem | None) -> None:
        """The selected video in the player; changes only when the selection does."""
        self.media_pane.set_player_visible(item is not None)
        key = item.key if item is not None else None
        if key == self._player_key and (item is None or item.is_downloaded == (self.player.original is not None)):
            return
        self._player_key = key
        if item is None:
            self.player.clear()
            return
        from .. import previews
        path = item.path(self.folder) if item.is_downloaded else None
        path = path if path and path.is_file() else None
        self.player.set_video(path, self.thumbs.cover(item), previews.existing_preview(path) if path else None)

    def close_inspector(self) -> None:
        """✕ or Esc: deselect, which closes the details. Ticked videos stay ticked."""
        if self.split.is_open:
            self.table.clearSelection()
            self._refresh_detail()

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
        self._library_cache = None
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
        self._dir_mtimes: dict[Path, float | None] = {}             # one stat per folder per reload
        previous = {i.key: i.checked for i in self.model.items()}   # what the person selected so far
        by_key: dict[str, VideoItem] = {}
        archived: set[str] = set()
        for row in self.store.load_videos(pid, archived=None):
            if row.archived_at:
                archived.add(row.item.key)   # hidden, and never re-imported from the folder
                continue
            self._refresh_status(row.item)
            by_key[row.item.key] = row.item

        imported = []
        names = self.library.load_custom_names()
        for item in self._library_items():
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
        self._apply_sort()
        self._update_counts()
        self._refresh_detail()

    # ---------------------------------------------------------------- cached file checks
    def _dir_mtime(self, folder: Path) -> float | None:
        """A folder's modification time changes whenever a file is added, removed or renamed in it."""
        try:
            return folder.stat().st_mtime
        except OSError:
            return None

    def _refresh_status(self, item: VideoItem) -> None:
        """item.refresh_download_status, but each file is only checked again when its folder changed
        (synced and network folders make every check slow)."""
        path = item.path(self.folder)
        if path is None:
            item.refresh_download_status(self.folder)
            return
        stamp = self._dir_mtimes.setdefault(path.parent, self._dir_mtime(path.parent))
        cached = self._file_cache.get(str(path))
        if cached is None or cached[0] != stamp or stamp is None:
            cached = (stamp, path.is_file())
            self._file_cache[str(path)] = cached
        if cached[1]:
            item.status, item.checked = Status.DOWNLOADED, False
        elif item.status == Status.DOWNLOADED:
            item.status = Status.NEW

    def _library_items(self) -> list[VideoItem]:
        """Videos found in the folder's metadata, re-read only when that folder or the manifest changes."""
        stamp = (self._dir_mtime(self.library.metadata_dir), self._dir_mtime(self.library.manifest_path),
                 self._dir_mtime(self.folder))
        if self._library_cache is None or self._library_cache[0] != stamp or None in stamp[:1]:
            self._library_cache = (stamp, self.library.load())
        return [replace(i) for i in self._library_cache[1]]

    def open_output_folder(self) -> None:
        if self.profile:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.folder)))

    def open_manifest(self) -> None:
        """Downloaded Videos: the list inside Reelay (manifest.md is still kept up to date in the folder)."""
        if not self.library:
            return
        from .video_list_dialog import VideoListDialog
        items = [i for i in self.model.items() if i.is_downloaded and self._in_folder(i)]
        self.video_list = VideoListDialog(items, self.folder, self.thumbs, self.library.manifest_path,
                                          self.sources, self.failures, self)
        self.video_list.show()

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
            QMessageBox.warning(self, APP_NAME, "These links aren't TikTok, Instagram, YouTube or Snapchat:\n\n" + "\n".join(bad))
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
            hint = muted("No sources yet: paste a link, then Save This Link…")
            tip(hint, "Saved sources become one-click buttons here. You can also connect a YouTube channel under "
                      "Accounts.")
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
        """Rebuild the brand and product menus from the videos on the page, keeping the choice."""
        from ..products import video_products
        self.products_filter.set_videos(video_products(i) for i in self.model.items())

    def _on_product_filter(self, ids=None) -> None:
        self.proxy.set_product_ids(self.products_filter.selection())
        self.pager.set_page(0)
        self._update_counts()

    def filter_by_product(self, product_id: str) -> None:
        self._refresh_products()
        self.products_filter.select_product(product_id)

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
        self.pager.set_page(0)
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
        for timer in (self._tags_timer, self._products_timer):
            timer.stop()
        self.player.shutdown()
        self.scrub.shutdown()
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
