"""Accounts page: where videos go OUT, as an even grid of integration cards.

The six platforms you can sign in to (YouTube, TikTok, Instagram, Bluesky, Threads, Facebook) are a grid that
is always full: 3 x 2, 2 x 3 or 1 x 6 (grid.balanced_columns), every card the same height. Each card shows
the platform, whether it's connected and what Reelay does there, with one action. Nothing grows a card: long
help is behind Learn more, several accounts collapse to "N connected · Manage…", and Advanced Setup (your
own developer app and keys) opens as its own window. Connecting is one click with Reelay's own apps where
this build has them; Bluesky connects with an App Password. Snapchat has no sign-in (it's posted by hand),
so it's a full-width strip under the grid rather than a seventh card."""

from __future__ import annotations

import html
from datetime import datetime

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QMessageBox, QPushButton, QSizePolicy, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from dataclasses import dataclass

from ..accounts import YOUTUBE, AccountService
from ..models import Account, Platform, Profile
from ..sources import SourceService
from ..publishers import BlueskyPublisher, FacebookPublisher, ThreadsPublisher
from ..publishing import parse_utc
from ..store import Store
from ..youtube import OAuthClient, YouTubeError, watch_url
from . import theme
from .chrome import icon_button
from .grid import balanced_columns, equalize_heights, max_columns_for, place_even
from .widgets import LineLabel, label as _label, scroll_page
from .logos import platform_pixmap
from .post_panel import local_text
from .sources_panel import SourcesPanel
from .setup_dialogs import GoogleSignInDialog, ProviderSignInDialog, TokenSetupDialog, YouTubeSetupWizard
from .theme import set_prop
from .tips import tip
from .workers import CallWorker


def _card() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame(objectName="Card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(18, 16, 18, 16)
    lay.setSpacing(8)
    return frame, lay


# ====================================================================== integration cards
class StatusPill(QLabel):
    """Connected / Not connected / Needs attention / Posted by hand: a tinted pill."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("StatusPill")
        self.set_state("off", "Not connected")

    def set_state(self, state: str, text: str) -> None:
        self.setText(text)
        set_prop(self, "state", state)


def show_sheet(parent: QWidget, title: str, text: str) -> None:
    """A "Learn more" sheet: the long explanation that doesn't fit on a card."""
    box = QMessageBox(QMessageBox.Information, title, title, QMessageBox.Ok, parent)
    box.setInformativeText(text)
    box.setTextFormat(Qt.PlainText)
    box.setWindowModality(Qt.WindowModal)            # a sheet on macOS
    box.exec()


class IntegrationCard(QFrame):
    """One platform, laid out the same way on every card so a row of them lines up:

    header (logo, name with a one-line mode under it, Learn more, status pill) · one status line ·
    stretch · footer (one action, plus at most one quiet text button), pinned to the bottom edge.
    Nothing on a card grows it: long help is behind Learn more, several accounts collapse to
    "2 connected · Manage…", and setup opens as a dialog."""

    def __init__(self, platform: str, mode: str, help_text: str, parent=None):
        super().__init__(parent)
        self.setObjectName("IntegrationCard")
        self.platform = platform
        self.help_text = help_text
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        label = Platform.LABELS.get(platform, platform)
        self.setAccessibleName(f"{label} account")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(10)
        self.logo = QLabel()
        self.logo.setPixmap(platform_pixmap(platform, 36))
        self.logo.setFixedSize(36, 36)
        self.logo.setAccessibleName(f"{label} logo")
        head.addWidget(self.logo, 0, Qt.AlignVCenter)
        names = QVBoxLayout()
        names.setSpacing(1)
        title_row = QHBoxLayout()
        title_row.setSpacing(4)
        self.title = QLabel(label, objectName="CardTitle")
        title_row.addWidget(self.title)
        self.info_btn = icon_button("help", f"Learn more about {label} in Reelay", 14)
        self.info_btn.setAccessibleName(f"Learn more about {label}")
        self.info_btn.clicked.connect(self.show_help)
        title_row.addWidget(self.info_btn, 0, Qt.AlignVCenter)
        title_row.addStretch(1)
        names.addLayout(title_row)
        self.capability = LineLabel(mode, "CardMode")    # the full width beside the pill
        names.addWidget(self.capability)
        head.addLayout(names, 1)
        self.pill = StatusPill()
        head.addWidget(self.pill, 0, Qt.AlignVCenter)
        lay.addLayout(head)
        line = QHBoxLayout()
        line.setSpacing(6)
        self.line = LineLabel("")
        line.addWidget(self.line, 1)
        self.manage_link = QLabel("<a href='manage'>Manage…</a>", objectName="CardLink")
        self.manage_link.setTextFormat(Qt.RichText)
        self.manage_link.setVisible(False)
        self.manage_link.linkActivated.connect(lambda _href: self.open_manage())
        tip(self.manage_link, f"See, check or disconnect the {label} accounts on this profile, or add another")
        line.addWidget(self.manage_link, 0)
        lay.addLayout(line)
        lay.addStretch(1)
        self.footer = QHBoxLayout()
        self.footer.setSpacing(12)
        self.footer.addStretch(1)
        lay.addLayout(self.footer)

    # ---------------------------------------------------------------- building
    def add_action(self, button: QPushButton) -> QPushButton:
        """A footer action (only one is visible at a time)."""
        self.footer.insertWidget(self.footer.count() - 1, button)
        return button

    def add_quiet(self, text: str, tooltip: str, slot) -> QPushButton:
        button = QPushButton(text, objectName="QuietButton")
        button.setCursor(Qt.PointingHandCursor)
        tip(button, tooltip)
        button.clicked.connect(slot)
        self.footer.insertWidget(self.footer.count() - 1, button)
        return button

    # ---------------------------------------------------------------- state
    def set_line(self, text: str, manage: bool = False) -> None:
        self.line.set_full(text)
        self.manage_link.setVisible(manage)

    def flash(self, text: str) -> None:
        """A short message on the status line (until the next refresh)."""
        self.line.set_full(text)

    def show_help(self) -> None:
        show_sheet(self.window(), f"{Platform.LABELS.get(self.platform, self.platform)} in Reelay", self.help_text)

    def open_manage(self) -> None:
        """Overridden by cards that list accounts."""

    def set_status(self, accounts: list, problems: int = 0, hand: bool = False) -> None:
        if accounts and problems:
            self.pill.set_state("warning", "Needs attention")
        elif accounts:
            self.pill.set_state("connected", "Connected" if len(accounts) == 1 else f"{len(accounts)} connected")
        elif hand:
            self.pill.set_state("hand", "Posted by hand")
        else:
            self.pill.set_state("off", "Not connected")


class HandOffStrip(QFrame):
    """A platform Reelay never signs in to (Snapchat): one full-width line under the grid instead of a card,
    so the grid itself stays even."""

    def __init__(self, platform: str, line: str, help_text: str, parent=None):
        super().__init__(parent)
        self.setObjectName("HandOffStrip")
        self.platform, self.help_text = platform, help_text
        self.connected = 0
        label = Platform.LABELS.get(platform, platform)
        self.setAccessibleName(f"{label}: posted by hand")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 10, 12, 10)
        lay.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(platform_pixmap(platform, 24))
        logo.setFixedSize(24, 24)
        lay.addWidget(logo)
        lay.addWidget(QLabel(label, objectName="CardTitle"))
        self.pill = StatusPill()
        self.pill.set_state("hand", "Posted by hand")
        lay.addWidget(self.pill)
        self.line = LineLabel(line)
        lay.addWidget(self.line, 1)
        info = icon_button("help", f"Learn more about {label} in Reelay")
        info.setAccessibleName(f"Learn more about {label}")
        info.clicked.connect(lambda: show_sheet(self.window(), f"{label} in Reelay", self.help_text))
        lay.addWidget(info)

    def refresh(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


class ManageDialog(QDialog):
    """The accounts a card holds, each with its actions (check, disconnect), and a way to add another."""

    def __init__(self, card, title: str, rows, add_button: QPushButton | None = None):
        super().__init__(card.window())
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        self._rows = rows
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 14)
        lay.setSpacing(10)
        lay.addWidget(QLabel(title, objectName="DetailTitle"))
        self.list = QVBoxLayout()
        self.list.setSpacing(6)
        lay.addLayout(self.list)
        self.status = _label("", "muted")
        lay.addWidget(self.status)
        foot = QHBoxLayout()
        if add_button is not None:
            foot.addWidget(add_button)
        foot.addStretch()
        close = QPushButton("Done")
        tip(close, "Close this window")
        close.clicked.connect(self.accept)
        foot.addWidget(close)
        lay.addLayout(foot)
        self.rebuild()

    def rebuild(self) -> None:
        while self.list.count():
            w = self.list.takeAt(0).widget()
            if w:
                w.hide()
                w.deleteLater()
        rows = self._rows(self)
        for row in rows:
            self.list.addWidget(row)
        if not rows:
            self.list.addWidget(_label("No accounts on this profile.", "faint"))


