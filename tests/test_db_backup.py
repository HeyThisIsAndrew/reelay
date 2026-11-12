"""scripts/build_local.sh backs up the database before installing (reelay.dblocation.backup_to)."""

from __future__ import annotations

import os
import sqlite3
import stat

from reelay.dblocation import backup_to
from reelay.store import Store


def test_backup_of_a_cleanly_closed_wal_database(tmp_path):
    """After Reelay quits there is no -wal or -shm file; a read-only open of a WAL database then fails with
    "unable to open database file". The backup must still work, and include everything."""
    db = tmp_path / "reelay.db"
    store = Store(db)
    store.create_profile("Hey This Is Andrew", tmp_path / "lib")
    store.close()
    check = sqlite3.connect(db)
    assert check.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    check.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    check.close()
    # The state after Reelay quits: the database file alone, no -wal and no -shm beside it.
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "reelay.db").write_bytes(db.read_bytes())
    db = clean / "reelay.db"

    out = backup_to(db, tmp_path / "backup.db")

    copy = sqlite3.connect(out)
    assert copy.execute("SELECT name FROM profiles").fetchall() == [("Hey This Is Andrew",)]
    assert copy.execute("PRAGMA user_version").fetchone()[0] > 0
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600


def test_build_local_uses_the_tested_backup():
    from pathlib import Path
    script = (Path(__file__).resolve().parent.parent / "scripts" / "build_local.sh").read_text()
    assert "from reelay.dblocation import backup_to" in script and "mode=ro" not in script
