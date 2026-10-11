"""Database location and sharing it through a synced folder (Google Drive,
Dropbox, iCloud): one editor at a time, view only for everyone else."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reelay import dblocation
from reelay.paths import db_path
from reelay.store import ReadOnlyError, Store


@pytest.fixture
def drive(tmp_path):
    folder = tmp_path / "Library" / "CloudStorage" / "GoogleDrive-lydia" / "My Drive" / "Reelay"
    folder.mkdir(parents=True)
    return folder


def test_synced_folders_are_recognised(tmp_path):
    assert dblocation.synced_service("/Users/l/Library/Mobile Documents/com~apple~CloudDocs/x") == "iCloud Drive"
    assert dblocation.synced_service("/Users/l/Dropbox/Team") == "Dropbox"
    assert dblocation.synced_service("/Users/l/Library/CloudStorage/GoogleDrive-l/My Drive") == "Google Drive"
    assert dblocation.synced_service("/Users/l/Documents/Reelay") is None
    assert dblocation.journal_mode("/Users/l/Dropbox/reelay.db") == "DELETE"


def test_move_and_point_to_a_shared_folder(drive):
    store = Store(db_path())
    store.create_profile("Lydia", drive)
    dest = store.move_to(drive)
    assert dest == drive / "reelay.db" and db_path() == dest
    copy = Store(dest)
    assert [p.name for p in copy.list_profiles()] == ["Lydia"]
    assert copy.conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"   # one file: safe to sync
    copy.close()
    with pytest.raises(dblocation.LocationError, match="already a Reelay database"):
        store.move_to(drive)
    store.close()
    dblocation.set_location(None)
    assert db_path() == dblocation.default_path()
    assert dblocation.open_existing(drive) == dest and db_path() == dest
    (drive / "notes.db").write_bytes(b"not sqlite")
    with pytest.raises(dblocation.LocationError):
        dblocation.open_existing(drive / "notes.db")


def test_missing_drive_is_reported_not_replaced(tmp_path):
    dblocation.set_location(tmp_path / "Dropbox" / "gone" / "reelay.db")
    assert "isn't available" in dblocation.problem() and "Dropbox" in dblocation.problem()


def test_lease_one_editor_at_a_time(drive):
    path = drive / "reelay.db"
    lock = path.with_name("reelay.db.reelay-lock")
    now = [datetime.now(timezone.utc)]
    a = dblocation.Lease(path, "laptop", clock=lambda: now[0])
    b = dblocation.Lease(path, "desktop", clock=lambda: now[0])
    assert a.acquire() is None and a.held
    held = b.acquire()
    assert held is not None and held.instance == "laptop" and not b.held and "last active just now" in held.describe()
    assert b.acquire(force=True) is None and b.held      # Take Over
    assert not a.beat() and not a.held                   # the laptop learns it lost the lock and stops editing
    a.release()
    assert lock.exists()                                 # not the laptop's to remove
    now[0] += timedelta(minutes=10)                      # the desktop crashed: its heartbeat stopped
    assert a.other() is None and a.acquire() is None and a.held   # an abandoned lock expires
    a.release()
    assert not lock.exists()


def test_open_database_asks_and_respects_the_answer(drive):
    from reelay.gui.shared_db import open_database
    path = drive / "reelay.db"
    Store(path).close()
    dblocation.set_location(path)
    other = dblocation.Lease(path, "teammate")
    other.acquire()
    questions = []

    def answer(n):
        def ask(title, text, buttons, default=0):
            questions.append((title, buttons))
            return n
        return ask
    assert open_database(answer(2)) is None
    store, lease = open_database(answer(0))
    assert store.read_only and "is editing it" in store.read_only_reason and not lease.held
    assert questions[-1] == ("Someone Else Is Editing", ["Open View Only", "Take Over", "Quit"])
    with pytest.raises(ReadOnlyError, match="view only"):
        store.create_profile("Nope", drive)
    store.set_setting("theme", "light")                  # preferences quietly aren't saved
    store.close()
    store, lease = open_database(answer(1))              # take over
    assert not store.read_only and lease.held and not other.beat()
    store.close()
    lease.release()
    store, lease = open_database(answer(0))              # free now: no question asked
    assert not store.read_only and lease.held
    store.close()
    lease.release()


def test_open_database_when_the_drive_is_missing(tmp_path):
    from reelay.gui.shared_db import open_database
    dblocation.set_location(tmp_path / "Dropbox" / "gone" / "reelay.db")
    store, lease = open_database(lambda title, text, buttons, default=0: 1)   # use this computer's
    assert store.path == str(dblocation.default_path())
    store.close()
    lease.release()


def test_view_only_window_follows_the_synced_file(qtbot, drive):
    from PySide6.QtWidgets import QApplication

    from reelay.gui.app import create_window
    from reelay.gui.shared_db import SharedSession
    from reelay.tokens import MemoryTokenStore
    path = drive / "reelay.db"
    editor = Store(path)
    profile = editor.create_profile("Lydia", drive / "lib")
    editor.set_active_profile(profile.id)
    lease_editor = dblocation.Lease(path, "editor")
    lease_editor.acquire()
    viewer = Store(path, read_only=True)
    viewer.read_only_reason = "lydia is editing it"
    window = create_window(QApplication.instance(), viewer, MemoryTokenStore())
    qtbot.addWidget(window)
    session = SharedSession(window, viewer, dblocation.Lease(path, "viewer"))
    assert window.banner.isVisibleTo(window) and "View only" in window.banner_text.text()
    assert window.banner_btn.isHidden()
    editor.create_profile("Blenders", drive / "lib2")    # the teammate's change arrives through sync
    session._reopen()
    assert {p.name for p in viewer.list_profiles()} == {"Lydia", "Blenders"}
    lease_editor.release()
    session.tick()
    assert not window.banner_btn.isHidden() and window.banner_btn.text() == "Edit Now"
    window.settings_page.refresh()
    assert "View only" in window.settings_page.db_status.text() and "Google Drive" in \
        window.settings_page.db_status.text()
    assert not window.settings_page.move_db_btn.isEnabled()
    session.stop()
    window.close()
    viewer.close()
    editor.close()
