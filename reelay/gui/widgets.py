"""Small reusable widgets: the friendly file-naming picker and the growing search box."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics, QKeyEvent
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QSizePolicy, QWidget

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
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
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
        tip(self.example, "What a file will be called with this choice, using a highlighted video "
                          "(or a sample when none is highlighted).")
        lay.addWidget(self.example, 1)
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
        self.example.setText("Example: " + metrics.elidedText(name, Qt.ElideMiddle, 360))
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
        lines = max(1, min(3, int(self.document().size().height())))
        self.setFixedHeight(line * lines + 18)
