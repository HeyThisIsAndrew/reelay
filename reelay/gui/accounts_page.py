"""Accounts page: where videos go OUT, as a grid of integration cards.

Each card shows the platform, whether it's connected, and what Reelay can do there. Connecting is one click
with Reelay's own apps ("Sign in with Google / TikTok / Instagram / Threads / Facebook"); your own developer
app and keys stay available under "Advanced Setup", which opens by itself when it's the only way in (for
example while Reelay's Google sign-in is still being set up). Bluesky connects with an App Password;
TikTok, Instagram and Snapchat are posted by hand."""

from __future__ import annotations

import html
from datetime import datetime

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QSizePolicy, QToolButton,
                               QMessageBox,
                               QPushButton, QScrollArea, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from dataclasses import dataclass

from ..accounts import YOUTUBE, AccountService
from ..models import Account, Platform, Profile
from ..publishers import BlueskyPublisher, FacebookPublisher, ThreadsPublisher
from ..publishing import parse_utc
from ..store import Store
from ..youtube import OAuthClient, YouTubeError, watch_url
from . import theme
from .logos import platform_pixmap
from .post_panel import local_text
from .setup_dialogs import GoogleSignInDialog, ProviderSignInDialog, TokenSetupDialog, YouTubeSetupWizard
from .theme import set_prop
from .tips import tip
from .workers import CallWorker

def _label(text: str, prop: str | None = None, rich: bool = False) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    if prop:
        lab.setProperty(prop, True)
    if rich:
        lab.setTextFormat(Qt.RichText)
        lab.setOpenExternalLinks(True)
    return lab


