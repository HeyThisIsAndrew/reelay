"""App shell: sidebar with profile switcher + pages (Download, Board, Accounts, Settings)."""

from __future__ import annotations

import logging
import re

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QKeySequence, QPainter
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow,
                               QMenu, QMessageBox, QPushButton, QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from ..models import Profile
from ..paths import cache_dir, default_library_root, log_dir
from ..services import Services
from .accounts_page import AccountsPage
from .board_page import BoardPage
from .download_page import DownloadPage
from .help_page import HelpPage
from .settings_page import SettingsPage
from . import branding
from .profile_dialog import ProfileDialog
from .chrome import NavButton, UpNext, icon_button, retint_icon_buttons
from .theme import MODE_LABELS, MODES, apply_theme, set_prop
from .thumbnails import ThumbnailCache
from .tips import tip

log = logging.getLogger("reelay")

PAGES = [("download", "Library"), ("board", "Board"), ("automations", "Automations"), ("accounts", "Accounts"),
         ("settings", "Settings")]
NAV_ICONS = {"download": "library", "board": "board", "automations": "bolt", "accounts": "accounts",
             "settings": "settings", "help": "help"}
SIDEBAR_WIDE, SIDEBAR_RAIL = 212, 64
NAV_TIPS = {
    "download": "Your video library: bring videos in from TikTok, Instagram or YouTube",
    "board": "Your video tickets: write captions, check readiness, schedule and track posts",
    "automations": "Rules that work your tickets for you: templates, hashtags, platforms and scheduling",
    "accounts": "Connect the accounts videos go out to",
    "settings": "Appearance, automatic posting, tips and AI agents",
    "help": "How Reelay works, step by step (works offline)",
}


