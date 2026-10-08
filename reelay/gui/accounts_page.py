"""Accounts page: where videos go OUT. Guided setup for YouTube (your own Google
project, then one-click "Sign in with Google"), Bluesky (App Password),
Threads and Facebook (your own Meta app), and honest notes on TikTok and
Instagram."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel,
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
from .setup_dialogs import GoogleSignInDialog, TokenSetupDialog, YouTubeSetupWizard
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


class AccountsPage(QWidget):
    accountsChanged = Signal()

    def __init__(self, store: Store, accounts: AccountService, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.store, self.service = store, accounts
        self.profile: Profile | None = None
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
        self.auto_label = _label("Automatic posting and scheduling", "faint")
        head.addWidget(self.auto_label)
        self.creds_label = _label("", "faint")
        head.addWidget(self.creds_label)
        yl.addLayout(head)

        self.setup_box = QWidget()
        sl = QVBoxLayout(self.setup_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(10)
        sl.addWidget(_label("Google requires apps like Reelay to connect through a free Google Cloud project that "
                            "you own. Reelay walks you through it, once (about five minutes). After that, "
                            "connecting a channel is a single “Sign in with Google”.", "muted"))
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
        yl.addWidget(self.setup_box)

        self.connected_box = QWidget()
        cl = QVBoxLayout(self.connected_box)
        cl.setContentsMargins(0, 0, 0, 0)
        self.accounts_list = QVBoxLayout()
        self.accounts_list.setSpacing(6)
        cl.addLayout(self.accounts_list)
        crow = QHBoxLayout()
        self.connect_btn = QPushButton("Sign in with Google")
        set_prop(self.connect_btn, "primary")
        tip(self.connect_btn, "Connect a YouTube channel to this profile by signing in with its Google account")
        self.connect_btn.clicked.connect(self._connect)
        replace_creds = QPushButton("Change Google Setup…")
        set_prop(replace_creds, "flat")
        tip(replace_creds, "Use a different Google client ID and secret")
        replace_creds.clicked.connect(lambda: self._setup_youtube(paste=True))
        crow.addWidget(self.connect_btn)
        crow.addStretch()
        crow.addWidget(replace_creds)
        cl.addLayout(crow)
        self.connect_status = _label("", "muted")
        cl.addWidget(self.connect_status)
        yl.addWidget(self.connected_box)
        yl.addWidget(_label("While your Google project is in <b>Testing</b>, Google signs you out every 7 days "
                            "(just sign in again). Uploads stay <b>private</b> until the project passes YouTube's "
                            "free <a href='https://support.google.com/youtube/contact/yt_api_form'>API compliance "
                            "audit</a>, so scheduled videos won't go public before then.", "faint", rich=True))
        self.body.addWidget(yt)

        # ---- channel schedule
        sc, scl = _card()
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

        # ---- Bluesky / Threads / Facebook
        self.platform_cards: dict[str, PlatformCard] = {}
        for spec in PLATFORM_SPECS:
            card = PlatformCard(spec, self)
            self.platform_cards[spec.platform] = card
            self.body.addWidget(card)

        # ---- TikTok / Instagram: honest status
        for name, status, text in (
            ("TikTok", "Copy and paste",
             "Reelay can't post to TikTok for you. TikTok only allows automatic posting from developer apps "
             "it has reviewed and approved; without that approval, an app can only send private (“Only me”) "
             "posts or drafts. Copy the TikTok caption from a ticket and post in the TikTok app. Downloading "
             "your TikTok videos works on the Download page."),
            ("Instagram", "Copy and paste",
             "Reelay can't post to Instagram for you yet. Instagram requires a Professional (Creator or Business) "
             "account linked to a Facebook Page, and a Meta developer app that passes App Review. Copy the "
             "Instagram caption from a ticket and post in the Instagram app."),
        ):
            card, lay = _card()
            row = QHBoxLayout()
            row.addWidget(QLabel(name, objectName="DetailTitle"))
            row.addStretch()
            row.addWidget(_label(status, "faint"))
            lay.addLayout(row)
            lay.addWidget(_label(text, "muted"))
            self.body.addWidget(card)
        self.body.addStretch()

    # ---------------------------------------------------------------- state
    def set_profile(self, profile: Profile) -> None:
        self.profile = profile
        self.subtitle.setText(f"Where {profile.name}'s videos go out. (Videos come in on the Download page.) "
                              "Each profile has its own accounts, so creators stay separate.")
        self.schedule.clear()
        self.refresh()

    def refresh(self) -> None:
        client = self.service.oauth_client()
        self.setup_box.setVisible(client is None)
        self.connected_box.setVisible(client is not None)
        self.creds_label.setText("Google setup done" if client else "")

        while self.accounts_list.count():
            w = self.accounts_list.takeAt(0).widget()
            if w:
                w.deleteLater()
        accounts = self.store.list_accounts(self.profile.id, YOUTUBE) if self.profile else []
        if not accounts:
            self.accounts_list.addWidget(_label("No channels connected to this profile yet. Click "
                                                "“Sign in with Google” to add one.", "faint"))
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
                warn = _label("Signed out: click Sign in with Google again", "danger")
                rl.addWidget(warn)
            remove = tip(QPushButton("Disconnect"), "Remove this channel from the profile. Videos on YouTube stay.")
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
        self.connect_btn = QPushButton(f"Connect {Platform.LABELS[spec.platform]}…")
        set_prop(self.connect_btn, "primary")
        tip(self.connect_btn, "Step-by-step setup. Reelay checks the details with "
                              f"{Platform.LABELS[spec.platform]} before saving anything.")
        self.connect_btn.clicked.connect(self._open_setup)
        row.addWidget(self.connect_btn)
        row.addStretch()
        lay.addLayout(row)
        self.status = _label("", "muted")
        lay.addWidget(self.status)

    def refresh(self) -> None:
        from ..flags import env_var, publishing_enabled
        on = publishing_enabled(self.spec.platform, self.page.store)
        self.flag_label.setText("Automatic posting on" if on else "Copy and paste (automatic posting is off)")
        tip(self.flag_label, "Automatic posting is on: Reelay posts scheduled videos here for you." if on else
            "Reelay won't post here on its own. Turn on automatic posting in Settings "
            f"(or set {env_var(self.spec.platform)}=1).")
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
                rl.addWidget(_label("Sign-in details missing: connect again", "danger"))
            else:
                checked = local_text(acc.last_verified_at) if acc.last_verified_at else "never"
                rl.addWidget(_label(f"Last checked: {checked}", "faint"))
                verify = tip(QPushButton("Check Connection"), "Make sure the saved details still work. Nothing is posted.")
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
