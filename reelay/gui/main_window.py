"""App shell: sidebar with profile switcher + pages (Download, Board, Accounts, Settings)."""

from __future__ import annotations

import logging
import re

from PySide6.QtCore import QRectF, QSettings, QSize, Qt, QUrl
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QButtonGroup, QHBoxLayout, QInputDialog, QLabel, QMainWindow, QMenu,
                               QMessageBox, QPushButton, QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from ..models import Profile
from ..paths import cache_dir, default_library_root, log_dir
from ..services import Services
from .accounts_page import AccountsPage
from .board_page import BoardPage
from .download_page import DownloadPage
from .help_page import HelpPage
from .settings_page import SettingsPage
from .theme import THEMES, apply_theme, set_prop
from .thumbnails import ThumbnailCache
from .tips import tip

log = logging.getLogger("reelay")

PAGES = [("download", "Download"), ("board", "Board"), ("accounts", "Accounts"), ("settings", "Settings")]
NAV_TIPS = {
    "download": "Bring videos in from TikTok, Instagram or YouTube",
    "board": "Your video tickets: write captions, check readiness, schedule and track posts",
    "accounts": "Connect the accounts videos go out to",
    "settings": "Appearance, automatic posting, tips and AI agents",
    "help": "How Reelay works, step by step (works offline)",
}


def avatar_icon(profile: Profile, size: int = 26) -> QIcon:
    ratio = 2
    pix = QPixmap(size * ratio, size * ratio)
    pix.setDevicePixelRatio(ratio)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(profile.color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(QRectF(0, 0, size, size))
    font = QFont()
    font.setPixelSize(int(size * 0.42))
    font.setWeight(QFont.DemiBold)
    p.setFont(font)
    p.setPen(QColor("#ffffff"))
    p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, profile.initials)
    p.end()
    return QIcon(pix)


