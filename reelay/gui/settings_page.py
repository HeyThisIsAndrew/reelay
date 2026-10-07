"""Settings page: appearance, upload defaults, data locations, about."""

from __future__ import annotations

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
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

    def __init__(self, store: Store, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.store = store
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
