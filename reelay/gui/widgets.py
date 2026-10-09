"""Small reusable widgets: the friendly file-naming picker and the growing search box."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics, QKeyEvent
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QSizePolicy,
                               QVBoxLayout, QWidget)

from ..models import VideoItem
from ..naming import DEFAULT_TEMPLATE, PRESETS, TOKENS, render_stem
from .tips import tip

CUSTOM = "__custom__"


def _sample_item() -> VideoItem:
    return VideoItem(platform="TikTok", video_id="7692175258450677023", url="", custom_name="letting-go",
                     upload_date=date.today().strftime("%Y%m%d"), uploader="livingood.lifestyle",
                     description="the loneliness in the middle of who you've been")


class NamingPicker(QWidget):
    """How downloaded files are named: a plain-language choice plus a live
    example. Typing a pattern ({date}_{custom} …) is tucked behind "Custom pattern…"."""

    templateChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        lay = QHBoxLayout()
        lay.setSpacing(8)
        outer.addLayout(lay)
        self.combo = QComboBox()
        for label, template in PRESETS:
            self.combo.addItem(label, template)
        self.combo.addItem("Custom pattern…", CUSTOM)
        tip(self.combo, "How downloaded video files are named. The example on the right shows the result.")
        lay.addWidget(self.combo)
        self.pattern = QLineEdit()
        self.pattern.setPlaceholderText("{date}_{custom}")
        self.pattern.setMinimumWidth(170)
        self.pattern.setAttribute(Qt.WA_MacShowFocusRect, False)
        tip(self.pattern, "For power users: build a name from pieces. "
                          + "  ".join(f"{{{k}}} = {v}" for k, v in TOKENS.items()))
        self.pattern.hide()
        lay.addWidget(self.pattern)
        self.example = QLabel()
        self.example.setProperty("faint", True)
        self.example.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.example.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.example.setMinimumWidth(120)
        self.example.setContentsMargins(2, 0, 0, 0)
        tip(self.example, "What a file will be called with this choice, using a highlighted video "
                          "(or a sample when none is highlighted).")
        lay.addStretch(1)
        outer.addWidget(self.example)
        self._item: VideoItem | None = None
        self._loading = False
        self.combo.currentIndexChanged.connect(self._on_choice)
        self.pattern.textEdited.connect(self._on_pattern)

    def template(self) -> str:
        data = self.combo.currentData()
        if data == CUSTOM:
            return self.pattern.text().strip() or DEFAULT_TEMPLATE
        return data or DEFAULT_TEMPLATE

    def set_template(self, template: str) -> None:
        """Select the matching preset, or show the pattern box for anything else."""
        template = (template or DEFAULT_TEMPLATE).strip()
        self._loading = True
        index = self.combo.findData(template)
        if index >= 0:
            self.combo.setCurrentIndex(index)
            self.pattern.hide()
        else:
            self.combo.setCurrentIndex(self.combo.findData(CUSTOM))
            self.pattern.setText(template)
            self.pattern.show()
        self._loading = False
        self._update_example()
        self.templateChanged.emit(self.template())

    def set_example_item(self, item: VideoItem | None) -> None:
        self._item = item
        self._update_example()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_example()

    def _on_choice(self) -> None:
        custom = self.combo.currentData() == CUSTOM
        self.pattern.setVisible(custom)
        if custom and not self.pattern.text():
            self.pattern.setText(PRESETS[0][1])
        if custom:
            self.pattern.setFocus()
        self._changed()

    def _on_pattern(self, _text: str) -> None:
        self._changed()

    def _changed(self) -> None:
        self._update_example()
        if not self._loading:
            self.templateChanged.emit(self.template())

    def _update_example(self) -> None:
        name = render_stem(self.template(), self._item or _sample_item(), 1) + ".mp4"
        metrics = QFontMetrics(self.example.font())
        width = max(self.width(), 320) - 80   # the whole row under the choice, minus "Example: "
        self.example.setText("Example: " + metrics.elidedText(name, Qt.ElideMiddle, width))
        self.example.setToolTip(f"Example file name: {name}")


class GrowingSearch(QPlainTextEdit):
    """A search box that shows everything you type: it grows to three lines,
    then scrolls. Return doesn't add a new line."""

    textChangedTo = Signal(str)

    def __init__(self, placeholder: str, parent=None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setTabChangesFocus(True)
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.document().documentLayout().documentSizeChanged.connect(lambda _s: self._fit())
        self.textChanged.connect(lambda: self.textChangedTo.emit(self.toPlainText()))
        self._fit()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            event.accept()
            return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source) -> None:  # noqa: N802 (pasted text stays on one logical line)
        self.insertPlainText(" ".join(source.text().split()))

    def text(self) -> str:
        return self.toPlainText()

    def _fit(self) -> None:
        line = QFontMetrics(self.font()).lineSpacing()
        content = int(self.document().size().height())
        lines = max(1, min(3, content))
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded if content > 3 else Qt.ScrollBarAlwaysOff)
        self.setFixedHeight(line * lines + 18)


class Collapsible(QWidget):
    """A section with a clickable header (arrow, optional logo, title, and a
    one-line summary on the right) that shows or hides its body. Whether it's
    open is remembered per `key`."""

    toggled = Signal(bool)

    def __init__(self, title: str, key: str = "", icon=None, expanded: bool = False, hint: str = "", parent=None):
        super().__init__(parent)
        from PySide6.QtCore import QSettings, QSize
        from PySide6.QtWidgets import QFrame, QToolButton, QVBoxLayout
        self._key = f"sections/{key}" if key else ""
        if self._key:
            stored = QSettings().value(self._key)
            if stored is not None:
                expanded = str(stored).lower() in ("true", "1")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        head = QFrame(objectName="SectionHeader")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 2, 0, 2)
        hl.setSpacing(6)
        self.button = QToolButton()
        self.button.setCheckable(True)
        self.button.setAutoRaise(True)
        self.button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.button.setProperty("section", True)
        self._title = title
        if icon is not None:
            self.button.setIcon(icon)
            self.button.setIconSize(QSize(16, 16))
        self.button.setToolTip(hint or f"Show or hide {title.lower()}")
        hl.addWidget(self.button)
        self.summary = QLabel()
        self.summary.setProperty("faint", True)
        self.summary.setToolTip(hint)
        self.summary.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)   # clips rather than widening
        self.summary.setMinimumWidth(40)
        hl.addWidget(self.summary, 1)
        outer.addWidget(head)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 4)
        self.body_layout.setSpacing(6)
        outer.addWidget(self.body)
        self.button.toggled.connect(self._toggle)
        self.button.setChecked(expanded)
        self._toggle(expanded, save=False)

    def _toggle(self, on: bool, save: bool = True) -> None:
        from PySide6.QtCore import QSettings
        self.body.setVisible(on)
        self.button.setText(self._title + ("  ▾" if on else "  ▸"))
        if save and self._key:
            QSettings().setValue(self._key, on)
        self.toggled.emit(on)

    def set_expanded(self, on: bool) -> None:
        self.button.setChecked(on)

    @property
    def expanded(self) -> bool:
        return self.button.isChecked()

    def add(self, widget: QWidget) -> QWidget:
        self.body_layout.addWidget(widget)
        return widget

    def set_summary(self, text: str, danger: bool = False) -> None:
        self.summary.setText(text)
        self.summary.setProperty("faint", not danger)
        self.summary.setProperty("danger", danger)
        self.summary.style().unpolish(self.summary)
        self.summary.style().polish(self.summary)