class BrandStrip(QWidget):
    """A thin bar in the profile's brand colour across the top of the sidebar: in every theme,
    a glance tells you whose profile you're in."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(4)
        self.color = ""

    def set_color(self, color: str) -> None:
        self.color = color
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        if self.color:
            p = QPainter(self)
            p.fillRect(self.rect(), QColor(self.color))
            p.end()


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
        self.help_page = HelpPage(store)
        self.help_page.navigate.connect(self.go)          # "Take me there" in the Help Center
        from .automations_page import AutomationsPage
        self.automations_page = AutomationsPage(self.pipeline)
        self.pages = {"download": self.download, "board": self.board, "automations": self.automations_page,
                      "accounts": self.accounts_page, "settings": self.settings_page, "help": self.help_page}

        self._build_ui()
        self._build_menu()
        self.rail_btn.setChecked(store.get_setting("ui.sidebar_compact", "0") == "1")
        self.download.videosChanged.connect(self.board.reload)
        self.download.busyChanged.connect(lambda _b: self._sync_switch_enabled())
        self.board.openAccounts.connect(lambda: self.go("accounts"))
        self.download.openAccounts.connect(lambda: self.go("accounts"))
        self.accounts_page.listSource.connect(lambda view: (self.go("download"), self.download.fetch_source(view)))
        self.accounts_page.listLinks.connect(lambda links: (self.go("download"), self.download.fetch_links(links)))
        self.settings_page.themeChosen.connect(self.set_theme)
        self.settings_page.editProfileRequested.connect(self.edit_profile)

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
        side.setFixedWidth(SIDEBAR_WIDE)
        self.sidebar = side
        outer_side = QVBoxLayout(side)
        outer_side.setContentsMargins(0, 0, 0, 0)
        outer_side.setSpacing(0)
        self.brand_strip = BrandStrip()
        outer_side.addWidget(self.brand_strip)
        sl = QVBoxLayout()
        sl.setContentsMargins(10, 12, 10, 12)
        sl.setSpacing(4)
        outer_side.addLayout(sl, 1)
        # Who you're working as: the profile's logo and name (wrapping, never cut off). Click to switch.
        self.profile_btn = QToolButton(objectName="ProfileButton")
        self.profile_btn.setPopupMode(QToolButton.InstantPopup)
        head = QHBoxLayout(self.profile_btn)
        head.setContentsMargins(8, 6, 8, 6)
        head.setSpacing(10)
        self.profile_logo = QLabel()
        self.profile_logo.setFixedSize(32, 32)
        self.profile_name = QLabel(objectName="ProfileName")
        self.profile_name.setWordWrap(True)
        from .widgets import LineLabel
        self.tagline = LineLabel("", "BrandTagline")          # the profile's tagline: one line under its name
        self.tagline.setVisible(False)
        for lab in (self.profile_logo, self.profile_name, self.tagline):
            lab.setAttribute(Qt.WA_TransparentForMouseEvents)
        head.addWidget(self.profile_logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(self.profile_name)
        names.addWidget(self.tagline)
        head.addLayout(names, 1)
        self.profile_btn.setMinimumHeight(48)
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
        self.nav_buttons: dict[str, NavButton] = {}
        for key, label in PAGES + [("help", "Help")]:
            b = NavButton(label, NAV_ICONS[key])
            b.clicked.connect(lambda _=False, k=key: self.go(k))
            tip(b, NAV_TIPS[key])
            self.nav.addButton(b)
            self.nav_buttons[key] = b
            if key == "help":
                sl.addStretch()
                self.up_next = UpNext()
                self.up_next.clicked.connect(lambda: self.go("board"))
                sl.addWidget(self.up_next)
                sl.addSpacing(8)
            sl.addWidget(b)
        sl.addSpacing(6)
        foot_row = QHBoxLayout()
        foot_row.setContentsMargins(10, 0, 0, 0)
        self.version_label = QLabel(f"{APP_NAME} {__version__}")
        self.version_label.setProperty("faint", True)
        self.rail_btn = icon_button("sidebar", "Collapse the sidebar to icons (or expand it again)")
        self.rail_btn.setCheckable(True)
        self.rail_btn.toggled.connect(self.set_sidebar_compact)
        foot_row.addWidget(self.version_label, 1)
        foot_row.addWidget(self.rail_btn)
        sl.addLayout(foot_row)
        self._up_next_timer = QTimer(self, interval=60_000, timeout=self._refresh_up_next)
        self._up_next_timer.start()
        h.addWidget(side)

        self.stack = QStackedWidget()
        for page in self.pages.values():
            self.stack.addWidget(page)
        h.addWidget(self.stack, 1)
        outer = QWidget()
        ov = QVBoxLayout(outer)
        ov.setContentsMargins(0, 0, 0, 0)
        ov.setSpacing(0)
        self.banner = QFrame(objectName="Card")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(16, 8, 16, 8)
        self.banner_text = QLabel()
        self.banner_text.setWordWrap(True)
        self.banner_btn = QPushButton("Edit Now")
        set_prop(self.banner_btn, "primary")
        self._banner_action = None
        self.banner_btn.clicked.connect(lambda: self._banner_action and self._banner_action())
        bl.addWidget(self.banner_text, 1)
        bl.addWidget(self.banner_btn)
        self.banner.setVisible(False)
        ov.addWidget(self.banner)
        ov.addWidget(root, 1)
        self.setCentralWidget(outer)

    def _build_menu(self) -> None:
        mb = self.menuBar()
        f = mb.addMenu("File")
        self._action(f, "New Profile…", self.new_profile, "Ctrl+Shift+N")
        f.addSeparator()
        self._action(f, "Choose Output Folder…", self.download.choose_folder, QKeySequence.StandardKey.Open)
        self._action(f, "Open Output Folder", self.download.open_output_folder)
        self._action(f, "Downloaded Videos…", self.download.open_manifest, "Ctrl+Shift+L")
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
        for name in MODES:
            a = QAction(MODE_LABELS[name], self, checkable=True)
            a.setData(name)
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

    # ---------------------------------------------------------------- sidebar
    def set_sidebar_compact(self, compact: bool) -> None:
        """Icons only (more room for videos), or icons with labels."""
        self.sidebar.setFixedWidth(SIDEBAR_RAIL if compact else SIDEBAR_WIDE)
        for b in self.nav_buttons.values():
            b.set_compact(compact)
        for w in (self.profile_name, self.version_label):
            w.setVisible(not compact)
        self.tagline.setVisible(not compact and bool(self.tagline.full_text()))
        self.up_next.setMaximumHeight(0 if compact else 16777215)
        self.store.set_setting("ui.sidebar_compact", "1" if compact else "0")

    def _refresh_up_next(self) -> None:
        what, when = self.board.next_up() if self.profile else ("", None)
        self.up_next.set_next(what, when)

    # ---------------------------------------------------------------- navigation
    def set_view_only(self, text: str, button: str = "", on_click=None) -> None:
        """Show (or with text="" hide) the banner across the top, e.g. for a shared database
        someone else is editing."""
        self.banner.setVisible(bool(text))
        self.banner_text.setText(text)
        self.banner_btn.setVisible(bool(button))
        self.banner_btn.setText(button)
        self._banner_action = on_click

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
            self._refresh_up_next()
        elif page is self.accounts_page:
            self.accounts_page.refresh()
        elif page is self.automations_page:
            self.automations_page.refresh()

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
        changed = self.profile is None or self.profile.id != profile.id
        self.profile = profile
        self.store.set_active_profile(profile.id)
        self.setWindowTitle(f"{APP_NAME} · {profile.name}")
        if changed and self.store.get_setting("theme", "dark") == "custom":
            self._apply_theme("custom")          # Custom follows each profile's brand colours
        self._show_brand()
        for page in self.pages.values():
            if hasattr(page, "set_profile"):
                page.set_profile(profile)
        self._refresh_up_next()

    def _show_brand(self) -> None:
        """The active profile's logo, name, tagline and colour strip in the sidebar."""
        profile = self.profile
        if profile is None:
            return
        brand = branding.load(self.store, profile.id)
        self.profile_name.setText(profile.name)
        self.profile_logo.setPixmap(branding.avatar_pixmap(profile, brand, 32))
        self.profile_name.ensurePolished()        # bold from the stylesheet changes how it wraps
        self.profile_btn.setMinimumHeight(max(48, self.profile_btn.layout().totalHeightForWidth(184)))
        self.tagline.set_full(brand.tagline)
        self.tagline.setVisible(bool(brand.tagline) and not self.rail_btn.isChecked())
        self.brand_strip.set_color(branding.strip_color(self.store, profile))

    def _fill_profile_menu(self, menu: QMenu | None = None) -> None:
        menu = menu or self.profile_menu
        menu.clear()
        for n, p in enumerate(self.store.list_profiles(), 1):
            a = QAction(branding.avatar_icon(p, branding.load(self.store, p.id), 18), p.name, menu, checkable=True)
            a.setChecked(self.profile is not None and p.id == self.profile.id)
            if n <= 9:
                a.setShortcut(QKeySequence(f"Ctrl+Alt+{n}"))
            a.triggered.connect(lambda _=False, prof=p: self.switch_profile(prof))
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction("New Profile…", self.new_profile)
        menu.addAction("Edit Profile && Branding…", self.edit_profile)
        delete = menu.addAction("Delete Profile…", self.delete_profile)
        delete.setEnabled(len(self.store.list_profiles()) > 1)

    def new_profile(self) -> None:
        dialog = ProfileDialog(self.store, None, self.store.get_setting("theme", "dark") == "custom", self)
        if dialog.exec() != ProfileDialog.Accepted:
            return
        name, brand, use_custom = dialog.values()
        folder_name = re.sub(r'[<>:"/\\|?*]', "", name).strip() or "Profile"
        profile = self.store.create_profile(name, default_library_root() / folder_name,
                                            color=brand.accent or None)
        branding.save(self.store, profile.id, brand)
        self.switch_profile(profile)
        self._set_custom(use_custom)
        self.go("download")

    def edit_profile(self) -> None:
        """Name, tagline, logo and brand colours of the current profile."""
        if not self.profile:
            return
        dialog = ProfileDialog(self.store, self.profile, self.store.get_setting("theme", "dark") == "custom", self)
        if dialog.exec() != ProfileDialog.Accepted:
            return
        name, brand, use_custom = dialog.values()
        self.profile.name = name
        if brand.accent:
            self.profile.color = brand.accent     # the initials avatar matches the brand when there's no logo
        self.store.update_profile(self.profile)
        branding.save(self.store, self.profile.id, brand)
        self.switch_profile(self.profile)
        if not self._set_custom(use_custom) and self.store.get_setting("theme", "dark") == "custom":
            self.set_theme("custom")              # new colours, same theme: repaint in them

    rename_profile = edit_profile

    def _set_custom(self, on: bool) -> bool:
        """Turn the Custom theme on or off as the profile dialog asked. True if the theme changed."""
        current = self.store.get_setting("theme", "dark")
        if on and current != "custom":
            self.set_theme("custom")
            return True
        if not on and current == "custom":
            self.set_theme("dark")
            return True
        return False

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
        branding.forget(self.store, self.profile.id)
        self.store.delete_profile(self.profile.id)
        self.switch_profile(self.store.default_profile())

    # ---------------------------------------------------------------- theme
    def _apply_theme(self, name: str) -> None:
        apply_theme(QApplication.instance(), name, branding.custom_colors(self.store, self.profile))

    def set_theme(self, name: str) -> None:
        name = name if name in MODES else "dark"
        self._apply_theme(name)
        self.store.set_setting("theme", name)
        for a in self.theme_actions.actions():
            a.setChecked(a.data() == name)
        self.settings_page.refresh()
        self._show_brand()
        retint_icon_buttons(self)
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
