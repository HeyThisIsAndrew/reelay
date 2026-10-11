"""The contextual inspector: a right-hand panel that slides in when something is selected
and gets out of the way when nothing is, so the library or board can use the whole window."""

from __future__ import annotations

import os

from PySide6.QtCore import QEasingCurve, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSplitter, QToolButton, QVBoxLayout, QWidget

from .tips import tip

# 0 turns the slide off (tests, or REELAY_REDUCE_MOTION=1 for anyone who prefers no motion).
ANIMATION_MS = 0 if os.environ.get("REELAY_REDUCE_MOTION") == "1" else 180


class Inspector(QFrame):
    """The panel itself: a slim header (title + close) above whatever it shows."""

    closeRequested = Signal()

    def __init__(self, content: QWidget, title: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Inspector")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        header = QWidget(objectName="InspectorHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(14, 6, 6, 6)
        self.title = QLabel(title.upper(), objectName="InspectorTitle")
        close = QToolButton(objectName="InspectorClose")
        close.setText("✕")
        close.setCursor(Qt.PointingHandCursor)
        tip(close, "Close this panel (Esc)")
        close.clicked.connect(self.closeRequested.emit)
        self.close_btn = close
        hl.addWidget(self.title, 1)
        hl.addWidget(close)
        lay.addWidget(header)
        lay.addWidget(content, 1)
        self.content = content

    def set_title(self, text: str) -> None:
        self.title.setText(text.upper())


class _Slide(QWidget):
    """Holds the inspector at its full width and clips it while the splitter opens or
    closes, so it slides in instead of squashing its contents. Reports no minimum size of
    its own, which is what lets the splitter animate it from zero."""

    def __init__(self, child: QWidget, parent=None):
        super().__init__(parent)
        self.child = child
        child.setParent(self)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, 0)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.child.sizeHint()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.child.setGeometry(0, 0, max(self.width(), self.child.minimumSizeHint().width()), self.height())


class ContextualSplitter(QSplitter):
    """Main area on the left, an inspector on the right that's only there when it has something to show.

    ``reveal()`` slides it in at the width the person last chose (remembered per page), ``dismiss()``
    slides it out and hides it. Dragging the gap resizes it while it's open."""

    MAIN_MIN_WIDTH = 320          # the library / board never gets narrower than one readable column
    MAX_WIDTH, MAX_SHARE = 600, 0.55   # the inspector never covers more than this

    def __init__(self, main: QWidget, inspector: Inspector, store=None, key: str = "", default_width: int = 400,
                 min_width: int = 320, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.store, self.key = store, key
        self.min_width = min_width
        main.setMinimumWidth(self.MAIN_MIN_WIDTH)
        self.inspector = inspector
        self.slide = _Slide(inspector)
        self.addWidget(main)
        self.addWidget(self.slide)
        self.setStretchFactor(0, 1)
        self.setStretchFactor(1, 0)
        self.setChildrenCollapsible(False)
        self.width_wanted = default_width
        if store is not None and key:
            try:
                self.width_wanted = max(min_width, int(store.get_setting(self._setting, "") or default_width))
            except ValueError:
                pass
        self.is_open = False
        self.slide.hide()
        self._anim = QVariantAnimation(self)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._step)
        self._anim.finished.connect(self._settle)
        self._save_timer = QTimer(self, singleShot=True, interval=400, timeout=self._save_width)
        self.splitterMoved.connect(self._moved)

    @property
    def _setting(self) -> str:
        return f"ui.inspector_width.{self.key}"

    # ------------------------------------------------------------ open / close
    def reveal(self) -> None:
        if self.is_open:
            return
        self.is_open = True
        self.slide.setMinimumWidth(0)
        self.slide.setMaximumWidth(16777215)
        self.slide.show()
        self._run(self._current(), self._target())

    def dismiss(self) -> None:
        if not self.is_open:
            return
        self.is_open = False
        self.slide.setMinimumWidth(0)
        self._run(self._current(), 0)

    def max_width(self) -> int:
        total = sum(self.sizes()) or self.width()
        return max(self.min_width, min(self.MAX_WIDTH, int(total * self.MAX_SHARE), total - self.MAIN_MIN_WIDTH))

    def _target(self) -> int:
        return max(self.min_width, min(self.width_wanted, self.max_width()))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.is_open and self._anim.state() != QVariantAnimation.Running:
            self.slide.setMaximumWidth(self.max_width())

    def _current(self) -> int:
        return self.sizes()[1] if self.slide.isVisible() else 0

    def _run(self, start: int, end: int) -> None:
        self._anim.stop()
        if ANIMATION_MS <= 0 or not self.isVisible():
            self._step(end)
            self._settle()
            return
        self._anim.setDuration(ANIMATION_MS)
        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        self._anim.start()

    def _step(self, value) -> None:
        total = sum(self.sizes()) or self.width()
        self.setSizes([max(total - int(value), 0), int(value)])

    def _settle(self) -> None:
        if self.is_open:
            self.slide.setMinimumWidth(self.min_width)   # dragging can't squash it once it's open…
            self.slide.setMaximumWidth(self.max_width())  # …or stretch it across the library
            self._step(self._target())
        else:
            self.slide.hide()

    # ------------------------------------------------------------ remembering the width
    def _moved(self, *_args) -> None:
        if self.is_open and self._anim.state() != QVariantAnimation.Running:
            self.width_wanted = self.sizes()[1]
            self._save_timer.start()

    def _save_width(self) -> None:
        if self.store is not None and self.key:
            self.store.set_setting(self._setting, str(self.width_wanted))


class MediaPane(QWidget):
    """The player on top (16:9, never more than about half the panel), the rest below it.
    The player stays put while the content under it scrolls."""

    def __init__(self, player: QWidget, content: QWidget, parent=None):
        super().__init__(parent)
        self.player, self.content = player, content
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(player)
        lay.addWidget(content, 1)

    def set_player_visible(self, on: bool) -> None:
        self.player.setVisible(on)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        stage = getattr(self.player, "stage", None)
        if stage is not None:
            stage.setFixedHeight(int(min(self.width() * 9 / 16, self.height() * 0.48)))