class MainWindow(QMainWindow):
    def __init__(self, services: Services):
        super().__init__()
        store = services.store
        self.setWindowTitle(APP_NAME)
        self.resize(1440, 900)
        self.setMinimumSize(1100, 680)
        self.services = services
        self.store = store
        self.accounts = services.accounts
        self.uploads = services.uploads
        self.pipeline = services.pipeline
        self.thumbs = ThumbnailCache(cache_dir() / "covers", self)
        self.profile: Profile | None = None

        self.download = DownloadPage(store, self.thumbs, services.accounts)
        self.board = BoardPage(self.pipeline, self.uploads, self.thumbs)
        self.accounts_page = AccountsPage(store, self.accounts)
        self.settings_page = SettingsPage(store, services)
        self.help_page = HelpPage()
        self.pages = {"download": self.download, "board": self.board,
                      "accounts": self.accounts_page, "settings": self.settings_page, "help": self.help_page}

        self._build_ui()
        self._build_menu()
        self.download.videosChanged.connect(self.board.reload)
        self.download.busyChanged.connect(lambda _b: self._sync_switch_enabled())
        self.board.openAccounts.connect(lambda: self.go("accounts"))
        self.download.openAccounts.connect(lambda: self.go("accounts"))
        self.settings_page.themeChosen.connect(self.set_theme)

        geo = QSettings().value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        self.switch_profile(store.active_profile(default_library_root() / "My Channel"))
        self.go(store.get_setting("last_page", "download"))

    # ---------------------------------------------------------------- layout
    def _build_ui(self) -> None:
        root = QWidget()
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        side = QWidget(objectName="Sidebar")
        side.setFixedWidth(188)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(10, 14, 10, 12)
        sl.setSpacing(4)
        self.profile_btn = QToolButton(objectName="ProfileButton")
        self.profile_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.profile_btn.setIconSize(QSize(26, 26))
        self.profile_btn.setPopupMode(QToolButton.InstantPopup)
        self.profile_btn.setSizePolicy(self.profile_btn.sizePolicy().horizontalPolicy().Expanding,
                                       self.profile_btn.sizePolicy().verticalPolicy())
        self.profile_menu = QMenu(self)
        self.profile_menu.aboutToShow.connect(self._fill_profile_menu)
        self.profile_btn.setMenu(self.profile_menu)
        tip(self.profile_btn, "The current profile. Click to switch: each creator or channel gets their own "
                              "videos, board and accounts.")
        sl.addWidget(self.profile_btn)
        sl.addSpacing(14)

        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        self.nav_buttons: dict[str, QPushButton] = {}
        for key, label in PAGES:
            b = QPushButton(label)
            set_prop(b, "nav")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, k=key: self.go(k))
            tip(b, NAV_TIPS[key])
            self.nav.addButton(b)
            self.nav_buttons[key] = b
            sl.addWidget(b)
        sl.addStretch()
        help_btn = QPushButton("Help")
        set_prop(help_btn, "nav")
        help_btn.setCheckable(True)
        help_btn.clicked.connect(lambda: self.go("help"))
        tip(help_btn, NAV_TIPS["help"])
        self.nav.addButton(help_btn)
        self.nav_buttons["help"] = help_btn
        sl.addWidget(help_btn)
        sl.addSpacing(6)
        foot = QLabel(f"{APP_NAME} {__version__}")
        foot.setProperty("faint", True)
        foot.setContentsMargins(10, 0, 0, 0)
        sl.addWidget(foot)
        h.addWidget(side)

        self.stack = QStackedWidget()
        for page in self.pages.values():
            self.stack.addWidget(page)
        h.addWidget(self.stack, 1)
        self.setCentralWidget(root)

    def _build_menu(self) -> None:
        mb = self.menuBar()
        f = mb.addMenu("File")
        self._action(f, "New Profile…", self.new_profile, "Ctrl+Shift+N")
        f.addSeparator()
        self._action(f, "Choose Output Folder…", self.download.choose_folder, QKeySequence.StandardKey.Open)
        self._action(f, "Open Output Folder", self.download.open_output_folder)
        self._action(f, "Open Video List (manifest.md)", self.download.open_manifest)
        f.addSeparator()
        self._action(f, "Import from Spreadsheet…", lambda: (self.go("board"), self.board.open_import()),
                     "Ctrl+Shift+I")
        self._action(f, "Archived Videos…", lambda: (self.go("board"), self.board.open_archived()))

        v = mb.addMenu("View")
        for n, (key, label) in enumerate(PAGES, 1):
            self._action(v, label, lambda k=key: self.go(k), f"Ctrl+{n}")
        v.addSeparator()
        tm = v.addMenu("Theme")
        self.theme_actions = QActionGroup(self)
        for name, pal in THEMES.items():
            a = QAction(pal.label, self, checkable=True)
            a.triggered.connect(lambda _=False, n=name: self.set_theme(n))
            self.theme_actions.addAction(a)
            tm.addAction(a)
            a.setChecked(name == self.store.get_setting("theme", "dark"))

        self.profiles_menu = mb.addMenu("Profile")
        self.profiles_menu.aboutToShow.connect(lambda: self._fill_profile_menu(self.profiles_menu))

        hm = mb.addMenu("Help")
        self._action(hm, f"{APP_NAME} Help", lambda: self.go("help"), "Ctrl+?")
        hm.addSeparator()
        self._action(hm, "Custom Naming Patterns", self.download.show_tokens_help)
        self._action(hm, "Open Log Folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir()))))

    def _action(self, menu: QMenu, text: str, slot, shortcut=None) -> QAction:
        a = QAction(text, self)
        if shortcut is not None:
            a.setShortcut(QKeySequence(shortcut))
        a.triggered.connect(lambda _=False: slot())
        menu.addAction(a)
        return a

    # ---------------------------------------------------------------- navigation
    def check_interrupted(self) -> None:
        """Ask about sends that were cut off or missed (every profile)."""
        from .recovery_dialog import RecoveryDialog
        held = self.services.pipeline.interrupted()
        if not held:
            return
        dialog = RecoveryDialog(self.services.pipeline, held, self)
        dialog.exec()
        self.board.reload()

    def go(self, key: str) -> None:
        page = self.pages.get(key, self.download)
        key = next(k for k, p in self.pages.items() if p is page)
        self.stack.setCurrentWidget(page)
        self.nav_buttons[key].setChecked(True)
        if key != "help":
            self.store.set_setting("last_page", key)
        if page is self.board:
            self.board.reload()
            self.board.sync()
        elif page is self.accounts_page:
            self.accounts_page.refresh()

    # ---------------------------------------------------------------- profiles
    def _busy_reason(self) -> str:
        # Uploads run app-wide (UploadManager), so they don't block switching.
        if self.download.busy:
            return "Wait for the current download to finish (or stop it) before switching profiles."
        return ""

    def _sync_switch_enabled(self) -> None:
        self.profile_btn.setEnabled(not self._busy_reason())

    def switch_profile(self, profile: Profile) -> None:
        reason = self._busy_reason()
        if reason:
            QMessageBox.information(self, APP_NAME, reason)
            return
        self.profile = profile
        self.store.set_active_profile(profile.id)
        self.profile_btn.setText(f"  {profile.name}")
        self.profile_btn.setIcon(avatar_icon(profile))
        self.setWindowTitle(f"{APP_NAME} · {profile.name}")
        for page in self.pages.values():
            if hasattr(page, "set_profile"):
                page.set_profile(profile)

    def _fill_profile_menu(self, menu: QMenu | None = None) -> None:
        menu = menu or self.profile_menu
        menu.clear()
        for n, p in enumerate(self.store.list_profiles(), 1):
            a = QAction(avatar_icon(p, 18), p.name, menu, checkable=True)
            a.setChecked(self.profile is not None and p.id == self.profile.id)
            if n <= 9:
                a.setShortcut(QKeySequence(f"Ctrl+Alt+{n}"))
            a.triggered.connect(lambda _=False, prof=p: self.switch_profile(prof))
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction("New Profile…", self.new_profile)
        menu.addAction("Rename Profile…", self.rename_profile)
        delete = menu.addAction("Delete Profile…", self.delete_profile)
        delete.setEnabled(len(self.store.list_profiles()) > 1)

    def new_profile(self) -> None:
        name, ok = QInputDialog.getText(self, "New Profile",
                                        "Name this profile (e.g. the creator or channel it's for):")
        name = name.strip()
        if not ok or not name:
            return
        folder_name = re.sub(r'[<>:"/\\|?*]', "", name).strip() or "Profile"
        profile = self.store.create_profile(name, default_library_root() / folder_name)
        self.switch_profile(profile)
        self.go("download")

    def rename_profile(self) -> None:
        if not self.profile:
            return
        name, ok = QInputDialog.getText(self, "Rename Profile", "Profile name:", text=self.profile.name)
        if ok and name.strip():
            self.profile.name = name.strip()
            self.store.update_profile(self.profile)
            self.switch_profile(self.profile)

    def delete_profile(self) -> None:
        if not self.profile or len(self.store.list_profiles()) <= 1:
            return
        answer = QMessageBox.question(
            self, "Delete Profile",
            f"Delete the profile “{self.profile.name}”?\n\nIts board, drafts and connected accounts are removed "
            f"from Reelay. Downloaded video files stay in {self.profile.output_folder}.")
        if answer != QMessageBox.Yes:
            return
        for account in self.store.list_accounts(self.profile.id):
            self.accounts.disconnect(account)
        self.store.delete_profile(self.profile.id)
        self.switch_profile(self.store.default_profile())

    # ---------------------------------------------------------------- theme
    def set_theme(self, name: str) -> None:
        apply_theme(QApplication.instance(), name)
        self.store.set_setting("theme", name)
        for a in self.theme_actions.actions():
            a.setChecked(a.text() == THEMES[name].label)
        self.settings_page.refresh()
        if self.profile:
            self.profile_btn.setIcon(avatar_icon(self.profile))
        self.download.model.refresh_all()
        for w in QApplication.allWidgets():
            w.update()

    # ---------------------------------------------------------------- close
    def closeEvent(self, event) -> None:
        busy = []
        if self.download.busy:
            busy.append("a download")
        if self.uploads.busy:
            busy.append("a YouTube upload")
        if busy:
            answer = QMessageBox.question(self, APP_NAME, f"{' and '.join(busy).capitalize()} is in progress. "
                                                          "Stop and quit?")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self.download.shutdown()
        self.board.shutdown()
        self.services.stop()
        self.accounts_page.shutdown()
        QSettings().setValue("geometry", self.saveGeometry())
        super().closeEvent(event)
