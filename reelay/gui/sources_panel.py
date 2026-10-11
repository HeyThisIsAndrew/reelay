"""Download sources, one section per platform: where a profile's videos come IN from.

``SourcesPanel`` is shared by the Library's "Download Sources" window and the Accounts page. Each platform
section lists its saved sources (and, for YouTube, the connected channels) with one-click List Videos, and
has one field that takes either a username (``@khaby.lame``) or a profile link. Snapchat has no profile
listing, so its section takes Spotlight links to list right away instead.
"""

from __future__ import annotations

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMenu, QMessageBox, QPushButton, QScrollArea, QToolButton, QVBoxLayout,
                               QWidget)

from .. import APP_NAME
from ..models import Profile
from ..platforms import detect_platform, is_supported_url
from ..sources import SOURCE_PLATFORMS, SourceError, SourceService, SourceView, normalize_url, resolve_source_input
from ..store import Store
from .bridge import change_bridge
from .logos import platform_pixmap
from .tips import tip

PLATFORMS = ("tiktok", "instagram", "youtube", "snapchat")
LABELS = {"tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube", "snapchat": "Snapchat"}
PLACEHOLDERS = {"tiktok": "@username or profile link", "instagram": "@username or profile link",
                "youtube": "@handle, channel ID or channel link", "snapchat": "Paste Spotlight links"}


def _muted(text: str, prop: str = "muted") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty(prop, True)
    return label


class SourceRow(QFrame):
    """One saved source or connected channel: name and link, Mine, List Videos, ⋯."""

    def __init__(self, view: SourceView, panel: "SourcesPanel"):
        super().__init__(objectName="AccountRow")
        self.view = view
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 6, 6)
        lay.setSpacing(8)
        detail = "Connected channel" if view.account else (view.url or "")
        name = QLabel(f"<b>{html.escape(view.name)}</b><br><span style='font-size:small'>"
                      f"{html.escape(detail)}</span>")
        name.setTextFormat(Qt.RichText)
        name.setToolTip((view.url or "") + (f"\nSaved {view.duplicates + 1} times" if view.duplicates else ""))
        lay.addWidget(name, 1)
        self.mine_btn = None
        if view.account is None:
            self.mine_btn = QPushButton("Mine", objectName="MineToggle")
            self.mine_btn.setCheckable(True)
            self.mine_btn.setChecked(view.mine)
            self.mine_btn.setCursor(Qt.PointingHandCursor)
            tip(self.mine_btn, "On: this is one of this profile's own accounts, so its videos are recorded as "
                               "your own content. Off: an account you manage or have permission to use.")
            self.mine_btn.toggled.connect(lambda on: panel.set_mine(self.view, on))
            lay.addWidget(self.mine_btn)
        self.fetch_btn = QPushButton("List Videos")
        tip(self.fetch_btn, f"Show every video from {view.name} in the Library, so you can choose which to "
                            "download. Nothing is downloaded yet.")
        self.fetch_btn.clicked.connect(lambda: panel.fetchRequested.emit(self.view))
        lay.addWidget(self.fetch_btn)
        more = QToolButton(text="⋯")
        more.setPopupMode(QToolButton.InstantPopup)
        tip(more, "Rename or remove this source")
        menu = QMenu(more)
        if view.account:
            if panel.show_manage:
                menu.addAction("Manage This Channel in Accounts", panel.manageAccounts.emit)
            else:
                more.setVisible(False)
        else:
            menu.addAction("Rename This Source…", lambda: panel.rename(self.view))
            menu.addSeparator()
            menu.addAction("Remove This Source", lambda: panel.remove(self.view))
        more.setMenu(menu)
        lay.addWidget(more)


