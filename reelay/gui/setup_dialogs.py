"""Guided, in-app setup for connecting accounts.

Honest about what each platform needs:
  - YouTube: your own free Google Cloud project (Google requires one for apps
    like Reelay). A step-by-step wizard ends with pasting a client ID and
    secret; after that, connecting a channel is one "Sign in with Google" click.
  - Bluesky: an App Password (Bluesky has no third-party sign-in page).
  - Threads / Facebook: your own Meta developer app and a token you paste.
"""

from __future__ import annotations

import threading
import webbrowser

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton,
                               QStackedWidget, QVBoxLayout, QWidget)

from ..youtube import OAuthClient
from .theme import set_prop
from .tips import tip
from .workers import CallWorker


def _text(text: str, prop: str | None = None) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setTextFormat(Qt.RichText)
    lab.setOpenExternalLinks(True)
    if prop:
        lab.setProperty(prop, True)
    return lab


def _link_button(label: str, url: str, hint: str = "") -> QPushButton:
    btn = QPushButton(label)
    btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
    tip(btn, hint or f"Opens {url} in your browser")
    return btn


# ====================================================================== YouTube
GOOGLE_STEPS = [
    ("Create a Google Cloud project",
     "Google requires apps like Reelay to connect through a project that you own. It's free and takes about "
     "five minutes, once. Your channels and upload quota stay yours.<br><br>"
     "Click the button, give the project any name (for example <b>Reelay</b>), and click <b>Create</b>.",
     "Create a Project", "https://console.cloud.google.com/projectcreate"),
    ("Turn on the YouTube Data API",
     "Make sure your new project is selected at the top of the page, then click <b>Enable</b>.",
     "Open the YouTube Data API Page", "https://console.cloud.google.com/apis/library/youtube.googleapis.com"),
    ("Set up the sign-in screen",
     "Click <b>Get started</b>. Use any app name (for example <b>Reelay</b>) and your email. "
     "For <b>Audience</b>, choose <b>External</b>. Finish and click <b>Create</b>.<br><br>"
     "Then open <b>Audience</b> and, under <b>Test users</b>, add every Google account whose channel "
     "you'll connect.",
     "Open the Sign-in Screen Settings", "https://console.cloud.google.com/auth/overview"),
    ("Create a client ID",
     "Click <b>Create client</b>, choose <b>Desktop app</b> as the application type, give it any name, "
     "and click <b>Create</b>. Google then shows a <b>Client ID</b> and <b>Client secret</b>. Keep that "
     "window open for the next step.",
     "Create a Client ID", "https://console.cloud.google.com/auth/clients/create"),
]


