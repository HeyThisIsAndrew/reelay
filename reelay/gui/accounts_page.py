"""Accounts page: Google API setup, connected YouTube channels, channel schedule,
and what's needed for TikTok / Instagram."""

from __future__ import annotations

import threading
import webbrowser
from datetime import datetime

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMessageBox,
                               QPushButton, QScrollArea, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from dataclasses import dataclass

from ..accounts import YOUTUBE, AccountService
from ..models import Account, Platform, Profile
from ..publishers import BlueskyPublisher, FacebookPublisher, ThreadsPublisher
from ..publishing import parse_utc
from ..store import Store
from ..youtube import OAuthClient, YouTubeError, watch_url
from .post_panel import local_text
from .theme import set_prop
from .workers import CallWorker

SETUP_STEPS = (
    "<ol style='margin-left:-18px'>"
    "<li>Open the Google Cloud Console and create a project (any name, e.g. “Reelay”).</li>"
    "<li>Under <b>APIs &amp; Services → Library</b>, enable <b>YouTube Data API v3</b>.</li>"
    "<li>Under <b>OAuth consent screen</b>, choose <b>External</b>, fill in the app name and your email, "
    "and add every Google account you'll connect as a <b>test user</b>.</li>"
    "<li>Under <b>Credentials → Create credentials → OAuth client ID</b>, pick <b>Desktop app</b>, then "
    "<b>Download JSON</b>.</li>"
    "<li>Click <b>Import client_secret.json</b> below and choose that file.</li></ol>"
)
CAVEATS = (
    "Good to know: while your Google project is in <b>Testing</b> mode, Google signs you out every 7 days "
    "(just reconnect). Videos uploaded through a project that hasn't passed YouTube's free "
    "<a href='https://support.google.com/youtube/contact/yt_api_form'>API compliance audit</a> stay "
    "<b>private</b>, so scheduled videos won't go public until that audit is approved."
)


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