class PlatformSources(QFrame):
    """One platform: its logo and name, its sources, and a field for a username or link."""

    def __init__(self, platform: str, panel: "SourcesPanel"):
        super().__init__(objectName="SourcePlatform")
        self.platform, self.panel = platform, panel
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(platform_pixmap(platform, 26))
        logo.setFixedSize(28, 28)
        head.addWidget(logo)
        head.addWidget(QLabel(LABELS[platform], objectName="DetailTitle"))
        self.count = _muted("", "faint")
        head.addWidget(self.count)
        head.addStretch()
        lay.addLayout(head)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(6)
        lay.addLayout(self.rows)
        add = QHBoxLayout()
        add.setSpacing(8)
        self.field = QLineEdit()
        self.field.setPlaceholderText(PLACEHOLDERS[platform])
        self.field.setClearButtonEnabled(True)
        self.field.setAttribute(Qt.WA_MacShowFocusRect, False)
        tip(self.field, f"A {LABELS[platform]} username, with or without @, or the profile's link"
            if platform in SOURCE_PLATFORMS else "One or more Snapchat Spotlight links, separated by spaces")
        add.addWidget(self.field, 1)
        if platform in SOURCE_PLATFORMS:
            self.add_btn = QPushButton("Add")
            tip(self.add_btn, f"Save this {LABELS[platform]} account as a source for this profile. Type the "
                              "username (with or without @) or paste its profile link.")
            self.add_btn.clicked.connect(self._add)
            self.field.returnPressed.connect(self._add)
        else:
            self.add_btn = QPushButton("List Videos")
            tip(self.add_btn, "Show these Spotlight videos in the Library, so you can download them")
            self.add_btn.clicked.connect(self._list_links)
            self.field.returnPressed.connect(self._list_links)
        add.addWidget(self.add_btn)
        lay.addLayout(add)
        self.error = _muted("", "danger")
        self.error.setVisible(False)
        lay.addWidget(self.error)
        self.field.textChanged.connect(lambda: self.error.setVisible(False))
        if platform == "snapchat":
            lay.addWidget(_muted("Snapchat doesn't let apps list a profile's videos, so paste the links of the "
                                 "Spotlight videos you want (Share → Copy Link).", "faint"))

    def set_views(self, views: list[SourceView], busy: bool) -> None:
        while self.rows.count():
            w = self.rows.takeAt(0).widget()
            if w:
                w.hide()               # off screen now, not when Qt gets round to deleting it
                w.deleteLater()
        for view in views:
            row = SourceRow(view, self.panel)
            row.fetch_btn.setEnabled(not busy)
            self.rows.addWidget(row)
        self.count.setText(f"{len(views)} source{'s' if len(views) != 1 else ''}" if views else "")
        if not views and self.platform in SOURCE_PLATFORMS:
            self.rows.addWidget(_muted("No sources yet.", "faint"))

    def show_error(self, message: str) -> None:
        self.error.setText(message)
        self.error.setVisible(True)

    def _add(self) -> None:
        text = self.field.text().strip()
        if not text:
            self.field.setFocus()
            return
        try:
            self.panel.add(self.platform, text)
        except SourceError as exc:
            self.show_error(str(exc))
            return
        self.field.clear()

    def _list_links(self) -> None:
        links = [u if "://" in u else "https://" + u for u in self.field.text().split() if u.strip()]
        bad = [u for u in links if not is_supported_url(u) or detect_platform(u) != "Snapchat"]
        if not links or bad:
            self.show_error("Paste one or more Snapchat Spotlight links (snapchat.com/spotlight/…)."
                            if links else "Paste the Spotlight links first.")
            return
        self.field.clear()
        self.panel.linksRequested.emit(links)


