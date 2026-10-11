"""Shared app chrome: sidebar nav buttons, the up-next reminder, the Filter & Sort popover and the
grid / list toggle. Everything reads its colours from the theme, so the active profile's brand accent
marks exactly what's active."""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton, QVBoxLayout,
                               QWidget)

from . import icons, theme
from .theme import set_prop
from .tips import tip


class NavButton(QPushButton):
    """A sidebar page button: icon + label, with an accent marker on the current page.
    In the collapsed sidebar only the icon shows (the label becomes the tooltip)."""

    def __init__(self, label: str, icon_name: str, parent=None):
        super().__init__(label, parent)
        self.label, self.icon_name = label, icon_name
        set_prop(self, "nav")
        self.setCheckable(True)
        self.setIconSize(icons.size(18))
        self.setCursor(Qt.PointingHandCursor)
        self.retint()

    def retint(self) -> None:
        self.setIcon(icons.icon(self.icon_name, 18))

    def set_compact(self, compact: bool) -> None:
        self.setText("" if compact else self.label)
        set_prop(self, "compact", compact)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self.isChecked():
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            path = QPainterPath()
            h = self.height() * 0.5
            path.addRoundedRect(QRectF(1, (self.height() - h) / 2, 3, h), 1.5, 1.5)
            p.fillPath(path, QColor(theme.P.accent_color))
            p.end()


class UpNext(QFrame):
    """The next post going out for this profile ("Next: TikTok · in 2h 14m"), at the foot of the sidebar."""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("UpNext")
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(1)
        self.heading = QLabel("UP NEXT", objectName="UpNextHeading")
        self.text = QLabel(objectName="UpNextText")
        self.text.setWordWrap(True)
        lay.addWidget(self.heading)
        lay.addWidget(self.text)
        tip(self, "The next post Reelay sends or a platform publishes for this profile. Click to open the Board.")
        self.setVisible(False)
        self._when: datetime | None = None
        self._what = ""

    def set_next(self, what: str, when: datetime | None) -> None:
        self._what, self._when = what, when
        self.refresh()

    def refresh(self, now: datetime | None = None) -> None:
        if self._when is None:
            self.setVisible(False)
            return
        now = now or datetime.now(timezone.utc)
        self.text.setText(f"{self._what} · {countdown(self._when, now)}")
        self.setVisible(True)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit()
        super().mousePressEvent(event)


def countdown(when: datetime, now: datetime) -> str:
    seconds = int((when - now).total_seconds())
    if seconds <= 60:
        return "any moment"
    minutes = seconds // 60
    if minutes < 60:
        return f"in {minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"in {hours}h {minutes:02d}m"
    local = when.astimezone()
    days = (local.date() - now.astimezone().date()).days
    day = "tomorrow" if days == 1 else f"{local:%a %b} {local.day}"
    clock = f"{local:%I:%M}".lstrip("0") + ("am" if local.hour < 12 else "pm")     # 9:00am
    return f"{day} {clock}"


class Popover(QWidget):
    """A floating panel under its button (closes when you click elsewhere). The window itself is
    transparent; the rounded panel inside it carries the theme's colours."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)   # rounded corners
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        panel = QFrame(objectName="Popover")
        outer.addWidget(panel)
        self.body = QVBoxLayout(panel)
        self.body.setContentsMargins(16, 14, 16, 14)
        self.body.setSpacing(10)

    def add_section(self, title: str, hint: str, *widgets: QWidget) -> None:
        label = QLabel(title.upper())
        label.setProperty("section", True)
        tip(label, hint)
        self.body.addWidget(label)
        for w in widgets:
            self.body.addWidget(w)

    def show_under(self, anchor: QWidget) -> None:
        self.adjustSize()
        pos = anchor.mapToGlobal(QPoint(anchor.width() - self.sizeHint().width(), anchor.height() + 6))
        screen = anchor.screen().availableGeometry() if anchor.screen() else None
        if screen is not None:
            pos.setX(max(screen.left() + 8, min(pos.x(), screen.right() - self.sizeHint().width() - 8)))
        self.move(pos)
        self.show()


class FilterButton(QToolButton):
    """"Filter" with a count of active filters; opens its popover."""

    def __init__(self, popover: Popover, parent=None):
        super().__init__(parent)
        self.popover = popover
        self.setObjectName("FilterButton")
        self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.setIconSize(icons.size(16))
        self.setCursor(Qt.PointingHandCursor)
        self.clicked.connect(lambda: self.popover.show_under(self))
        self.set_count(0)
        tip(self, "Filter and sort: hashtags, brands, products and readiness")

    def retint(self) -> None:
        self.setIcon(icons.icon("filter", 16))

    def set_count(self, n: int) -> None:
        self.retint()
        self.setText(f"Filter · {n}" if n else "Filter")
        set_prop(self, "active", bool(n))


class ViewToggle(QWidget):
    """Grid / List, remembered per page."""

    changed = Signal(str)

    def __init__(self, store=None, key: str = "", parent=None):
        super().__init__(parent)
        self.store, self.key = store, key
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.group = QButtonGroup(self)
        self.buttons: dict[str, QToolButton] = {}
        for n, (mode, icon_name, label) in enumerate((("grid", "grid", "Grid"), ("list", "list", "List"))):
            b = QToolButton()
            b.setCheckable(True)
            b.setIconSize(icons.size(16))
            b.setProperty("segment", "first" if n == 0 else "last")
            b.setObjectName("ViewToggle")
            tip(b, f"{label} view")
            b.clicked.connect(lambda _=False, m=mode: self.set_mode(m, emit=True))
            self.group.addButton(b)
            self.buttons[mode] = b
            lay.addWidget(b)
        saved = store.get_setting(f"ui.view.{key}", "grid") if store is not None and key else "grid"
        self.mode = saved if saved in self.buttons else "grid"
        self.buttons[self.mode].setChecked(True)
        self.retint()

    def retint(self) -> None:
        for mode, b in self.buttons.items():
            b.setIcon(icons.icon("grid" if mode == "grid" else "list", 16, active=theme.P.on_accent))

    def set_mode(self, mode: str, emit: bool = False) -> None:
        self.mode = mode
        self.buttons[mode].setChecked(True)
        if self.store is not None and self.key:
            self.store.set_setting(f"ui.view.{self.key}", mode)
        if emit:
            self.changed.emit(mode)


def icon_button(icon_name: str, tooltip: str, size: int = 16) -> QToolButton:
    b = QToolButton()
    b.setObjectName("IconButton")
    b.setIcon(icons.icon(icon_name, size))
    b.setIconSize(QSize(size, size))
    b.setCursor(Qt.PointingHandCursor)
    b.setProperty("icon_name", icon_name)
    tip(b, tooltip)
    return b


def retint_icon_buttons(root: QWidget) -> None:
    """After a theme change: re-tint every icon button under ``root``."""
    for b in root.findChildren(QToolButton, "IconButton"):
        name = b.property("icon_name")
        if name:
            b.setIcon(icons.icon(name, b.iconSize().width()))
    for kind in (NavButton, FilterButton, ViewToggle):
        for w in root.findChildren(kind):
            w.retint()