class AccountsPage(QWidget):
    accountsChanged = Signal()

    def __init__(self, store: Store, accounts: AccountService, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.store, self.service = store, accounts
        self.profile: Profile | None = None
        self.signin_worker: CallWorker | None = None
        self.signin_cancel: threading.Event | None = None
        self.schedule_worker: CallWorker | None = None
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

        self.body.addWidget(QLabel("Accounts", objectName="PageTitle"))
        self.subtitle = _label("", "muted")
        self.body.addWidget(self.subtitle)

        # ---- YouTube
        yt, yl = _card()
        head = QHBoxLayout()
        head.addWidget(QLabel("YouTube", objectName="DetailTitle"))
        head.addStretch()
        self.creds_label = _label("", "faint")
        head.addWidget(self.creds_label)
        yl.addLayout(head)

        self.setup_box = QWidget()
        sl = QVBoxLayout(self.setup_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(_label("One-time setup: Reelay talks to YouTube through your own free Google API "
                            "project, so your channels and quota stay yours.", "muted"))
        sl.addWidget(_label(SETUP_STEPS, rich=True))
        srow = QHBoxLayout()
        console = QPushButton("Open Google Cloud Console")
        console.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl("https://console.cloud.google.com/apis/library/youtube.googleapis.com")))
        self.import_btn = QPushButton("Import client_secret.json…")
        set_prop(self.import_btn, "primary")
        self.import_btn.clicked.connect(self._import_client)
        srow.addWidget(console)
        srow.addWidget(self.import_btn)
        srow.addStretch()
        sl.addLayout(srow)
        yl.addWidget(self.setup_box)

        self.connected_box = QWidget()
        cl = QVBoxLayout(self.connected_box)
        cl.setContentsMargins(0, 0, 0, 0)
        self.accounts_list = QVBoxLayout()
        self.accounts_list.setSpacing(6)
        cl.addLayout(self.accounts_list)
        crow = QHBoxLayout()
        self.connect_btn = QPushButton("Connect YouTube Channel")
        set_prop(self.connect_btn, "primary")
        self.connect_btn.clicked.connect(self._connect)
        self.cancel_connect_btn = QPushButton("Cancel Sign-in")
        self.cancel_connect_btn.clicked.connect(self._cancel_connect)
        self.cancel_connect_btn.hide()
        replace_creds = QPushButton("Change API Credentials…")
        set_prop(replace_creds, "flat")
        replace_creds.clicked.connect(self._import_client)
        crow.addWidget(self.connect_btn)
        crow.addWidget(self.cancel_connect_btn)
        crow.addStretch()
        crow.addWidget(replace_creds)
        cl.addLayout(crow)
        self.connect_status = _label("", "muted")
        cl.addWidget(self.connect_status)
        yl.addWidget(self.connected_box)
        yl.addWidget(_label(CAVEATS, "faint", rich=True))
        self.body.addWidget(yt)

        # ---- channel schedule
        sc, scl = _card()
        shead = QHBoxLayout()
        shead.addWidget(QLabel("Channel Schedule", objectName="DetailTitle"))
        shead.addStretch()
        self.schedule_account = QComboBox()
        self.schedule_account.setMinimumWidth(220)
        shead.addWidget(self.schedule_account)
        self.load_schedule_btn = QPushButton("Load from YouTube")
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

        # ---- Bluesky / Threads / Facebook
        self.platform_cards: dict[str, PlatformCard] = {}
        for spec in PLATFORM_SPECS:
            card = PlatformCard(spec, self)
            self.platform_cards[spec.platform] = card
            self.body.addWidget(card)

        # ---- not yet supported
        for name, text in (
            ("TikTok", "Posting isn't available yet. TikTok's Content Posting API requires registering a TikTok "
                       "developer app and passing TikTok's audit; until then, API uploads can only be "
                       "posted as private (“Only me”). Downloading from TikTok already works on the "
                       "Download page."),
            ("Instagram", "Posting isn't available yet. Publishing Reels requires an Instagram Professional "
                          "(Creator or Business) account linked to a Facebook Page, and a Meta app that "
                          "passes App Review."),
        ):
            card, lay = _card()
            row = QHBoxLayout()
            row.addWidget(QLabel(name, objectName="DetailTitle"))
            row.addStretch()
            row.addWidget(_label("Not available yet", "faint"))
            lay.addLayout(row)
            lay.addWidget(_label(text, "muted"))
            self.body.addWidget(card)
        self.body.addStretch()

    # ---------------------------------------------------------------- state
    def set_profile(self, profile: Profile) -> None:
        self.profile = profile
        self.subtitle.setText(f"Channels that {profile.name} publishes to. Each profile has its own accounts, "
                              "so you can keep creators separate.")
        self.schedule.clear()
        self.refresh()

    def refresh(self) -> None:
        client = self.service.oauth_client()
        self.setup_box.setVisible(client is None)
        self.connected_box.setVisible(client is not None)
        self.creds_label.setText(f"API client …{client.client_id.split('.')[0][-8:]}" if client else "")

        while self.accounts_list.count():
            w = self.accounts_list.takeAt(0).widget()
            if w:
                w.deleteLater()
        accounts = self.store.list_accounts(self.profile.id, YOUTUBE) if self.profile else []
        if not accounts:
            self.accounts_list.addWidget(_label("No channels connected to this profile yet.", "faint"))
        for acc in accounts:
            row = QFrame()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 2, 0, 2)
            dot = QLabel("●")
            dot.setProperty("muted", True)
            rl.addWidget(dot)
            rl.addWidget(QLabel(f"<b>{acc.display_name}</b>  <span style='opacity:.6'>{acc.handle}</span>"))
            rl.addStretch()
            if self.service.load_tokens(acc) is None:
                warn = _label("Signed out: reconnect", "danger")
                rl.addWidget(warn)
            remove = QPushButton("Disconnect")
            set_prop(remove, "flat")
            remove.clicked.connect(lambda _=False, a=acc: self._disconnect(a))
            rl.addWidget(remove)
            self.accounts_list.addWidget(row)

        for card in getattr(self, "platform_cards", {}).values():
            card.refresh()

        self.schedule_account.clear()
        for acc in accounts:
            self.schedule_account.addItem(acc.label, acc.id)
        self.load_schedule_btn.setEnabled(bool(accounts))

    # ---------------------------------------------------------------- actions
    def _import_client(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose client_secret.json", "", "JSON (*.json)")
        if not path:
            return
        try:
            client = OAuthClient.from_json(path)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Couldn't use that file", str(exc))
            return
        self.service.set_oauth_client(client)
        self.connect_status.setText("API credentials saved. Now connect a channel.")
        self.refresh()

    def _connect(self) -> None:
        oauth = self.service.oauth_client()
        if oauth is None or not self.profile or (self.signin_worker and self.signin_worker.isRunning()):
            return
        self.signin_cancel = threading.Event()
        self.signin_worker = CallWorker(self.service.sign_in, oauth, webbrowser.open, self.signin_cancel, parent=self)
        self.signin_worker.succeeded.connect(self._on_signed_in)
        self.signin_worker.failed.connect(self._on_signin_failed)
        self.signin_worker.finished.connect(self._signin_done)
        self.connect_btn.setEnabled(False)
        self.cancel_connect_btn.show()
        self.connect_status.setText("Finish signing in with Google in your browser, then pick the channel to connect…")
        self.signin_worker.start()

    def _cancel_connect(self) -> None:
        if self.signin_cancel:
            self.signin_cancel.set()

    def _on_signed_in(self, result) -> None:
        channel, tokens = result
        account = self.service.finish_connect(self.profile.id, channel, tokens)
        self.connect_status.setText(f"Connected {account.display_name}.")
        self.refresh()
        self.accountsChanged.emit()

    def _on_signin_failed(self, message: str) -> None:
        self.connect_status.setText(message)

    def _signin_done(self) -> None:
        self.connect_btn.setEnabled(True)
        self.cancel_connect_btn.hide()

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
        self._cancel_connect()
        if self.signin_worker:
            self.signin_worker.wait(2000)


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
                 "Create an App Password in Bluesky → Settings → Privacy and security → App passwords. Don't use "
                 "your main password. Video posting needs a verified email on the account.", BlueskyPublisher),
    PlatformSpec(Platform.THREADS, (("access_token", "Threads user access token", True),),
                 "Needs a Meta developer app with the Threads use case and the threads_basic + "
                 "threads_content_publish permissions (approved through App Review to post for accounts you "
                 "don't own). Generate a long-lived user access token (valid 60 days, then refresh it). Videos "
                 "are fetched by Meta from your Cloudflare Tunnel, so also set the Public URL in Settings.",
                 ThreadsPublisher),
    PlatformSpec(Platform.FACEBOOK, (("page_id", "Page ID", False), ("page_access_token", "Page access token", True)),
                 "Needs a Meta developer app with pages_manage_posts, pages_read_engagement and pages_show_list "
                 "(App Review for Pages you don't manage). Use a long-lived Page access token from someone who can "
                 "create content on the Page.", FacebookPublisher),
)