class SourcesPanel(QWidget):
    fetchRequested = Signal(object)    # a SourceView to list
    linksRequested = Signal(list)      # links to list once, not saved (Snapchat Spotlight)
    manageAccounts = Signal()

    def __init__(self, store: Store, service: SourceService, show_manage: bool = True, parent=None):
        super().__init__(parent)
        self.store, self.service, self.show_manage = store, service, show_manage
        self.profile: Profile | None = None
        self.busy = False
        self._columns = 0
        self.sections = {p: PlatformSources(p, self) for p in PLATFORMS}
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(12)
        self.set_columns(1)
        change_bridge(store).changed.connect(self._on_store_change)

    def set_columns(self, columns: int) -> None:
        if columns == self._columns:
            return
        self._columns = columns
        for section in self.sections.values():
            self.grid.removeWidget(section)
        for n, platform in enumerate(PLATFORMS):
            self.grid.addWidget(self.sections[platform], n // columns, n % columns, Qt.AlignTop)
        for c in range(2):
            self.grid.setColumnStretch(c, 1 if c < columns else 0)

    def set_profile(self, profile: Profile | None) -> None:
        self.profile = profile
        self.refresh()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        for row in self.findChildren(SourceRow):
            row.fetch_btn.setEnabled(not busy)
        self.sections["snapchat"].add_btn.setEnabled(not busy)

    def views(self) -> list[SourceView]:
        return self.service.display_sources(self.profile.id) if self.profile else []

    def refresh(self) -> None:
        views = self.views()
        for platform, section in self.sections.items():
            section.set_views([v for v in views if v.platform == platform], self.busy)

    def rows(self) -> list[SourceRow]:
        """The rows on screen, platform by platform (not ones waiting to be deleted)."""
        rows = []
        for platform in PLATFORMS:
            layout = self.sections[platform].rows
            rows += [layout.itemAt(i).widget() for i in range(layout.count())
                     if isinstance(layout.itemAt(i).widget(), SourceRow)]
        return rows

    def prefill(self, text: str) -> None:
        """Offer an unsaved profile link (e.g. the one in the Library's link field) in its platform's field."""
        text = (text or "").strip()
        if not text or len(text.split()) > 1 or not is_supported_url(text if "://" in text else "https://" + text):
            return
        platform = detect_platform(text if "://" in text else "https://" + text).lower()
        if platform not in SOURCE_PLATFORMS or "/video/" in text or "/reel/" in text or "watch?" in text:
            return
        if any(v.url and normalize_url(v.url) == normalize_url(text) for v in self.views()):
            return
        self.sections[platform].field.setText(text)

    def _on_store_change(self, change) -> None:
        if change.entity in ("source", "account") and self.profile and change.profile_id in (None, self.profile.id):
            self.refresh()

    # ---------------------------------------------------------------- actions
    def add(self, platform: str, text: str) -> None:
        if not self.profile:
            raise SourceError("Choose a profile first.")
        url = resolve_source_input(platform, text)
        if any(v.url and normalize_url(v.url) == normalize_url(url) for v in self.views()):
            raise SourceError("That account is already saved.")
        self.service.add(self.profile.id, url)

    def set_mine(self, view: SourceView, mine: bool) -> None:
        if view.mine != mine:
            self.service.set_mine(view.source_id, mine)

    def rename(self, view: SourceView) -> None:
        name, ok = QInputDialog.getText(self, "Rename This Source", "New name for this source:", text=view.name)
        if ok:
            try:
                self.service.rename(view.source_id, name)
            except SourceError as exc:
                QMessageBox.warning(self, APP_NAME, str(exc))

    def remove(self, view: SourceView, confirm: bool = True) -> None:
        extra = f" (and {view.duplicates} duplicate{'s' if view.duplicates != 1 else ''})" if view.duplicates else ""
        if confirm and QMessageBox.question(self, "Remove Source", f"Remove “{view.name}”{extra} from this "
                                            "profile's sources?\n\nVideos already listed or downloaded "
                                            "stay.") != QMessageBox.Yes:
            return
        target = normalize_url(view.url)
        for other in self.service.all_sources(self.profile.id):
            if other.kind == "saved" and normalize_url(other.url) == target:
                self.service.remove(other.source_id)


class SourcesDialog(QDialog):
    """The Library's "Download Sources" window: pick a source to list, or add one by username or link."""

    def __init__(self, store: Store, service: SourceService, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Download Sources")
        self.setMinimumSize(620, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 14)
        lay.setSpacing(8)
        lay.addWidget(QLabel("Download Sources", objectName="PageTitle"))
        self.subtitle = _muted("")
        lay.addWidget(self.subtitle)
        self.panel = SourcesPanel(store, service, show_manage=True)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget(objectName="CardBody")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 6, 0, 6)
        bl.addWidget(self.panel)
        bl.addStretch()
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)
        lay.addWidget(_muted("Only import media you own or have permission to manage.", "faint"))
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        tip(buttons.button(QDialogButtonBox.Close), "Close this window. Sources are saved as you add them.")
        lay.addWidget(buttons)
        for signal in (self.panel.fetchRequested, self.panel.linksRequested, self.panel.manageAccounts):
            signal.connect(lambda *_: self.accept())

    def open_for(self, profile: Profile, pasted: str = "") -> None:
        self.subtitle.setText(f"Where {profile.name}'s videos come in from. Type a username or paste a profile "
                              "link, then click List Videos on the one you want.")
        self.panel.set_profile(profile)
        for section in self.panel.sections.values():
            section.field.clear()
            section.error.setVisible(False)
        self.panel.prefill(pasted)
        self.open()


def apply_button_state(button: QPushButton, view: SourceView | None) -> None:
    """The Library's source button: the current source's logo and name, or an invitation to choose one."""
    button.setText(f"{view.name}  ▾" if view else "Download Sources  ▾")
    button.setIcon(QIcon(platform_pixmap(view.platform, 18)) if view else QIcon())
