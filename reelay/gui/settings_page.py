"""Settings page: appearance, upload defaults, data locations, about."""

from __future__ import annotations

import os

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from ..flags import FLAG_PLATFORMS, env_var, flag_source, setting_key
from ..models import Platform
from ..paths import data_dir, log_dir
from ..store import Store
from . import tips
from .theme import THEMES
from .tips import tip


def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame(objectName="Card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(18, 16, 18, 16)
    lay.setSpacing(10)
    lay.addWidget(QLabel(title, objectName="DetailTitle"))
    return frame, lay


def _muted(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setProperty("muted", True)
    lab.setWordWrap(True)
    return lab


class SettingsPage(QWidget):
    themeChosen = Signal(str)

    def __init__(self, store: Store, services=None, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.store = store
        self.services = services
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget(objectName="Page")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(24, 18, 24, 24)
        lay.setSpacing(14)
        scroll.setWidget(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        lay.addWidget(QLabel("Settings", objectName="PageTitle"))

        card, cl = _card("Appearance")
        cl.addWidget(_muted("Black is pure black for OLED screens."))
        row = QHBoxLayout()
        self.theme_group = QButtonGroup(self)
        self.theme_buttons: dict[str, QPushButton] = {}
        for name in ("light", "dark", "black"):
            b = QPushButton(THEMES[name].label)
            b.setCheckable(True)
            b.setMinimumWidth(110)
            tip(b, {"light": "Light background", "dark": "Dark gray background (the default)",
                    "black": "Pure black, for OLED screens"}[name])
            self.theme_group.addButton(b)
            self.theme_buttons[name] = b
            b.clicked.connect(lambda _=False, n=name: self.themeChosen.emit(n))
            row.addWidget(b)
        row.addStretch()
        cl.addLayout(row)
        self.tips_box = QCheckBox("Show helpful tips")
        tip(self.tips_box, "Show a short explanation when you hover over a button, field or heading")
        self.tips_box.toggled.connect(lambda on: tips.set_enabled(self.store, on))
        cl.addWidget(self.tips_box)
        cl.addWidget(_muted("Hover over almost anything in Reelay to see what it does."))
        lay.addWidget(card)

        card, cl = _card("Uploading")
        self.shorts = QCheckBox("Add #Shorts to YouTube descriptions by default")
        tip(self.shorts, "New tickets start with “Add #Shorts” ticked. You can still change it per video.")
        self.shorts.toggled.connect(lambda on: self.store.set_setting("add_shorts", "1" if on else "0"))
        cl.addWidget(self.shorts)
        lay.addWidget(card)

        card, cl = _card("Automatic posting")
        cl.addWidget(_muted("Every ticket holds ready-to-paste text for each platform, so you can always post by "
                            "hand. YouTube can always post automatically. Turn these on to let Reelay post to them "
                            "too (connect them under Accounts first). Changes apply after you restart Reelay."))
        self.flag_boxes: dict[str, QCheckBox] = {}
        for platform in FLAG_PLATFORMS:
            box = QCheckBox(f"Post to {Platform.LABELS[platform]} automatically")
            tip(box, f"When on, Reelay posts scheduled videos to {Platform.LABELS[platform]} for you. "
                     "When off, you copy and paste.")
            box.toggled.connect(lambda on, p=platform: (self.store.set_setting(setting_key(p), "1" if on else "0"),
                                                        self._refresh_flags()))
            self.flag_boxes[platform] = box
            cl.addWidget(box)
        cl.addWidget(_muted("Facebook schedules natively (it holds the video until the set time). Bluesky and "
                            "Threads are posted by Reelay at the set time, so Reelay must be running and the Mac "
                            "awake then."))
        lay.addWidget(card)

        card, cl = _card("AI agents (advanced)")
        cl.addWidget(_muted("Lets AI assistants you run on this Mac read your board, make edits and schedule posts. "
                            "Only programs that have the access key below can use it, and it only listens on this "
                            "Mac. If you don't use AI agents, you can ignore this."))
        self.api_status = _muted("")
        cl.addWidget(self.api_status)
        row = QHBoxLayout()
        self.copy_token_btn = tip(QPushButton("Copy Access Key"), "Copy the key to give to an AI agent")
        self.copy_token_btn.clicked.connect(self._copy_token)
        self.rotate_token_btn = tip(QPushButton("Make a New Key…"),
                                    "Replace the key. Agents using the old key stop working until you give "
                                    "them the new one.")
        self.rotate_token_btn.clicked.connect(self._rotate_token)
        row.addWidget(self.copy_token_btn)
        row.addWidget(self.rotate_token_btn)
        row.addStretch()
        cl.addLayout(row)
        cl.addWidget(_muted("Public web address (only needed for posting to Threads). Threads fetches each video "
                            "from a link instead of receiving the file, so Reelay needs an address on the internet, "
                            "such as a Cloudflare Tunnel (e.g. https://reelay.example.com). Links expire after "
                            "a short time."))
        self.public_url = QLineEdit()
        self.public_url.setPlaceholderText("https://…")
        tip(self.public_url, "Leave empty unless you post to Threads automatically")
        self.public_url.editingFinished.connect(
            lambda: self.store.set_setting("public_url", self.public_url.text().strip().rstrip("/")))
        cl.addWidget(self.public_url)
        lay.addWidget(card)

        card, cl = _card("Data")
        cl.addWidget(_muted(f"Database and caches: {data_dir()}\nLogs: {log_dir()}\n"
                            "Sign-in tokens are stored in your system keychain, not in these folders."))
        row = QHBoxLayout()
        for text, path, hint in (("Open Data Folder", data_dir(), "Where Reelay keeps its database and caches"),
                                 ("Open Logs", log_dir(), "Diagnostic logs, useful when reporting a problem")):
            b = tip(QPushButton(text), hint)
            b.clicked.connect(lambda _=False, p=path: QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))))
            row.addWidget(b)
        row.addStretch()
        cl.addLayout(row)
        lay.addWidget(card)

        card, cl = _card("About")
        cl.addWidget(_muted(f"{APP_NAME} {__version__}: download, stage and schedule a creator's short-form "
                            "videos across platforms. Downloads use yt-dlp. Only import media you own or have "
                            "permission to manage."))
        lay.addWidget(card)
        lay.addStretch()
        self.refresh()

    def refresh(self) -> None:
        current = self.store.get_setting("theme", "dark")
        for name, b in self.theme_buttons.items():
            b.setChecked(name == current)
        self.shorts.blockSignals(True)
        self.shorts.setChecked(self.store.get_setting("add_shorts", "1") == "1")
        self.shorts.blockSignals(False)
        self.tips_box.blockSignals(True)
        self.tips_box.setChecked(self.store.get_setting(tips.SETTING, "1") == "1")
        self.tips_box.blockSignals(False)
        self._refresh_flags()
        self._refresh_api()

    def _refresh_flags(self) -> None:
        running = self.services.enabled if self.services else frozenset()
        for platform, box in self.flag_boxes.items():
            enabled, source = flag_source(platform, self.store)
            box.blockSignals(True)
            box.setChecked(enabled)
            box.blockSignals(False)
            box.setEnabled(source != "env")
            if source == "env":
                box.setToolTip(f"Set by the {env_var(platform)} environment variable")
            elif enabled != (platform in running):
                box.setToolTip("Restart Reelay to apply this change")
            label = f"Post to {Platform.LABELS[platform]} automatically"
            if source == "env":
                label += f"  ({env_var(platform)}={'1' if enabled else '0'})"
            elif enabled != (platform in running):
                label += "  (restart to apply)"
            box.setText(label)

    def _refresh_api(self) -> None:
        api = self.services.api if self.services else None
        env_url = os.environ.get("REELAY_PUBLIC_URL")
        if os.environ.get("REELAY_API_DISABLED") == "1":
            text = "Off (REELAY_API_DISABLED=1)."
        elif api is not None and api.running:
            text = f"On, at {api.url} (description for agents: {api.url}/openapi.json)."
        elif api is not None and api.error:
            text = api.error
        else:
            text = "Off."
        if os.environ.get("REELAY_API_TOKEN"):
            text += "\nThe key comes from the REELAY_API_TOKEN environment variable."
        else:
            text += "\nThe key is kept in your Mac's Keychain. Agents send it as “Authorization: Bearer <key>”."
        self.api_status.setText(text)
        self.rotate_token_btn.setEnabled(bool(self.services) and not os.environ.get("REELAY_API_TOKEN"))
        self.copy_token_btn.setEnabled(bool(self.services))
        self.public_url.setText(env_url or self.store.get_setting("public_url"))
        self.public_url.setEnabled(not env_url)
        if env_url:
            self.public_url.setToolTip("Set by REELAY_PUBLIC_URL")

    def _copy_token(self) -> None:
        if self.services:
            QGuiApplication.clipboard().setText(self.services.agent_token())
            self.api_status.setText(self.api_status.text().split("\n")[0] + "\nKey copied to the clipboard.")

    def _rotate_token(self) -> None:
        if not self.services:
            return
        answer = QMessageBox.question(self, "Make a New Access Key",
                                      "Replace the access key? Agents using the old key will stop working "
                                      "until you give them the new one.")
        if answer == QMessageBox.Yes:
            try:
                self.services.rotate_agent_token()
            except RuntimeError as exc:
                QMessageBox.warning(self, "Make a New Access Key", str(exc))
                return
            QGuiApplication.clipboard().setText(self.services.agent_token())
            self._refresh_api()
            self.api_status.setText(self.api_status.text() + "\nNew key copied to the clipboard.")