def account_row(acc, problem: str = "", actions: list[QPushButton] = ()) -> QFrame:
    row = QFrame(objectName="AccountRow")
    rl = QHBoxLayout(row)
    rl.setContentsMargins(10, 6, 6, 6)
    rl.setSpacing(8)
    name = QLabel(f"<b>{html.escape(acc.display_name)}</b>  "
                  f"<span style='color:{theme.P.text_muted}'>{html.escape(acc.handle or '')}</span>")
    rl.addWidget(name, 1)
    if problem:
        rl.addWidget(_label(problem, "danger"))
    for b in actions:
        set_prop(b, "flat")
        rl.addWidget(b)
    return row


def account_line(accounts: list, problem: str = "", empty: str = "") -> tuple[str, bool]:
    """(status line, show Manage…): the one account's name, "N connected", a problem, or a hint."""
    if not accounts:
        return empty, False
    if problem:
        return problem, True
    if len(accounts) == 1:
        acc = accounts[0]
        return (f"{acc.display_name}  {acc.handle}" if acc.handle and acc.handle != acc.display_name
                else acc.display_name), True
    return f"{len(accounts)} connected", True


class AccountsPage(QWidget):
    accountsChanged = Signal()
    listSource = Signal(object)        # a download source's List Videos: show it in the Library
    listLinks = Signal(list)

    def __init__(self, store: Store, accounts: AccountService, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.store, self.service = store, accounts
        self.profile: Profile | None = None
        self.schedule_worker: CallWorker | None = None
        self._columns = 0
        self._build_ui()

    def _build_ui(self) -> None:
        scroll, body, self.body = scroll_page((24, 18, 24, 24), 14, body_name="Page", sideways=False)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(QLabel("Accounts", objectName="PageTitle"), 0, Qt.AlignBottom)
        self.count = QLabel(objectName="PageCount")
        head.addWidget(self.count, 0, Qt.AlignBottom)
        head.addStretch()
        self.body.addLayout(head)
        self.subtitle = _label("", "muted")
        self.body.addWidget(self.subtitle)

        # ---- YouTube
        yc = self.youtube_card = IntegrationCard(YOUTUBE, "Uploads and schedules", YOUTUBE_HELP)
        self.auto_label = yc.capability
        self._youtube_accounts: list = []
        self.setup_btn = yc.add_action(QPushButton("Set Up YouTube…"))
        set_prop(self.setup_btn, "primary")
        tip(self.setup_btn, "A step-by-step guide to connecting through your own free Google project")
        self.setup_btn.clicked.connect(self._setup_youtube)
        self.connect_btn = yc.add_action(QPushButton("Sign in with Google"))
        set_prop(self.connect_btn, "primary")
        tip(self.connect_btn, "Connect a YouTube channel to this profile by signing in with its Google account")
        self.connect_btn.clicked.connect(self._connect)
        self.disconnect_btn = yc.add_action(QPushButton("Disconnect"))
        tip(self.disconnect_btn, "Remove this channel from the profile. Videos on YouTube stay.")
        self.disconnect_btn.clicked.connect(lambda: self._youtube_accounts and self._disconnect(self._youtube_accounts[0]))
        self.manage_btn = yc.add_action(QPushButton("Manage…"))
        tip(self.manage_btn, "See or disconnect this profile's channels, or add another")
        self.manage_btn.clicked.connect(self._manage_youtube)
        yc.open_manage = self._manage_youtube
        self.advanced_btn = yc.add_quiet("Advanced Setup…", "Connect through your own Google project and keys "
                                         "instead of Reelay's one-click sign-in", self._open_youtube_advanced)

        # Advanced Setup opens as its own window, so it never makes the cards taller.
        adv = self.youtube_advanced = QDialog(self)
        adv.setWindowTitle("YouTube: Advanced Setup")
        adv.setMinimumWidth(520)
        al = QVBoxLayout(adv)
        al.setContentsMargins(22, 20, 22, 16)
        al.setSpacing(10)
        al.addWidget(QLabel("Advanced Setup: use my own developer keys", objectName="DetailTitle"))
        self.creds_label = _label("", "faint")
        al.addWidget(self.creds_label)
        self.pending_note = _label("", "muted")
        al.addWidget(self.pending_note)
        self.setup_box = QWidget()
        sl = QVBoxLayout(self.setup_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(10)
        sl.addWidget(_label("Connect through a free Google Cloud project that you own. Reelay walks you through "
                            "it, once (about five minutes). After that, connecting a channel is a single "
                            "“Sign in with Google”.", "muted"))
        srow = QHBoxLayout()
        self.wizard_btn = QPushButton("Set Up YouTube…")
        set_prop(self.wizard_btn, "primary")
        tip(self.wizard_btn, "A step-by-step guide to creating your Google project and client ID")
        self.wizard_btn.clicked.connect(lambda: (adv.accept(), self._setup_youtube()))
        self.have_id_btn = QPushButton("I Already Have a Client ID…")
        set_prop(self.have_id_btn, "flat")
        tip(self.have_id_btn, "Skip straight to pasting a client ID and secret (or loading the JSON file)")
        self.have_id_btn.clicked.connect(lambda: (adv.accept(), self._setup_youtube(paste=True)))
        srow.addWidget(self.wizard_btn)
        srow.addWidget(self.have_id_btn)
        srow.addStretch()
        sl.addLayout(srow)
        al.addWidget(self.setup_box)
        own_row = QWidget()
        orl = QHBoxLayout(own_row)
        orl.setContentsMargins(0, 0, 0, 0)
        self.replace_creds = QPushButton("Change Google Setup…")
        set_prop(self.replace_creds, "flat")
        tip(self.replace_creds, "Use a different Google client ID and secret")
        self.replace_creds.clicked.connect(lambda: (adv.accept(), self._setup_youtube(paste=True)))
        self.forget_creds = tip(QPushButton("Stop Using My Own Project"),
                                "Go back to Reelay's one-click Google sign-in. Channels connected through your project "
                                "will need to sign in again.")
        set_prop(self.forget_creds, "flat")
        self.forget_creds.clicked.connect(lambda: (adv.accept(), self._forget_client()))
        orl.addWidget(self.replace_creds)
        orl.addWidget(self.forget_creds)
        orl.addStretch()
        self.own_row = own_row
        al.addWidget(own_row)
        al.addWidget(_label("While your own Google project is in <b>Testing</b>, Google signs you out every 7 days "
                            "(just sign in again), and uploads stay <b>private</b> until the project passes YouTube's "
                            "free <a href='https://support.google.com/youtube/contact/yt_api_form'>API compliance "
                            "audit</a>.", "faint", rich=True))
        done = QPushButton("Done")
        tip(done, "Close this window")
        done.clicked.connect(adv.accept)
        dl = QHBoxLayout()
        dl.addStretch()
        dl.addWidget(done)
        al.addLayout(dl)

        # ---- the platforms you can sign in to (an even grid) and Snapchat (a strip: posted by hand)
        self.platform_cards: dict[str, PlatformCard | SignInOnlyCard | HandOffStrip] = {}
        for spec in PLATFORM_SPECS:
            self.platform_cards[spec.platform] = PlatformCard(spec, self)
        for platform, text in HAND_OFF_TEXT.items():
            if platform == Platform.SNAPCHAT:
                self.platform_cards[platform] = HandOffStrip(
                    platform, "Copy the caption from a ticket and post Spotlight from the Snapchat app.", text)
            else:
                self.platform_cards[platform] = SignInOnlyCard(platform, text, self)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(14)
        self.body.addWidget(self.grid_host)
        self.snapchat_strip = self.platform_cards[Platform.SNAPCHAT]
        self.body.addWidget(self.snapchat_strip)

        # ---- download sources: where videos come in from (full width)
        sc, scl = _card()
        self.sources_card = sc
        scl.addWidget(QLabel("Download Sources", objectName="DetailTitle"))
        self.sources_note = _label("", "muted")
        scl.addWidget(self.sources_note)
        self.sources_panel = SourcesPanel(self.store, SourceService(self.store, self.service), show_manage=False)
        self.sources_panel.fetchRequested.connect(self.listSource.emit)
        self.sources_panel.linksRequested.connect(self.listLinks.emit)
        scl.addSpacing(4)
        scl.addWidget(self.sources_panel)
        self.body.addWidget(sc)

        # ---- channel schedule (full width, once a channel is connected)
        sc, scl = _card()
        self.schedule_card = sc
        shead = QHBoxLayout()
        shead.addWidget(QLabel("Channel Schedule", objectName="DetailTitle"))
        shead.addStretch()
        self.schedule_account = QComboBox()
        self.schedule_account.setMinimumWidth(220)
        shead.addWidget(self.schedule_account)
        tip(self.schedule_account, "Which connected channel to show")
        self.load_schedule_btn = QPushButton("Load from YouTube")
        tip(self.load_schedule_btn, "Read this channel's latest uploads from YouTube. Nothing is changed.")
        self.load_schedule_btn.clicked.connect(self._load_schedule)
        shead.addWidget(self.load_schedule_btn)
        scl.addLayout(shead)
        scl.addWidget(_label("Your channel's latest 50 uploads, including private and scheduled ones that "
                             "only you can see. Double-click to open.", "muted"))
        self.schedule = QTreeWidget()
        self.schedule.setHeaderLabels(["TITLE", "STATUS", "GOES LIVE / PUBLISHED"])
        self.schedule.setRootIsDecorated(False)
        self.schedule.setMinimumHeight(260)
        self.schedule.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.schedule.header().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.schedule.itemDoubleClicked.connect(
            lambda it, _c: QDesktopServices.openUrl(QUrl(watch_url(it.data(0, Qt.UserRole)))))
        scl.addWidget(self.schedule)
        self.body.addWidget(sc)
        self.body.addStretch()
        self._place_cards()

    GRID_ORDER = (YOUTUBE, "tiktok", "instagram", "bluesky", "threads", "facebook")

    def _cards(self) -> list[QWidget]:
        """The grid: the six platforms you can sign in to. (Snapchat, posted by hand, is the strip below.)"""
        return [self.youtube_card if p == YOUTUBE else self.platform_cards[p] for p in self.GRID_ORDER]

    def _place_cards(self) -> None:
        cards = self._cards()
        columns = balanced_columns(len(cards), max_columns_for(self.width() - 48))
        if columns != self._columns:
            self._columns = columns
            place_even(self.grid, cards, columns)
            self.sources_panel.set_columns(balanced_columns(4, 2 if columns > 1 else 1))
        else:
            equalize_heights(cards)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_cards()

    # ---------------------------------------------------------------- state
    def set_profile(self, profile: Profile) -> None:
        self.profile = profile
        self.subtitle.setText(f"Where {profile.name}'s videos go out, and below, where they come in from. Each "
                              "profile has its own accounts, so creators stay separate.")
        self.sources_note.setText(f"Where {profile.name}'s videos come in from. Type a username or paste a profile "
                                  "link; List Videos shows its videos in the Library. Only import media you own or "
                                  "have permission to manage.")
        self.sources_panel.set_profile(profile)
        self.schedule.clear()
        self.refresh()

    def refresh(self) -> None:
        client = self.service.oauth_client()
        source = self.service.google_client_source()
        central = self.service.central_oauth_client() is not None
        # Advanced Setup (a dialog): your own project's setup, or switching back to Reelay's sign-in
        self.setup_box.setVisible(source != "own")
        self.own_row.setVisible(source == "own")
        self.forget_creds.setVisible(source == "own" and central)
        self.creds_label.setText({"own": "Using your Google project",
                                  "reelay": "Using Reelay's Google sign-in"}.get(source, ""))
        self.creds_label.setVisible(bool(source))
        hint = ("Reelay's one-click Google sign-in isn't finished being set up yet. Until it is, connect with "
                "your own Google project." if self.service.central_pending() else
                "Set up your own Google project to connect a channel.")
        self.pending_note.setText(hint)
        self.pending_note.setVisible(client is None)

        accounts = self.store.list_accounts(self.profile.id, YOUTUBE) if self.profile else []
        self._youtube_accounts = accounts
        problems = [self.service.needs_reconnect(acc) for acc in accounts]
        problem = next((p for p in problems if p), "")
        if client is None:
            action = self.setup_btn
        elif problem or not accounts:
            action = self.connect_btn
            self.connect_btn.setText("Sign in Again" if problem else "Sign in with Google")
        else:
            action = self.disconnect_btn if len(accounts) == 1 else self.manage_btn
        for button in (self.setup_btn, self.connect_btn, self.disconnect_btn, self.manage_btn):
            button.setVisible(button is action)
        line, manage = account_line(accounts, problem, hint if client is None else "No channel connected yet")
        self.youtube_card.set_line(line, manage and len(accounts) == 1)
        self.youtube_card.set_status(accounts, sum(bool(p) for p in problems))

        grid = [card for card in self._cards() if card is not self.youtube_card]
        for card in self.platform_cards.values():
            card.refresh()
        connected = int(bool(accounts)) + sum(card.connected for card in grid)
        self.count.setText(f"{connected} of {len(self._cards())} connected")
        equalize_heights(self._cards())

        self.schedule_account.clear()
        for acc in accounts:
            self.schedule_account.addItem(acc.label, acc.id)
        self.load_schedule_btn.setEnabled(bool(accounts))
        self.schedule_card.setVisible(bool(accounts))

    # ---------------------------------------------------------------- actions
    def _setup_youtube(self, paste: bool = False) -> None:
        wizard = YouTubeSetupWizard(self)
        if paste:
            wizard.jump_to_paste()
        if wizard.exec() != YouTubeSetupWizard.Accepted or wizard.client is None:
            return
        self._save_client(wizard.client)

    def _save_client(self, client: OAuthClient) -> None:
        self.service.set_oauth_client(client)
        self.refresh()
        self.youtube_card.flash("Google setup saved. Now click “Sign in with Google” to connect a channel.")

    def _forget_client(self) -> None:
        if QMessageBox.question(self, "Stop Using My Own Project",
                                "Go back to Reelay's one-click Google sign-in?\n\nChannels connected through your own "
                                "Google project will need to sign in again.") != QMessageBox.Yes:
            return
        self.service.set_oauth_client(None)
        self.refresh()
        self.youtube_card.flash("Using Reelay's Google sign-in. Sign in again for any channel that needs it.")

    def _import_client(self) -> None:
        """Load a downloaded client_secret JSON file directly (also offered inside the wizard)."""
        path, _ = QFileDialog.getOpenFileName(self, "Choose the client_secret file", "", "JSON (*.json)")
        if not path:
            return
        try:
            client = OAuthClient.from_json(path)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Couldn't use that file", str(exc))
            return
        self._save_client(client)

    def _open_youtube_advanced(self) -> None:
        self.refresh()
        self.youtube_advanced.exec()

    def _manage_youtube(self) -> None:
        def rows(dialog):
            out = []
            for acc in self.store.list_accounts(self.profile.id, YOUTUBE) if self.profile else []:
                remove = tip(QPushButton("Disconnect"), "Remove this channel from the profile. Videos on YouTube stay.")
                remove.clicked.connect(lambda _=False, a=acc: (self._disconnect(a), dialog.rebuild()))
                out.append(account_row(acc, self.service.needs_reconnect(acc), [remove]))
            return out
        add = None
        if self.service.oauth_client() is not None:
            add = tip(QPushButton("Add Another Channel…"), "Sign in with another Google account's channel")
        dialog = ManageDialog(self.youtube_card, "YouTube Channels", rows, add)
        if add is not None:
            add.clicked.connect(lambda: (self._connect(), dialog.rebuild()))
        dialog.exec()

    def _connect(self) -> None:
        oauth = self.service.oauth_client()
        if oauth is None or not self.profile:
            return
        dialog = GoogleSignInDialog(self.service.sign_in, oauth, self)
        dialog.signedIn.connect(self._on_signed_in)
        dialog.exec()

    def _on_signed_in(self, result) -> None:
        channel, tokens = result
        self.service.finish_connect(self.profile.id, channel, tokens)
        self.refresh()
        self.accountsChanged.emit()

    def _disconnect(self, account) -> None:
        answer = QMessageBox.question(self, "Disconnect Channel",
                                      f"Disconnect {account.display_name} from {self.profile.name}?\n\n"
                                      "Videos already on YouTube aren't affected.")
        if answer == QMessageBox.Yes:
            self.service.disconnect(account)
            self.refresh()
            self.accountsChanged.emit()

    def _load_schedule(self) -> None:
        account_id = self.schedule_account.currentData()
        account = self.store.get_account(account_id) if account_id else None
        if account is None or (self.schedule_worker and self.schedule_worker.isRunning()):
            return
        try:
            client = self.service.client_for(account)
        except YouTubeError as exc:
            QMessageBox.warning(self, "YouTube", str(exc))
            return
        self.load_schedule_btn.setEnabled(False)
        self.load_schedule_btn.setText("Loading…")
        self.schedule_worker = CallWorker(client.recent_uploads, 50, parent=self)
        self.schedule_worker.succeeded.connect(self._show_schedule)
        self.schedule_worker.failed.connect(lambda m: QMessageBox.warning(self, "YouTube", m))
        self.schedule_worker.finished.connect(lambda: (self.load_schedule_btn.setEnabled(True),
                                                       self.load_schedule_btn.setText("Load from YouTube")))
        self.schedule_worker.start()

    def _show_schedule(self, uploads: list[dict]) -> None:
        self.schedule.clear()
        now = datetime.now().astimezone()
        for up in uploads:
            when = parse_utc(up.get("publish_at"))
            if when and up.get("privacy") == "private" and when > now:
                status, time_text = "Scheduled", local_text(up["publish_at"])
            else:
                status = {"public": "Public", "unlisted": "Unlisted", "private": "Private"}.get(up.get("privacy"), "—")
                time_text = local_text((up.get("published_at") or "").replace(".000Z", "Z")) or "—"
            item = QTreeWidgetItem([up.get("title", ""), status, time_text])
            item.setData(0, Qt.UserRole, up["id"])
            self.schedule.addTopLevelItem(item)
        if not uploads:
            self.schedule.addTopLevelItem(QTreeWidgetItem(["No uploads yet.", "", ""]))

    def shutdown(self) -> None:
        for card in self.platform_cards.values():
            card.shutdown()


YOUTUBE_HELP = ("Connect a channel with one click: Reelay uploads, schedules and checks when videos go live, and "
                "reads your channel's latest uploads (including private and scheduled ones) for the Channel "
                "Schedule.\n\nPrefer your own Google project and keys? Use Advanced Setup on the card. While your "
                "own project is in Testing, Google signs you out every 7 days and uploads stay private until the "
                "project passes YouTube's free API compliance audit.")


# ====================================================================== other platforms
@dataclass(frozen=True)
class PlatformSpec:
    platform: str
    fields: tuple[tuple[str, str, bool], ...]   # (secret key, label, is_password)
    help: str
    publisher: type


PLATFORM_SPECS = (
    PlatformSpec(Platform.BLUESKY, (("handle", "Handle, e.g. @yourname.bsky.social", False),
                                    ("app_password", "App Password", True)),
                 "Connect with a Bluesky App Password: a separate password just for Reelay that you can revoke "
                 "any time.", BlueskyPublisher),
    PlatformSpec(Platform.THREADS, (("access_token", "Threads access token", True),),
                 "Threads only lets apps post through a Meta developer app you own, so connecting means pasting "
                 "a token from that app. The guide walks you through it.", ThreadsPublisher),
    PlatformSpec(Platform.FACEBOOK, (("page_id", "Page ID", False), ("page_access_token", "Page access token", True)),
                 "Facebook only lets apps post to Pages through a Meta developer app you own, so connecting means "
                 "pasting a Page token from that app. The guide walks you through it.", FacebookPublisher),
)


class PlatformCard(IntegrationCard):
    """Bluesky, Threads, Facebook: connect (one click with Reelay's app where there is one), check, disconnect."""

    NOUN = {Platform.FACEBOOK: "Page"}

    def __init__(self, spec: PlatformSpec, page: "AccountsPage"):
        super().__init__(spec.platform, "", spec.help)
        self.spec, self.page = spec, page
        self.worker: CallWorker | None = None
        self.connected = 0
        self._accounts: list = []
        self._report = self.flash          # where Check results go (the Manage window while it's open)
        label = Platform.LABELS[spec.platform]
        self.flag_label = self.capability
        self.signin = SignInButton(spec.platform, self)
        self.signin.connected.connect(self._signed_in)
        self.add_action(self.signin)
        self.connect_btn = QPushButton(f"Connect {label}…")
        set_prop(self.connect_btn, "primary")
        tip(self.connect_btn, f"Step-by-step setup. Reelay checks the details with {label} before saving anything.")
        self.connect_btn.clicked.connect(self._open_setup)
        self.disconnect_btn = self.add_action(QPushButton("Disconnect"))
        tip(self.disconnect_btn, "Remove this account from the profile")
        self.disconnect_btn.clicked.connect(lambda: self._accounts and self._disconnect(self._accounts[0]))
        self.manage_btn = self.add_action(QPushButton("Manage…"))
        tip(self.manage_btn, f"Check or disconnect this profile's {label} accounts, or add another")
        self.manage_btn.clicked.connect(self.open_manage)
        if self.signin.available:
            # One click with Reelay's app; your own app or token stays available under Advanced Setup.
            self.help_text = (f"Sign in with your {label} account: one click, in your browser. Reelay never sees "
                              f"your password.\n\nAdvanced Setup: {spec.help}")
            self.connect_btn.setText("Use Your Own App Instead…")
            self.advanced = QDialog(self)
            self.advanced.setWindowTitle(f"{label}: Advanced Setup")
            self.advanced.setMinimumWidth(460)
            al = QVBoxLayout(self.advanced)
            al.setContentsMargins(22, 20, 22, 16)
            al.setSpacing(10)
            al.addWidget(QLabel("Advanced Setup: use my own developer keys", objectName="DetailTitle"))
            al.addWidget(_label(spec.help, "muted"))
            row = QHBoxLayout()
            row.addWidget(self.connect_btn)
            row.addStretch()
            done = tip(QPushButton("Done"), "Close this window")
            done.clicked.connect(self.advanced.accept)
            row.addWidget(done)
            al.addLayout(row)
            self.connect_btn.clicked.connect(self.advanced.accept)
            self.advanced_btn = self.add_quiet("Advanced Setup…", f"Connect through your own {label} developer "
                                               "app and keys instead", self.advanced.exec)
        else:
            self.add_action(self.connect_btn)
            if self.signin.note.text():
                self.help_text = f"{spec.help}\n\n{self.signin.note.text()}"

    def _signed_in(self, account) -> None:
        self.refresh()
        self.flash(f"Connected {account.display_name}.")

    def _hint(self) -> str:
        if self.spec.platform == Platform.BLUESKY:
            return "Posts with an App Password"
        noun = self.NOUN.get(self.spec.platform, "account")
        return (f"Sign in to attach your {noun}" if self.signin.available
                else f"Connect your {noun} through your own Meta app")

    def refresh(self) -> None:
        from ..flags import env_var, publishing_enabled
        if self.signin.available:   # Reelay's own app: one click; your own app/token stays as a fallback
            self.connect_btn.setText("Use Your Own App Instead…")
            set_prop(self.connect_btn, "primary", False)
        on = publishing_enabled(self.spec.platform, self.page.store)
        self.flag_label.set_full("Automatic posting on" if on else "Automatic posting off")
        tip(self.flag_label, "Automatic posting is on: Reelay posts scheduled videos here for you." if on else
            "Reelay won't post here on its own. Turn on automatic posting in Settings "
            f"(or set {env_var(self.spec.platform)}=1).")
        profile = self.page.profile
        accounts = self.page.store.list_accounts(profile.id, self.spec.platform) if profile else []
        self._accounts = accounts
        problems = sum(not self.page.service.has_credentials(acc) for acc in accounts)
        problem = "Sign-in details missing: connect again" if problems else ""
        if problem or not accounts:
            action = self.signin if self.signin.available else self.connect_btn
        else:
            action = self.disconnect_btn if len(accounts) == 1 else self.manage_btn
        for button in (self.signin, self.connect_btn, self.disconnect_btn, self.manage_btn):
            if button is self.connect_btn and self.signin.available:
                continue                     # it lives in the Advanced Setup window
            button.setVisible(button is action and (button is not self.signin or self.signin.available))
        line, manage = account_line(accounts, problem, self._hint())
        self.set_line(line, manage and len(accounts) == 1)
        self.connected = int(bool(accounts))
        self.set_status(accounts, problems)

    def open_manage(self) -> None:
        label = Platform.LABELS[self.spec.platform]

        def rows(dialog):
            out = []
            for acc in self.page.store.list_accounts(self.page.profile.id, self.spec.platform):
                buttons, problem = [], ""
                if not self.page.service.has_credentials(acc):
                    problem = "Sign-in details missing: connect again"
                else:
                    verify = tip(QPushButton("Check"), "Make sure the saved details still work. Nothing is posted.")
                    verify.clicked.connect(lambda _=False, a=acc: self._verify(a))
                    buttons.append(verify)
                remove = tip(QPushButton("Disconnect"), "Remove this account from the profile")
                remove.clicked.connect(lambda _=False, a=acc: (self._disconnect(a), dialog.rebuild()))
                buttons.append(remove)
                row = account_row(acc, problem, buttons)
                if not problem:
                    checked = local_text(acc.last_verified_at) if acc.last_verified_at else "never"
                    row.setToolTip(f"Last checked: {checked}")
                out.append(row)
            return out
        if self.signin.available:
            add = SignInButton(self.spec.platform, self)
            add.setText(f"Add Another {label} Account…")
            add.connected.connect(self._signed_in)
        else:
            add = tip(QPushButton(f"Connect Another {label} Account…"), "Step-by-step setup for another account")
            add.clicked.connect(self._open_setup)
        dialog = ManageDialog(self, f"{label} Accounts", rows, add)
        add_refresh = getattr(add, "connected", None)
        if add_refresh is not None:
            add_refresh.connect(lambda _acc: dialog.rebuild())
        self._report = dialog.status.setText
        try:
            dialog.exec()
        finally:
            self._report = self.flash

    def _busy(self) -> bool:
        return bool(self.worker and self.worker.isRunning())

    def _open_setup(self) -> None:
        if self._busy() or not self.page.profile:
            return
        dialog = TokenSetupDialog(self.spec.platform, self.spec.fields, self.spec.publisher().verify, self)
        dialog.connected.connect(self._connected)
        dialog.exec()

    def _connected(self, identity: dict, secret: dict) -> None:
        if self.spec.platform == Platform.THREADS:
            secret = {**secret, "user_id": identity["external_id"]}
        if self.spec.platform == Platform.FACEBOOK and identity.get("external_id"):
            secret = {**secret, "page_id": identity["external_id"]}    # a Page link or username → its numeric ID
        account = self.page.service.connect_platform(self.page.profile.id, self.spec.platform, identity, secret)
        self.refresh()
        self.flash(f"Connected {account.display_name}.")

    def _verify(self, account: Account) -> None:
        secret = self.page.service.credentials(account)
        if self._busy() or not secret:
            return
        report = self._report
        report(f"Checking {account.display_name}…")
        self.worker = CallWorker(self.spec.publisher().verify, secret, parent=self)
        self.worker.succeeded.connect(lambda _identity: (self.page.store.mark_verified(account.id),
                                                         report(f"{account.display_name} is connected and working."),
                                                         self.refresh()))
        self.worker.failed.connect(report)
        self.worker.start()

    def _disconnect(self, account: Account) -> None:
        answer = QMessageBox.question(self, "Disconnect", f"Disconnect {account.display_name} from "
                                                          f"{self.page.profile.name}? Its credential is removed from "
                                                          "the keychain.")
        if answer == QMessageBox.Yes:
            self.page.service.disconnect(account)
            self.refresh()

    def shutdown(self) -> None:
        if self.worker:
            self.worker.wait(3000)


# ====================================================================== "Sign in with …" (Reelay's own apps)
class SignInButton(QPushButton):
    """"Sign in with <platform>" through Reelay's own app, shown only when this
    build has that app; otherwise hidden, with `note` saying why."""

    connected = Signal(object)   # Account

    def __init__(self, platform: str, card):
        from ..oauth import availability, provider_for
        self.platform, self.card = platform, card
        self.provider = provider_for(platform)
        label = self.provider.label if self.provider else Platform.LABELS.get(platform, platform)
        super().__init__(f"Sign in with {label}")
        set_prop(self, "primary")
        tip(self, f"Connect your {label} account in your browser. Reelay never sees your password.")
        self.available, why = availability(self.provider) if self.provider else (False, "")
        # Only its text is used (in the card's help). It's never placed in a layout, so it must never be
        # shown: showing a parentless label opens it as its own window at launch.
        self.note = _label("" if self.available else why, "faint")
        self.setVisible(self.available)
        self.clicked.connect(self._sign_in)

    def _sign_in(self) -> None:
        page = self.card.page
        if not page.profile or self.provider is None:
            return
        dialog = ProviderSignInDialog(self.provider.key, self.window())
        dialog.signedIn.connect(self._finish)
        dialog.exec()

    def _finish(self, tokens, found: list) -> None:
        from PySide6.QtWidgets import QInputDialog
        page = self.card.page
        choice = found[0]
        if len(found) > 1:   # e.g. several Facebook Pages
            names = [f["display_name"] for f in found]
            name, ok = QInputDialog.getItem(self.window(), "Choose an Account", "Which one should this profile use?",
                                            names, 0, False)
            if not ok:
                return
            choice = found[names.index(name)]
        try:
            account = page.service.connect_signed_in(page.profile.id, self.platform, tokens, choice)
        except ValueError as exc:
            QMessageBox.warning(self.window(), "Sign in", str(exc))
            return
        self.connected.emit(account)
        page.accountsChanged.emit()


HAND_OFF_TEXT = {
    "tiktok": ("Reelay can't post to TikTok for you. TikTok only allows automatic posting from developer apps it has "
               "reviewed and approved; without that approval, an app can only send private (“Only me”) posts or "
               "drafts. Copy the TikTok caption from a ticket and post in the TikTok app. Signing in attaches your "
               "account for analytics. Downloading your TikTok videos works in the Library."),
    "instagram": ("Reelay can't post to Instagram for you yet. Instagram needs a Professional (Creator or Business) "
                  "account and a Meta app that passes App Review. Copy the Instagram caption from a ticket and post in "
                  "the Instagram app. Signing in attaches your account for analytics."),
    "snapchat": ("Reelay doesn't post to Snapchat for you: Spotlight is posted by hand. Spotlight is made for vertical "
                 "(9:16) videos. Copy the caption from the video's ticket and post it from the Snapchat app. You can "
                 "also download your own Spotlight videos by pasting their links in the Library."),
}


class _InstagramSetupMixin:
    def _open_setup(self) -> None:
        from ..instagram_media import verify
        if not self.page.profile:
            return
        dialog = TokenSetupDialog(Platform.INSTAGRAM, (("access_token", "Instagram access token", True),),
                                  verify, self)
        dialog.connected.connect(self._token_connected)
        dialog.exec()

    def _token_connected(self, identity: dict, secret: dict) -> None:
        account = self.page.service.connect_platform(self.page.profile.id, Platform.INSTAGRAM, identity, secret)
        self.refresh()
        self.flash(f"Connected {account.display_name}. List your Instagram source in the Library to see every Reel.")


class SignInOnlyCard(_InstagramSetupMixin, IntegrationCard):
    """TikTok / Instagram: posted by hand. Signing in (where Reelay has an app) attaches the account
    for analytics."""

    def __init__(self, platform: str, text: str, page: "AccountsPage"):
        super().__init__(platform, "Copy and paste", text)
        self.page = page
        self.connected = 0
        self._accounts: list = []
        self.signin = SignInButton(platform, self)
        self.signin.connected.connect(lambda account: (self.refresh(), self.flash(f"Connected {account.display_name}.")))
        self.add_action(self.signin)
        self.disconnect_btn = self.add_action(QPushButton("Disconnect"))
        tip(self.disconnect_btn, "Remove this account from the profile")
        self.disconnect_btn.clicked.connect(lambda: self._accounts and self._disconnect(self._accounts[0]))
        self.manage_btn = self.add_action(QPushButton("Manage…"))
        tip(self.manage_btn, "See or disconnect this profile's accounts")
        self.manage_btn.clicked.connect(self.open_manage)
        # Instagram: a token from the creator's own Meta app lists their whole profile (instagram_media.py).
        self.setup_btn = None
        if platform == Platform.INSTAGRAM:
            self.setup_btn = self.add_action(QPushButton("Advanced Setup…"))
            set_prop(self.setup_btn, "flat")
            tip(self.setup_btn, "Connect with a token from your own Meta app, so Reelay can list every Reel on "
                                "your profile")
            self.setup_btn.clicked.connect(self._open_setup)
        if self.signin.note.text():
            self.help_text = f"{text}\n\n{self.signin.note.text()}"

    def refresh(self) -> None:
        profile = self.page.profile
        accounts = self.page.store.list_accounts(profile.id, self.platform) if profile else []
        self._accounts = accounts
        action = (self.signin if not accounts else self.disconnect_btn if len(accounts) == 1 else self.manage_btn)
        for button in (self.signin, self.disconnect_btn, self.manage_btn):
            button.setVisible(button is action and (button is not self.signin or self.signin.available))
        if self.setup_btn is not None:
            self.setup_btn.setVisible(not accounts)
        hint = "Sign in to attach your account" if self.signin.available else self.signin.note.text()
        if self.setup_btn is not None and not self.signin.available:
            hint = "Connect with Advanced Setup to list your whole profile"
        line, manage = account_line(accounts, "", hint)
        self.set_line(line, manage and len(accounts) == 1)
        self.connected = int(bool(accounts))
        self.set_status(accounts)

    def open_manage(self) -> None:
        label = Platform.LABELS.get(self.platform, self.platform)

        def rows(dialog):
            out = []
            for acc in self.page.store.list_accounts(self.page.profile.id, self.platform):
                remove = tip(QPushButton("Disconnect"), "Remove this account from the profile")
                remove.clicked.connect(lambda _=False, a=acc: (self._disconnect(a), dialog.rebuild()))
                out.append(account_row(acc, "", [remove]))
            return out
        ManageDialog(self, f"{label} Accounts", rows).exec()

    def _disconnect(self, account) -> None:
        if QMessageBox.question(self, "Disconnect", f"Disconnect {account.display_name}?") == QMessageBox.Yes:
            self.page.service.disconnect(account)
            self.refresh()

    def shutdown(self) -> None:
        pass