class YouTubeSetupWizard(QDialog):
    """Five short steps; the result is an OAuthClient (client ID + secret)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set Up YouTube")
        self.setMinimumWidth(560)
        self.client: OAuthClient | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(12)
        self.step_label = _text("", "faint")
        lay.addWidget(self.step_label)
        self.stack = QStackedWidget()
        for title, body, button, url in GOOGLE_STEPS:
            page = QWidget()
            pl = QVBoxLayout(page)
            pl.setContentsMargins(0, 0, 0, 0)
            pl.setSpacing(12)
            heading = QLabel(title, objectName="DetailTitle")
            pl.addWidget(heading)
            pl.addWidget(_text(body, "muted"))
            row = QHBoxLayout()
            row.addWidget(_link_button(button, url))
            row.addStretch()
            pl.addLayout(row)
            pl.addStretch()
            self.stack.addWidget(page)
        self.stack.addWidget(self._paste_page())
        lay.addWidget(self.stack, 1)
        lay.addWidget(_text("Good to know: while the project is in <b>Testing</b>, Google signs you out every "
                            "7 days (just sign in again). Uploads stay <b>private</b> until the project passes "
                            "YouTube's free <a href='https://support.google.com/youtube/contact/yt_api_form'>"
                            "API compliance audit</a>.", "faint"))
        nav = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        self.back_btn = QPushButton("Back")
        self.back_btn.clicked.connect(lambda: self._go(-1))
        self.next_btn = QPushButton("Next")
        set_prop(self.next_btn, "primary")
        self.next_btn.clicked.connect(self._next)
        nav.addWidget(self.cancel_btn)
        nav.addStretch()
        nav.addWidget(self.back_btn)
        nav.addWidget(self.next_btn)
        lay.addLayout(nav)
        self._go(0)

    def _paste_page(self) -> QWidget:
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(10)
        pl.addWidget(QLabel("Paste your client ID and secret", objectName="DetailTitle"))
        pl.addWidget(_text("Copy them from the Google window. They're stored in your Mac's Keychain, "
                           "never in a file.", "muted"))
        self.client_id = QLineEdit()
        self.client_id.setPlaceholderText("Client ID (ends in .apps.googleusercontent.com)")
        tip(self.client_id, "The Client ID Google showed when you created the Desktop app client")
        self.client_secret = QLineEdit()
        self.client_secret.setEchoMode(QLineEdit.Password)
        self.client_secret.setPlaceholderText("Client secret")
        tip(self.client_secret, "The Client secret shown next to the Client ID")
        for edit in (self.client_id, self.client_secret):
            edit.setAttribute(Qt.WA_MacShowFocusRect, False)
            edit.textChanged.connect(self._validate)
            pl.addWidget(edit)
        row = QHBoxLayout()
        load = QPushButton("Or Load the Downloaded JSON File…")
        set_prop(load, "flat")
        tip(load, "If you clicked Download JSON in Google Cloud, choose that client_secret file instead")
        load.clicked.connect(self._load_json)
        row.addWidget(load)
        row.addStretch()
        pl.addLayout(row)
        self.paste_error = _text("", "danger")
        pl.addWidget(self.paste_error)
        pl.addStretch()
        return page

    @property
    def last(self) -> int:
        return self.stack.count() - 1

    def _go(self, delta_or_index: int, absolute: bool = False) -> None:
        index = delta_or_index if absolute else self.stack.currentIndex() + delta_or_index
        index = max(0, min(self.last, index))
        self.stack.setCurrentIndex(index)
        self.step_label.setText(f"Step {index + 1} of {self.stack.count()}")
        self.back_btn.setEnabled(index > 0)
        self.next_btn.setText("Save and Continue" if index == self.last else "Next")
        self._validate()

    def _next(self) -> None:
        if self.stack.currentIndex() < self.last:
            self._go(1)
            return
        self.client = OAuthClient(self.client_id.text().strip(), self.client_secret.text().strip())
        self.accept()

    def _validate(self) -> None:
        if self.stack.currentIndex() != self.last:
            self.next_btn.setEnabled(True)
            return
        cid = self.client_id.text().strip()
        ok = cid.endswith(".apps.googleusercontent.com") and bool(self.client_secret.text().strip())
        self.paste_error.setText("" if ok or not cid or cid.endswith(".apps.googleusercontent.com")
                                 else "That doesn't look like a Google client ID; it should end in "
                                      ".apps.googleusercontent.com.")
        self.next_btn.setEnabled(ok)

    def _load_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose the client_secret file", "", "JSON (*.json)")
        if not path:
            return
        try:
            client = OAuthClient.from_json(path)
        except (ValueError, OSError) as exc:
            self.paste_error.setText(str(exc))
            return
        self.client_id.setText(client.client_id)
        self.client_secret.setText(client.client_secret)

    def jump_to_paste(self) -> None:
        self._go(self.last, absolute=True)


class GoogleSignInDialog(QDialog):
    """'Sign in with Google' in a modal: progress, a way to reopen the page, Cancel."""

    signedIn = Signal(object)   # (channel, tokens)

    def __init__(self, sign_in, oauth: OAuthClient, parent=None, open_browser=webbrowser.open):
        super().__init__(parent)
        self.setWindowTitle("Sign in with Google")
        self.setMinimumWidth(460)
        self._open = open_browser
        self._url = ""
        self.cancel = threading.Event()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(12)
        lay.addWidget(QLabel("Sign in with Google", objectName="DetailTitle"))
        self.message = _text("A Google sign-in page is opening in your browser. Choose the account that owns the "
                             "channel, allow access, then come back here.", "muted")
        lay.addWidget(self.message)
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setTextVisible(False)
        lay.addWidget(bar)
        self.bar = bar
        row = QHBoxLayout()
        self.reopen_btn = QPushButton("Open the Sign-in Page Again")
        set_prop(self.reopen_btn, "flat")
        tip(self.reopen_btn, "If the browser didn't open, or you closed the tab")
        self.reopen_btn.clicked.connect(lambda: self._url and self._open(self._url))
        self.reopen_btn.setEnabled(False)
        row.addWidget(self.reopen_btn)
        row.addStretch()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        row.addWidget(self.cancel_btn)
        lay.addLayout(row)
        self.worker = CallWorker(sign_in, oauth, self._open_page, self.cancel, parent=self)
        self.worker.succeeded.connect(self._done)
        self.worker.failed.connect(self._failed)

    def _open_page(self, url: str) -> None:   # called from the worker thread
        self._url = url
        self._open(url)

    def exec(self) -> int:  # noqa: A003
        self.worker.start()
        return super().exec()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.reopen_btn.setEnabled(True)

    def _done(self, result) -> None:
        self.signedIn.emit(result)
        self.accept()

    def _failed(self, message: str) -> None:
        if self.cancel.is_set():
            self.reject()
            return
        self.bar.hide()
        self.message.setText(f"Sign-in didn't finish: {message}")
        self.cancel_btn.setText("Close")
        self.reopen_btn.hide()

    def _cancel(self) -> None:
        self.cancel.set()
        if not self.worker.isRunning():
            self.reject()

    def reject(self) -> None:
        self.cancel.set()
        self.worker.wait(3000)
        super().reject()


# ====================================================================== token-based platforms
PLATFORM_GUIDES = {
    "bluesky": {
        "title": "Connect Bluesky",
        "intro": "Bluesky doesn't offer a “Sign in with Bluesky” page for apps. Instead you make an "
                 "<b>App Password</b>: a separate password just for Reelay that you can revoke any time. "
                 "Never use your main password.",
        "steps": ["Open Bluesky's App Passwords page and sign in if asked.",
                  "Click <b>Add App Password</b>, name it <b>Reelay</b>, and copy the password it shows.",
                  "Enter your handle and paste the App Password below."],
        "link": ("Open Bluesky App Passwords", "https://bsky.app/settings/app-passwords"),
        "note": "Posting videos needs a verified email on the Bluesky account.",
    },
    "threads": {
        "title": "Connect Threads",
        "intro": "Threads only lets apps post through a <b>Meta developer app you own</b>. There's no "
                 "one-click sign-in for Reelay itself, so this takes some setup on Meta's site.",
        "steps": ["Create a Meta developer app with the <b>Access the Threads API</b> use case.",
                  "Give it the <b>threads_basic</b> and <b>threads_content_publish</b> permissions, and add your "
                  "Threads account as a tester.",
                  "Generate a <b>long-lived user access token</b> (it lasts 60 days) and paste it below.",
                  "In Settings, set the Public URL: Threads fetches videos from a link, so Reelay needs one."],
        "link": ("Open Meta for Developers", "https://developers.facebook.com/apps/"),
        "note": "Posting for accounts you don't own requires Meta's App Review.",
    },
    "facebook": {
        "title": "Connect a Facebook Page",
        "intro": "Facebook only lets apps post to Pages through a <b>Meta developer app you own</b>. "
                 "This takes some setup on Meta's site.",
        "steps": ["Create a Meta developer app (type <b>Business</b>).",
                  "Give it <b>pages_manage_posts</b>, <b>pages_read_engagement</b> and <b>pages_show_list</b>.",
                  "In the Graph API Explorer, get a <b>long-lived Page access token</b> for your Page.",
                  "Paste the Page ID and that token below."],
        "link": ("Open Meta for Developers", "https://developers.facebook.com/apps/"),
        "note": "You must be able to create content on the Page. Pages you don't manage need App Review.",
    },
}


class TokenSetupDialog(QDialog):
    """Guided setup for platforms that use a pasted password or token.
    Verifies with the platform before anything is saved."""

    connected = Signal(dict, dict)   # identity, secret

    def __init__(self, platform: str, fields, verify, parent=None):
        super().__init__(parent)
        guide = PLATFORM_GUIDES[platform]
        self.setWindowTitle(guide["title"])
        self.setMinimumWidth(540)
        self.fields, self.verify = fields, verify
        self.worker: CallWorker | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(12)
        lay.addWidget(QLabel(guide["title"], objectName="DetailTitle"))
        lay.addWidget(_text(guide["intro"], "muted"))
        lay.addWidget(_text("<ol style='margin-left:-18px'>" + "".join(f"<li>{s}</li>" for s in guide["steps"])
                            + "</ol>"))
        row = QHBoxLayout()
        row.addWidget(_link_button(*guide["link"]))
        row.addStretch()
        lay.addLayout(row)
        self.inputs: dict[str, QLineEdit] = {}
        for key, label, password in fields:
            edit = QLineEdit()
            edit.setPlaceholderText(label)
            tip(edit, label + (". Stored only in your Mac's Keychain." if password else ""))
            if password:
                edit.setEchoMode(QLineEdit.Password)
            edit.setAttribute(Qt.WA_MacShowFocusRect, False)
            self.inputs[key] = edit
            lay.addWidget(edit)
        lay.addWidget(_text(guide["note"], "faint"))
        self.status = _text("", "muted")
        lay.addWidget(self.status)
        nav = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.connect_btn = QPushButton("Check and Connect")
        set_prop(self.connect_btn, "primary")
        tip(self.connect_btn, "Reelay checks these details with the platform first. Nothing is posted.")
        self.connect_btn.clicked.connect(self._connect)
        nav.addWidget(cancel)
        nav.addStretch()
        nav.addWidget(self.connect_btn)
        lay.addLayout(nav)

    def secret(self) -> dict:
        return {key: edit.text().strip() for key, edit in self.inputs.items()}

    def _connect(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        secret = self.secret()
        missing = [label for key, label, _ in self.fields if not secret[key]]
        if missing:
            self.status.setText(f"Fill in: {', '.join(missing)}.")
            return
        self.status.setText("Checking…")
        self.connect_btn.setEnabled(False)
        self.worker = CallWorker(self.verify, secret, parent=self)
        self.worker.succeeded.connect(lambda identity, s=secret: self._ok(identity, s))
        self.worker.failed.connect(self._failed)
        self.worker.start()

    def _ok(self, identity: dict, secret: dict) -> None:
        self.connected.emit(identity, secret)
        self.accept()

    def _failed(self, message: str) -> None:
        self.status.setText(message)
        self.connect_btn.setEnabled(True)

    def reject(self) -> None:
        if self.worker:
            self.worker.wait(3000)
        super().reject()
