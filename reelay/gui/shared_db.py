"""Opening the database at start-up, and sharing it through a synced folder.

See dblocation.py for the rules. This is the Qt side: the questions asked at
start-up, the 30-second heartbeat while editing, and, when someone else is
editing, view-only mode that reloads as the synced file changes and offers to
switch to editing once they're done.
"""

from __future__ import annotations

import logging
import sys
import uuid

from PySide6.QtCore import QFileSystemWatcher, QObject, QProcess, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from .. import APP_NAME, dblocation
from ..store import ReadOnlyError, Store

log = logging.getLogger("reelay")


def _ask(title: str, text: str, buttons: list[str], default: int = 0) -> int:
    box = QMessageBox(QMessageBox.Question, title, text)
    made = [box.addButton(b, QMessageBox.AcceptRole if i == default else QMessageBox.ActionRole)
            for i, b in enumerate(buttons)]
    box.setDefaultButton(made[default])
    box.exec()
    return made.index(box.clickedButton()) if box.clickedButton() in made else len(buttons) - 1


def open_database(ask=_ask) -> tuple[Store, dblocation.Lease | None] | None:
    """Open the database for editing, or view only if someone else is editing it.
    Returns None if the person chose to quit."""
    while dblocation.problem():
        choice = ask(APP_NAME, dblocation.problem() + "\n\nReelay won't start a new, empty database there by itself.",
                     ["Try Again", "Use This Computer's Database", "Quit"])
        if choice == 1:
            dblocation.set_location(None)
        elif choice == 2:
            return None
    path = dblocation.current_path()
    lease = dblocation.Lease(path, uuid.uuid4().hex)
    other = lease.acquire()
    if other is not None:
        choice = ask("Someone Else Is Editing",
                     f"{other.describe()} has this Reelay database open for editing.\n\nYou can open it view only "
                     "(you'll see their changes as they sync, and nothing is posted from this computer), or take "
                     "over if they're not really using it, e.g. they forgot to quit. Taking over while they're "
                     "still working can lose their latest changes.",
                     ["Open View Only", "Take Over", "Quit"])
        if choice == 2:
            return None
        if choice == 0:
            try:
                store = Store(path, read_only=True)
            except ReadOnlyError as exc:
                ask(APP_NAME, str(exc), ["Quit"])
                return None
            store.read_only_reason = f"{other.holder} is editing it"
            return store, lease
        lease.acquire(force=True)
        log.warning("Took over the database from %s.", other.describe())
    return Store(path), lease


class SharedSession(QObject):
    """Keeps the lock alive while editing; in view-only mode, follows the synced file."""

    def __init__(self, window, store: Store, lease: dblocation.Lease, parent=None):
        super().__init__(parent or window)
        self.window, self.store, self.lease = window, store, lease
        self.lost = False
        self._reload = QTimer(self, singleShot=True, interval=1500, timeout=self._reopen)
        self.watcher = QFileSystemWatcher(self)
        self.timer = QTimer(self, interval=dblocation.HEARTBEAT_SECONDS * 1000, timeout=self.tick)
        if store.read_only:
            for p in (str(lease.db_path), str(lease.db_path.parent)):
                self.watcher.addPath(p)
            self.watcher.fileChanged.connect(lambda _p: self._reload.start())
            self.watcher.directoryChanged.connect(lambda _p: self._reload.start())
            self._show_view_only()
        elif dblocation.synced_service(lease.db_path):
            window.set_view_only("")
        self.timer.start()

    # ------------------------------------------------------------------ editing
    def tick(self) -> None:
        if self.store.read_only:
            self._show_view_only()
            return
        if not self.lease.beat() and not self.lost:
            self.lost = True
            other = self.lease.read()
            who = other.holder if other else "Someone else"
            log.warning("Lost the database lock to %s.", who)
            self.window.services.stop()            # stop posting at once: only one computer may send
            QMessageBox.warning(self.window, "Editing Moved",
                                f"{who} took over editing this shared database. Reelay will close on this "
                                "computer so you don't overwrite each other's work. Open it again to view it.")
            QApplication.instance().quit()

    # ------------------------------------------------------------------ view only
    def _show_view_only(self) -> None:
        other = self.lease.other()
        if other is not None:
            self.window.set_view_only(f"<b>View only.</b> {other.describe()} is editing this shared database. "
                                      "You'll see their changes as they sync. Nothing is posted from this "
                                      "computer.")
        else:
            self.window.set_view_only("<b>View only.</b> No one is editing this database now.", "Edit Now",
                                      self.switch_to_editing)

    def _reopen(self) -> None:
        for p in (str(self.lease.db_path),):      # a replaced file drops off the watch list
            if p not in self.watcher.files() and self.lease.db_path.exists():
                self.watcher.addPath(p)
        try:
            self.store.reopen()
        except Exception:  # noqa: BLE001 — mid-sync; try again on the next change
            log.warning("Couldn't reload the shared database yet", exc_info=True)

    def switch_to_editing(self) -> None:
        """Relaunch Reelay, which takes the lock now that it's free."""
        if self.lease.other() is not None:
            self._show_view_only()
            return
        args = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
        QProcess.startDetached(sys.executable, args)
        QApplication.instance().quit()

    def stop(self) -> None:
        self.timer.stop()
        self.lease.release()
