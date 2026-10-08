"""Hover tips, with an app-wide off switch (Settings → Show helpful tips).

Every control gets a plain-language tooltip through `tip()`. When the user
turns tips off, one application-level event filter swallows all tooltip
events, so no widget needs to know about the setting.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QWidget

SETTING = "show_tips"


class _TipsFilter(QObject):
    enabled = True

    def eventFilter(self, _obj, event) -> bool:  # noqa: N802 (Qt API)
        return (not self.enabled) and event.type() == QEvent.ToolTip


_filter: _TipsFilter | None = None


def install(app: QApplication, store) -> None:
    """Install once at startup, reading the saved preference (default: on)."""
    global _filter
    if _filter is None:
        _filter = _TipsFilter(app)
        app.installEventFilter(_filter)
    _filter.enabled = store.get_setting(SETTING, "1") == "1"


def set_enabled(store, enabled: bool) -> None:
    store.set_setting(SETTING, "1" if enabled else "0")
    if _filter is not None:
        _filter.enabled = enabled


def enabled() -> bool:
    return _filter.enabled if _filter is not None else True


def tip(widget: QWidget, text: str) -> QWidget:
    """Attach a tooltip and return the widget, so it can wrap a constructor."""
    widget.setToolTip(text)
    return widget
