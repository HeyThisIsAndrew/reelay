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
from .theme import THEMES, set_prop
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

        self._build_assist_card(lay)

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

    # ---------------------------------------------------------------- local AI helper
    def _build_assist_card(self, lay) -> None:
        from PySide6.QtWidgets import QComboBox, QFileDialog

        from .. import assist
        card, cl = _card("Local AI helper")
        ok, why = assist.languagetool_available()
        cl.addWidget(_muted("Free writing help that runs on this computer: no paid AI service, no tokens. "
                            "Use it from the Assist ▾ menu on any caption."))
        self.grammar_status = _muted("Spelling & grammar: " + ("LanguageTool (offline, full grammar)." if ok else
                                     "built-in spelling check. " + why))
        cl.addWidget(self.grammar_status)
        row = QHBoxLayout()
        row.addWidget(_muted("Rewriting with"))
        self.assist_engine = tip(QComboBox(), "Which local model runner rewrites captions")
        for key, label in (("ollama", "Ollama (recommended)"), ("llamacpp", "llama.cpp model file"), ("off", "Off")):
            self.assist_engine.addItem(label, key)
        row.addWidget(self.assist_engine)
        row.addStretch()
        cl.addLayout(row)
        self.ollama_row = QWidget()
        orow = QHBoxLayout(self.ollama_row)
        orow.setContentsMargins(0, 0, 0, 0)
        self.ollama_url = tip(QLineEdit(), "Where Ollama listens (normally this computer)")
        self.ollama_url.setPlaceholderText(assist.DEFAULT_OLLAMA_URL)
        self.assist_model = tip(QComboBox(), "The model to use. Small 1B–3B models are fast; llama3.2:3b is a "
                                             "good start (`ollama pull llama3.2:3b`).")
        self.assist_model.setEditable(True)
        self.assist_model.setMinimumWidth(200)
        orow.addWidget(self.ollama_url, 1)
        orow.addWidget(self.assist_model)
        cl.addWidget(self.ollama_row)
        self.llama_row = QWidget()
        lrow = QHBoxLayout(self.llama_row)
        lrow.setContentsMargins(0, 0, 0, 0)
        self.llama_path = tip(QLineEdit(), "A .gguf model file on this computer")
        self.llama_path.setPlaceholderText("Choose a .gguf model file…")
        pick = tip(QPushButton("Choose…"), "Pick a .gguf model file")
        pick.clicked.connect(lambda: self._pick_model(QFileDialog))
        lrow.addWidget(self.llama_path, 1)
        lrow.addWidget(pick)
        cl.addWidget(self.llama_row)
        trow = QHBoxLayout()
        self.assist_test = tip(QPushButton("Test"), "Check that the local model is ready")
        self.assist_test.clicked.connect(self._test_assist)
        get = tip(QPushButton("Get Ollama…"), "Download Ollama, the free app that runs AI models locally")
        set_prop(get, "flat")
        get.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://ollama.com/download")))
        trow.addWidget(self.assist_test)
        trow.addWidget(get)
        trow.addStretch()
        cl.addLayout(trow)
        self.assist_status = _muted("")
        cl.addWidget(self.assist_status)
        self.assist_engine.currentIndexChanged.connect(self._save_assist)
        self.ollama_url.editingFinished.connect(self._save_assist)
        self.assist_model.currentTextChanged.connect(lambda _t: self._save_assist())
        self.llama_path.editingFinished.connect(self._save_assist)
        lay.addWidget(card)

    def _pick_model(self, dialog_cls) -> None:
        path, _ = dialog_cls.getOpenFileName(self, "Choose a Model File", "", "GGUF models (*.gguf)")
        if path:
            self.llama_path.setText(path)
            self._save_assist()

    def _refresh_assist(self) -> None:
        from .. import assist
        widgets = (self.assist_engine, self.ollama_url, self.assist_model, self.llama_path)
        for w in widgets:
            w.blockSignals(True)
        engine = self.store.get_setting(assist.SETTINGS["engine"], "ollama")
        self.assist_engine.setCurrentIndex(max(self.assist_engine.findData(engine), 0))
        self.ollama_url.setText(self.store.get_setting(assist.SETTINGS["ollama_url"], ""))
        model = self.store.get_setting(assist.SETTINGS["model"], assist.DEFAULT_MODEL) or assist.DEFAULT_MODEL
        if self.assist_model.findText(model) < 0:
            self.assist_model.addItem(model)
        self.assist_model.setCurrentText(model)
        self.llama_path.setText(self.store.get_setting(assist.SETTINGS["llamacpp_path"], ""))
        for w in widgets:
            w.blockSignals(False)
        self.ollama_row.setVisible(engine == "ollama")
        self.llama_row.setVisible(engine == "llamacpp")
        self.assist_test.setEnabled(engine != "off")

    def _save_assist(self) -> None:
        from .. import assist
        self.store.set_setting(assist.SETTINGS["engine"], self.assist_engine.currentData())
        self.store.set_setting(assist.SETTINGS["ollama_url"], self.ollama_url.text().strip())
        self.store.set_setting(assist.SETTINGS["model"], self.assist_model.currentText().strip())
        self.store.set_setting(assist.SETTINGS["llamacpp_path"], self.llama_path.text().strip())
        self._refresh_assist()

    def _test_assist(self) -> None:
        from .. import assist
        rewriter = assist.rewriter_from(self.store)
        if rewriter is None:
            self.assist_status.setText("Rewriting is off.")
            return
        ok, message = rewriter.status()
        if ok and isinstance(rewriter, assist.OllamaRewriter):
            try:
                names = rewriter.models()
                for name in names:
                    if self.assist_model.findText(name) < 0:
                        self.assist_model.addItem(name)
            except assist.AssistError:
                pass
        self.assist_status.setText(("✓ " if ok else "") + message)
        set_prop(self.assist_status, "danger", not ok)

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
        self._refresh_assist()

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
