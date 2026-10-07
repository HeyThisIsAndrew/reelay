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
from .theme import THEMES


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
            self.theme_group.addButton(b)
            self.theme_buttons[name] = b
            b.clicked.connect(lambda _=False, n=name: self.themeChosen.emit(n))
            row.addWidget(b)
        row.addStretch()
        cl.addLayout(row)
        lay.addWidget(card)

        card, cl = _card("Uploading")
        self.shorts = QCheckBox("Add #Shorts to YouTube descriptions by default")
        self.shorts.toggled.connect(lambda on: self.store.set_setting("add_shorts", "1" if on else "0"))
        cl.addWidget(self.shorts)
        lay.addWidget(card)

        card, cl = _card("Automatic posting")
        cl.addWidget(_muted("Reelay is a ticket system first: each card holds copy for every platform to paste by "
                            "hand. YouTube can always post automatically. These platforms can too, once turned on "
                            "(and connected under Accounts). Changes take effect after restarting Reelay."))
        self.flag_boxes: dict[str, QCheckBox] = {}
        for platform in FLAG_PLATFORMS:
            box = QCheckBox(f"Post to {Platform.LABELS[platform]} automatically")
            box.toggled.connect(lambda on, p=platform: (self.store.set_setting(setting_key(p), "1" if on else "0"),
                                                        self._refresh_flags()))
            self.flag_boxes[platform] = box
            cl.addWidget(box)
        cl.addWidget(_muted("Facebook schedules natively (it holds the video until the set time). Bluesky and "
                            "Threads are posted by Reelay at the set time, so Reelay must be running and the Mac "
                            "awake then."))
        lay.addWidget(card)

        card, cl = _card("Agent API")
        cl.addWidget(_muted("A local API that lets your AI agents read the board, apply approved edits and schedule "
                            "posts. Every request needs the token below (sent as “Authorization: Bearer …”)."))
        self.api_status = _muted("")
        cl.addWidget(self.api_status)
        row = QHBoxLayout()
        self.copy_token_btn = QPushButton("Copy Token")
        self.copy_token_btn.clicked.connect(self._copy_token)
        self.rotate_token_btn = QPushButton("Rotate Token…")
        self.rotate_token_btn.clicked.connect(self._rotate_token)
        row.addWidget(self.copy_token_btn)
        row.addWidget(self.rotate_token_btn)
        row.addStretch()
        cl.addLayout(row)
        cl.addWidget(_muted("Public URL (your Cloudflare Tunnel address, e.g. https://reelay.example.com). "
                            "Threads downloads videos from short-lived links under this address."))
        self.public_url = QLineEdit()
        self.public_url.setPlaceholderText("https://…")
        self.public_url.editingFinished.connect(
            lambda: self.store.set_setting("public_url", self.public_url.text().strip().rstrip("/")))
        cl.addWidget(self.public_url)
        lay.addWidget(card)

        card, cl = _card("Data")
        cl.addWidget(_muted(f"Database and caches: {data_dir()}\nLogs: {log_dir()}\n"
                            "Sign-in tokens are stored in your system keychain, not in these folders."))
        row = QHBoxLayout()
        for text, path in (("Open Data Folder", data_dir()), ("Open Logs", log_dir())):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, p=path: QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))))
            row.addWidget(b)
        row.addStretch()
        cl.addLayout(row)
        lay.addWidget(card)

        card, cl = _card("About")
        cl.addWidget(_muted(f"{APP_NAME} {__version__}: download, stage and schedule a creator's short-form "
                            "videos across platforms. Downloads use yt-dlp; only download content you have "
                            "the rights to republish."))
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
            tip = (f"Set by {env_var(platform)}" if source == "env" else
                   "Restart Reelay to apply" if enabled != (platform in running) else "")
            box.setToolTip(tip)
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
            text = f"Running at {api.url}. Machine-readable description: {api.url}/openapi.json"
        elif api is not None and api.error:
            text = api.error
        else:
            text = "Not running."
        if os.environ.get("REELAY_API_TOKEN"):
            text += "\nToken comes from the REELAY_API_TOKEN environment variable."
        else:
            text += "\nToken is stored in your system keychain (service “Reelay”, account “agent_api_token”)."
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
            self.api_status.setText(self.api_status.text().split("\n")[0] + "\nToken copied to the clipboard.")

    def _rotate_token(self) -> None:
        if not self.services:
            return
        answer = QMessageBox.question(self, "Rotate Agent Token",
                                      "Create a new agent API token? Agents using the old one will get 401 errors "
                                      "until you give them the new token.")
        if answer == QMessageBox.Yes:
            try:
                self.services.rotate_agent_token()
            except RuntimeError as exc:
                QMessageBox.warning(self, "Rotate Agent Token", str(exc))
                return
            QGuiApplication.clipboard().setText(self.services.agent_token())
            self._refresh_api()
            self.api_status.setText(self.api_status.text() + "\nNew token copied to the clipboard.")