def _card() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame(objectName="Card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(18, 16, 18, 16)
    lay.setSpacing(8)
    return frame, lay


# ====================================================================== integration cards
class StatusPill(QLabel):
    """Connected / Not connected / Signed out / Posted by hand."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("StatusPill")
        self.set_state("off", "Not connected")

    def set_state(self, state: str, text: str) -> None:
        self.setText(text)
        set_prop(self, "state", state)


class AdvancedSection(QWidget):
    """"Advanced Setup: use my own developer keys", folded away unless it's the only way in."""

    def __init__(self, title: str = "Advanced Setup: use my own developer keys", parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(8)
        self.toggle = QToolButton(objectName="AdvancedToggle")
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.RightArrow)
        self.toggle.setCursor(Qt.PointingHandCursor)
        tip(self.toggle, "For developers and agencies: connect through your own developer app and keys instead "
                         "of Reelay's one-click sign-in")
        self.toggle.toggled.connect(self.set_expanded)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(8)
        self.body.setVisible(False)
        lay.addWidget(self.toggle)
        lay.addWidget(self.body)

    def add(self, widget: QWidget) -> QWidget:
        self.body_layout.addWidget(widget)
        return widget

    def set_expanded(self, on: bool) -> None:
        if self.toggle.isChecked() != on:
            self.toggle.setChecked(on)
            return
        self.toggle.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.body.setVisible(on)


class IntegrationCard(QFrame):
    """One platform: logo, name, status, what Reelay can do there; then accounts, actions and Advanced Setup."""

    def __init__(self, platform: str, capability: str, description: str, parent=None):
        super().__init__(parent)
        self.setObjectName("IntegrationCard")
        self.platform = platform
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(12)
        self.logo = QLabel()
        self.logo.setPixmap(platform_pixmap(platform, 34))
        self.logo.setFixedSize(36, 36)
        head.addWidget(self.logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(QLabel(Platform.LABELS.get(platform, platform), objectName="DetailTitle"))
        self.capability = _label(capability, "faint")
        names.addWidget(self.capability)
        head.addLayout(names, 1)
        self.pill = StatusPill()
        head.addWidget(self.pill, 0, Qt.AlignTop)
        lay.addLayout(head)
        self.description = _label(description, "muted")
        lay.addWidget(self.description)
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        lay.addLayout(self.body)
        self.advanced = AdvancedSection()
        self.advanced.setVisible(False)
        lay.addWidget(self.advanced)

    def set_status(self, accounts: list, problems: int = 0, hand: bool = False) -> None:
        if accounts and problems:
            self.pill.set_state("warning", "Needs attention")
        elif accounts:
            self.pill.set_state("connected", "Connected" if len(accounts) == 1 else f"{len(accounts)} connected")
        elif hand:
            self.pill.set_state("hand", "Posted by hand")
        else:
            self.pill.set_state("off", "Not connected")


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


class AccountsPage(QWidget):
    accountsChanged = Signal()

    def __init__(self, store: Store, accounts: AccountService, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.store, self.service = store, accounts
        self.profile: Profile | None = None
        self.schedule_worker: CallWorker | None = None
        self._columns = 0
        self._build_ui()

    def _build_ui(self) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget(objectName="Page")
        self.body = QVBoxLayout(body)
        self.body.setContentsMargins(24, 18, 24, 24)
        self.body.setSpacing(14)
        scroll.setWidget(body)
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
        self.youtube_card = IntegrationCard(
            YOUTUBE, "Posts and schedules automatically",
            "Connect a channel with one click: Reelay uploads, schedules and checks when videos go live.")
        yl = self.youtube_card.body
        self.auto_label = self.youtube_card.capability
        self.creds_label = _label("", "faint")
        yl.addWidget(self.creds_label)

        self.connected_box = QWidget()
        cl = QVBoxLayout(self.connected_box)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(6)
        self.accounts_list = QVBoxLayout()
        self.accounts_list.setSpacing(6)
        cl.addLayout(self.accounts_list)
        crow = QHBoxLayout()
        self.connect_btn = QPushButton("Sign in with Google")
        set_prop(self.connect_btn, "primary")
        tip(self.connect_btn, "Connect a YouTube channel to this profile by signing in with its Google account")
        self.connect_btn.clicked.connect(self._connect)
        crow.addWidget(self.connect_btn)
        crow.addStretch()
        cl.addLayout(crow)
        self.connect_status = _label("", "muted")
        cl.addWidget(self.connect_status)
        yl.addWidget(self.connected_box)
        self.pending_note = _label("", "faint")
        yl.addWidget(self.pending_note)

        # Advanced: your own Google project (the guided wizard, a client ID, or switching back)
        adv = self.youtube_card.advanced
        adv.setVisible(True)
        self.setup_box = QWidget()
        sl = QVBoxLayout(self.setup_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(10)
        sl.addWidget(_label("Connect through a free Google Cloud project that you own. Reelay walks you through "
                            "it, once (about five minutes). After that, connecting a channel is a single "
                            "“Sign in with Google”.", "muted"))
        srow = QHBoxLayout()
        self.setup_btn = QPushButton("Set Up YouTube…")
        set_prop(self.setup_btn, "primary")
        tip(self.setup_btn, "A step-by-step guide to creating your Google project and client ID")
        self.setup_btn.clicked.connect(self._setup_youtube)
        self.have_id_btn = QPushButton("I Already Have a Client ID…")
        set_prop(self.have_id_btn, "flat")
        tip(self.have_id_btn, "Skip straight to pasting a client ID and secret (or loading the JSON file)")
        self.have_id_btn.clicked.connect(lambda: self._setup_youtube(paste=True))
        srow.addWidget(self.setup_btn)
        srow.addWidget(self.have_id_btn)
        srow.addStretch()
        sl.addLayout(srow)
        adv.add(self.setup_box)
        own_row = QWidget()
        orl = QHBoxLayout(own_row)
        orl.setContentsMargins(0, 0, 0, 0)
        self.replace_creds = QPushButton("Change Google Setup…")
        set_prop(self.replace_creds, "flat")
        tip(self.replace_creds, "Use a different Google client ID and secret")
        self.replace_creds.clicked.connect(lambda: self._setup_youtube(paste=True))
        self.forget_creds = tip(QPushButton("Stop Using My Own Project"),
                                "Go back to Reelay's one-click Google sign-in. Channels connected through your project "
                                "will need to sign in again.")
        set_prop(self.forget_creds, "flat")
        self.forget_creds.clicked.connect(self._forget_client)
        orl.addWidget(self.replace_creds)
        orl.addWidget(self.forget_creds)
        orl.addStretch()
        self.own_row = adv.add(own_row)
        adv.add(_label("While your own Google project is in <b>Testing</b>, Google signs you out every 7 days "
                       "(just sign in again), and uploads stay <b>private</b> until the project passes YouTube's "
                       "free <a href='https://support.google.com/youtube/contact/yt_api_form'>API compliance "
                       "audit</a>.", "faint", rich=True))

        # ---- Bluesky / Threads / Facebook, TikTok / Instagram / Snapchat
        self.platform_cards: dict[str, PlatformCard | SignInOnlyCard] = {}
        for spec in PLATFORM_SPECS:
            self.platform_cards[spec.platform] = PlatformCard(spec, self)
        for platform, text in HAND_OFF_TEXT.items():
            self.platform_cards[platform] = SignInOnlyCard(platform, text, self)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(14)
        self.body.addWidget(self.grid_host)

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

    def _cards(self) -> list[QWidget]:
        order = ["tiktok", "instagram", "bluesky", "threads", "facebook", "snapchat"]
        return [self.youtube_card] + [self.platform_cards[p] for p in order if p in self.platform_cards]

    def _place_cards(self) -> None:
        width = self.width() - 48
        columns = 3 if width >= 1180 else 2 if width >= 720 else 1
        if columns == self._columns:
            return
        self._columns = columns
        for card in self._cards():
            self.grid.removeWidget(card)
        for n, card in enumerate(self._cards()):
            self.grid.addWidget(card, n // columns, n % columns, Qt.AlignTop)
        for c in range(3):
            self.grid.setColumnStretch(c, 1 if c < columns else 0)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_cards()

    # ---------------------------------------------------------------- state
    def set_profile(self, profile: Profile) -> None:
        self.profile = profile
        self.subtitle.setText(f"Where {profile.name}'s videos go out. Each profile has its own accounts, so "
                              "creators stay separate. (Videos come in through the Library.)")
        self.schedule.clear()
        self.refresh()

    def refresh(self) -> None:
        client = self.service.oauth_client()
        source = self.service.google_client_source()
        central = self.service.central_oauth_client() is not None
        self.setup_box.setVisible(client is None)
        self.connected_box.setVisible(client is not None)
        self.own_row.setVisible(source == "own")
        self.forget_creds.setVisible(source == "own" and central)
        self.creds_label.setText({"own": "Using your Google project",
                                  "reelay": "Using Reelay's Google sign-in"}.get(source, ""))
        self.creds_label.setVisible(bool(source))
        if client is None:
            self.pending_note.setText("Reelay's one-click Google sign-in isn't finished being set up yet. Until it "
                                      "is, connect with your own Google project below."
                                      if self.service.central_pending() else
                                      "Set up your own Google project below to connect a channel.")
        self.pending_note.setVisible(client is None)
        self.youtube_card.advanced.set_expanded(client is None or source == "own")   # the only way in: open

        while self.accounts_list.count():
            w = self.accounts_list.takeAt(0).widget()
            if w:
                w.deleteLater()
        accounts = self.store.list_accounts(self.profile.id, YOUTUBE) if self.profile else []
        problems = 0
        if not accounts and client is not None:
            self.accounts_list.addWidget(_label("No channel connected to this profile yet.", "faint"))
        for acc in accounts:
            problem = self.service.needs_reconnect(acc)
            problems += bool(problem)
            remove = tip(QPushButton("Disconnect"), "Remove this channel from the profile. Videos on YouTube stay.")
            remove.clicked.connect(lambda _=False, a=acc: self._disconnect(a))
            self.accounts_list.addWidget(account_row(acc, problem, [remove]))
        self.youtube_card.set_status(accounts, problems)

        connected = len(accounts) > 0
        for card in getattr(self, "platform_cards", {}).values():
            card.refresh()
            connected += card.connected
        total = 1 + len(self.platform_cards)
        self.count.setText(f"{connected} of {total} connected")

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
        self.connect_status.setText("Google setup saved. Now click “Sign in with Google” to connect a channel.")
        self.refresh()

    def _forget_client(self) -> None:
        if QMessageBox.question(self, "Stop Using My Own Project",
                                "Go back to Reelay's one-click Google sign-in?\n\nChannels connected through your own "
                                "Google project will need to sign in again.") != QMessageBox.Yes:
            return
        self.service.set_oauth_client(None)
        self.connect_status.setText("Using Reelay's Google sign-in. Sign in again for any channel marked below.")
        self.refresh()

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

    def _connect(self) -> None:
        oauth = self.service.oauth_client()
        if oauth is None or not self.profile:
            return
        dialog = GoogleSignInDialog(self.service.sign_in, oauth, self)
        dialog.signedIn.connect(self._on_signed_in)
        dialog.exec()

    def _on_signed_in(self, result) -> None:
        channel, tokens = result
        account = self.service.finish_connect(self.profile.id, channel, tokens)
        self.connect_status.setText(f"Connected {account.display_name}.")
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


# ====================================================================== other platforms
@dataclass(frozen=True)
class PlatformSpec:
    platform: str
    fields: tuple[tuple[str, str, bool], ...]   # (secret key, label, is_password)
    help: str
    publisher: type


PLATFORM_SPECS = (
    PlatformSpec(Platform.BLUESKY, (("handle", "Handle (e.g. lydia.bsky.social)", False),
                                    ("app_password", "App Password", True)),
                 "Connect with a Bluesky App Password: a separate password just for Reelay that you can revoke "
                 "any time. Bluesky doesn't offer a sign-in page for apps.", BlueskyPublisher),
    PlatformSpec(Platform.THREADS, (("access_token", "Threads access token", True),),
                 "Threads only lets apps post through a Meta developer app you own, so connecting means pasting "
                 "a token from that app. The guide walks you through it.", ThreadsPublisher),
    PlatformSpec(Platform.FACEBOOK, (("page_id", "Page ID", False), ("page_access_token", "Page access token", True)),
                 "Facebook only lets apps post to Pages through a Meta developer app you own, so connecting means "
                 "pasting a Page token from that app. The guide walks you through it.", FacebookPublisher),
)


class PlatformCard(IntegrationCard):
    """Bluesky, Threads, Facebook: connect (one click with Reelay's app where there is one), verify, disconnect."""

    def __init__(self, spec: PlatformSpec, page: "AccountsPage"):
        super().__init__(spec.platform, "", spec.help)
        self.spec, self.page = spec, page
        self.worker: CallWorker | None = None
        self.connected = 0
        self.flag_label = self.capability
        self.list = QVBoxLayout()
        self.list.setSpacing(6)
        self.body.addLayout(self.list)
        row = QHBoxLayout()
        self.signin = SignInButton(spec.platform, self)
        self.signin.connected.connect(self._signed_in)
        row.addWidget(self.signin)
        self.connect_btn = QPushButton(f"Connect {Platform.LABELS[spec.platform]}…")
        set_prop(self.connect_btn, "primary")
        tip(self.connect_btn, "Step-by-step setup. Reelay checks the details with "
                              f"{Platform.LABELS[spec.platform]} before saving anything.")
        self.connect_btn.clicked.connect(self._open_setup)
        row.addWidget(self.connect_btn)
        row.addStretch()
        self.body.addLayout(row)
        self.body.addWidget(self.signin.note)
        self.status = _label("", "muted")
        self.body.addWidget(self.status)
        if self.signin.available:
            # One click with Reelay's app; your own app or token stays available under Advanced Setup.
            label = Platform.LABELS[spec.platform]
            self.description.setText(f"Sign in with your {label} account: one click, in your browser. Reelay never "
                                     "sees your password.")
            row.removeWidget(self.connect_btn)
            self.advanced.setVisible(True)
            self.advanced.add(_label(spec.help, "faint"))
            self.advanced.add(self.connect_btn)

    def _signed_in(self, account) -> None:
        self.status.setText(f"Connected {account.display_name}.")
        self.refresh()

    def refresh(self) -> None:
        from ..flags import env_var, publishing_enabled
        if self.signin.available:   # Reelay's own app: one click; your own app/token stays as a fallback
            self.connect_btn.setText("Use Your Own App Instead…")
            set_prop(self.connect_btn, "primary", False)
        on = publishing_enabled(self.spec.platform, self.page.store)
        self.flag_label.setText("Posts automatically" if on else "Copy and paste (automatic posting is off)")
        tip(self.flag_label, "Automatic posting is on: Reelay posts scheduled videos here for you." if on else
            "Reelay won't post here on its own. Turn on automatic posting in Settings "
            f"(or set {env_var(self.spec.platform)}=1).")
        while self.list.count():
            w = self.list.takeAt(0).widget()
            if w:
                w.deleteLater()
        profile = self.page.profile
        accounts = self.page.store.list_accounts(profile.id, self.spec.platform) if profile else []
        problems = 0
        for acc in accounts:
            buttons = []
            problem = ""
            if not self.page.service.has_credentials(acc):
                problem = "Sign-in details missing: connect again"
                problems += 1
            else:
                verify = tip(QPushButton("Check"), "Make sure the saved details still work. Nothing is posted.")
                verify.clicked.connect(lambda _=False, a=acc: self._verify(a))
                buttons.append(verify)
            remove = tip(QPushButton("Disconnect"), "Remove this account from the profile")
            remove.clicked.connect(lambda _=False, a=acc: self._disconnect(a))
            buttons.append(remove)
            row = account_row(acc, problem, buttons)
            if not problem:
                checked = local_text(acc.last_verified_at) if acc.last_verified_at else "never"
                row.setToolTip(f"Last checked: {checked}")
            self.list.addWidget(row)
        self.connected = int(bool(accounts))
        self.set_status(accounts, problems)

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
        account = self.page.service.connect_platform(self.page.profile.id, self.spec.platform, identity, secret)
        self.status.setText(f"Connected {account.display_name}.")
        self.refresh()

    def _verify(self, account: Account) -> None:
        secret = self.page.service.credentials(account)
        if self._busy() or not secret:
            return
        self.status.setText(f"Checking {account.display_name}…")
        self.worker = CallWorker(self.spec.publisher().verify, secret, parent=self)
        self.worker.succeeded.connect(lambda _identity: (self.page.store.mark_verified(account.id),
                                                         self.status.setText(f"{account.display_name} is connected "
                                                                             "and working."),
                                                         self.refresh()))
        self.worker.failed.connect(self.status.setText)
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
        self.note = _label("" if self.available else why, "faint")
        self.note.setVisible(bool(why) and not self.available)
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


class SignInOnlyCard(IntegrationCard):
    """TikTok / Instagram / Snapchat: posted by hand. Signing in (where Reelay has an app) attaches the account
    for analytics."""

    def __init__(self, platform: str, text: str, page: "AccountsPage"):
        super().__init__(platform, "Copy and paste", text)
        self.page = page
        self.connected = 0
        self.list = QVBoxLayout()
        self.list.setSpacing(6)
        self.body.addLayout(self.list)
        row = QHBoxLayout()
        self.signin = SignInButton(platform, self)
        self.signin.connected.connect(lambda account: (self.status.setText(f"Connected {account.display_name}."),
                                                       self.refresh()))
        row.addWidget(self.signin)
        row.addStretch()
        self.body.addLayout(row)
        self.body.addWidget(self.signin.note)
        self.status = _label("", "muted")
        self.body.addWidget(self.status)

    def refresh(self) -> None:
        while self.list.count():
            w = self.list.takeAt(0).widget()
            if w:
                w.deleteLater()
        profile = self.page.profile
        accounts = self.page.store.list_accounts(profile.id, self.platform) if profile else []
        for acc in accounts:
            remove = tip(QPushButton("Disconnect"), "Remove this account from the profile")
            remove.clicked.connect(lambda _=False, a=acc: self._disconnect(a))
            self.list.addWidget(account_row(acc, "", [remove]))
        self.connected = int(bool(accounts))
        self.set_status(accounts, hand=True)

    def _disconnect(self, account) -> None:
        if QMessageBox.question(self, "Disconnect", f"Disconnect {account.display_name}?") == QMessageBox.Yes:
            self.page.service.disconnect(account)
            self.refresh()

    def shutdown(self) -> None:
        pass