class PlatformCard(QFrame):
    """Connect / verify / disconnect accounts for one platform."""

    def __init__(self, spec: PlatformSpec, page: "AccountsPage"):
        super().__init__(objectName="Card")
        self.spec, self.page = spec, page
        self.worker: CallWorker | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(QLabel(Platform.LABELS[spec.platform], objectName="DetailTitle"))
        head.addStretch()
        self.flag_label = _label("", "faint")
        head.addWidget(self.flag_label)
        lay.addLayout(head)
        lay.addWidget(_label(spec.help, "muted"))
        self.list = QVBoxLayout()
        self.list.setSpacing(4)
        lay.addLayout(self.list)
        row = QHBoxLayout()
        self.inputs: dict[str, QLineEdit] = {}
        for key, label, password in spec.fields:
            edit = QLineEdit()
            edit.setPlaceholderText(label)
            if password:
                edit.setEchoMode(QLineEdit.Password)
            edit.setAttribute(Qt.WA_MacShowFocusRect, False)
            self.inputs[key] = edit
            row.addWidget(edit, 1)
        self.connect_btn = QPushButton("Connect")
        set_prop(self.connect_btn, "primary")
        self.connect_btn.clicked.connect(self._connect)
        row.addWidget(self.connect_btn)
        lay.addLayout(row)
        self.status = _label("", "muted")
        lay.addWidget(self.status)

    def refresh(self) -> None:
        from ..flags import env_var, publishing_enabled
        on = publishing_enabled(self.spec.platform, self.page.store)
        self.flag_label.setText("Automatic posting on" if on else
                                f"Copy-paste only (turn on in Settings or {env_var(self.spec.platform)}=1)")
        while self.list.count():
            w = self.list.takeAt(0).widget()
            if w:
                w.deleteLater()
        profile = self.page.profile
        accounts = self.page.store.list_accounts(profile.id, self.spec.platform) if profile else []
        if not accounts:
            self.list.addWidget(_label("No account connected to this profile yet.", "faint"))
        for acc in accounts:
            row = QFrame()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 2, 0, 2)
            rl.addWidget(QLabel(f"<b>{acc.display_name}</b>  <span style='opacity:.6'>{acc.handle}</span>"))
            rl.addStretch()
            if not self.page.service.has_credentials(acc):
                rl.addWidget(_label("Credential missing: reconnect", "danger"))
            else:
                checked = local_text(acc.last_verified_at) if acc.last_verified_at else "never"
                rl.addWidget(_label(f"Last verified: {checked}", "faint"))
                verify = QPushButton("Verify")
                set_prop(verify, "flat")
                verify.clicked.connect(lambda _=False, a=acc: self._verify(a))
                rl.addWidget(verify)
            remove = QPushButton("Disconnect")
            set_prop(remove, "flat")
            remove.clicked.connect(lambda _=False, a=acc: self._disconnect(a))
            rl.addWidget(remove)
            self.list.addWidget(row)

    def _busy(self) -> bool:
        return bool(self.worker and self.worker.isRunning())

    def _connect(self) -> None:
        if self._busy() or not self.page.profile:
            return
        secret = {key: edit.text().strip() for key, edit in self.inputs.items()}
        missing = [label for key, label, _ in self.spec.fields if not secret[key]]
        if missing:
            self.status.setText(f"Fill in: {', '.join(missing)}.")
            return
        self.status.setText("Checking with " + Platform.LABELS[self.spec.platform] + "…")
        self.connect_btn.setEnabled(False)
        self.worker = CallWorker(self.spec.publisher().verify, secret, parent=self)
        self.worker.succeeded.connect(lambda identity, s=secret: self._connected(identity, s))
        self.worker.failed.connect(self.status.setText)
        self.worker.finished.connect(lambda: self.connect_btn.setEnabled(True))
        self.worker.start()

    def _connected(self, identity: dict, secret: dict) -> None:
        if self.spec.platform == Platform.THREADS:
            secret = {**secret, "user_id": identity["external_id"]}
        account = self.page.service.connect_platform(self.page.profile.id, self.spec.platform, identity, secret)
        for edit in self.inputs.values():
            edit.clear()
        self.status.setText(f"Connected {account.display_name}.")
        self.refresh()

    def _verify(self, account: Account) -> None:
        secret = self.page.service.credentials(account)
        if self._busy() or not secret:
            return
        self.status.setText(f"Checking {account.display_name}…")
        self.worker = CallWorker(self.spec.publisher().verify, secret, parent=self)
        self.worker.succeeded.connect(lambda _identity: (self.page.store.mark_verified(account.id),
                                                         self.status.setText(f"{account.display_name} works."),
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
