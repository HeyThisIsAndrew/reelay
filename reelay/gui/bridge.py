"""Deliver Store change events on the GUI thread.

The Store notifies listeners on whichever thread wrote (the GUI, the agent
API server, the dispatcher). Widgets must only be touched on the GUI thread,
so pages connect to this bridge's Qt signal instead of subscribing directly;
Qt queues cross-thread emissions onto the receiver's thread.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ..store import Store


class ChangeBridge(QObject):
    changed = Signal(object)   # reelay.store.Change


def change_bridge(store: Store) -> ChangeBridge:
    """The bridge for `store` (created on first use, which must be on the GUI thread)."""
    bridge = getattr(store, "_qt_bridge", None)
    if bridge is None:
        bridge = ChangeBridge()
        store._qt_bridge = bridge
        store.subscribe(bridge.changed.emit)
    return bridge
