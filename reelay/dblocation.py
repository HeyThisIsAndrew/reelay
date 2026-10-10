"""Where the database lives, and sharing it through a synced folder.

A small team can share one Reelay database by keeping it in Google Drive,
Dropbox, iCloud Drive or OneDrive. SQLite is safe there only if two computers
never write at the same time, so:

* **One editor at a time.** Whoever opens the database first holds a lock file
  next to it (``reelay.db.reelay-lock``: who, which computer, and a heartbeat
  refreshed every 30 seconds). Anyone else opens it **view only**: they see
  everything (it reloads as the synced file changes) but can't edit, and their
  Reelay never posts anything, so two computers can never send the same video.
  A lock whose heartbeat stopped (a crash, a closed laptop) expires after a few
  minutes, and "Take Over" is there for when someone forgot to quit.
* **One file.** In a synced folder Reelay uses SQLite's classic single-file
  journal instead of WAL, whose side files (-wal, -shm) would sync separately
  and could corrupt the database.

The chosen location is remembered in a pointer file in Reelay's own data folder
(the database can't record where it is itself).
"""

from __future__ import annotations

import getpass
import json
import os
import socket
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .paths import data_dir

POINTER_NAME = "database-location.json"
DB_NAME = "reelay.db"
LOCK_SUFFIX = ".reelay-lock"
HEARTBEAT_SECONDS = 30
LOCK_EXPIRES_SECONDS = 180       # sync can lag; a lock silent this long is abandoned
SYNC_MARKERS = (                 # path fragment → service name
    ("Mobile Documents", "iCloud Drive"), ("iCloud Drive", "iCloud Drive"), ("iCloudDrive", "iCloud Drive"),
    ("Google Drive", "Google Drive"), ("GoogleDrive", "Google Drive"), ("My Drive", "Google Drive"),
    ("Dropbox", "Dropbox"), ("OneDrive", "OneDrive"), ("Box Sync", "Box"), ("CloudStorage", "a cloud drive"),
)


class LocationError(Exception):
    """Plain-language reason the database can't be used there."""


# ---------------------------------------------------------------------- location
def pointer_path() -> Path:
    return data_dir() / POINTER_NAME


def default_path() -> Path:
    return data_dir() / DB_NAME


def configured_path() -> Path | None:
    """The location chosen in Settings, or None for the default."""
    try:
        data = json.loads(pointer_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    path = data.get("path") if isinstance(data, dict) else None
    return Path(path) if path else None


def current_path() -> Path:
    return configured_path() or default_path()


def problem() -> str | None:
    """Why the chosen location can't be opened right now (e.g. the drive isn't mounted)."""
    path = configured_path()
    if path is None:
        return None
    if not path.parent.is_dir():
        service = synced_service(path)
        return (f"Reelay's database is set to {path}, but that folder isn't available"
                + (f". Is {service} running and signed in?" if service else ".") )
    return None


def set_location(path: Path | str | None) -> None:
    """Remember where the database is (None = the default)."""
    if path is None or Path(path) == default_path():
        pointer_path().unlink(missing_ok=True)
        return
    tmp = pointer_path().with_suffix(".tmp")
    tmp.write_text(json.dumps({"path": str(Path(path))}), encoding="utf-8")
    tmp.replace(pointer_path())


def synced_service(path: Path | str) -> str | None:
    """The sync service a path is inside, if any."""
    text = str(Path(path).expanduser())
    for marker, name in SYNC_MARKERS:
        if f"/{marker}" in text or f"\\{marker}" in text:
            return name
    return None


def journal_mode(path: Path | str) -> str:
    return "DELETE" if synced_service(path) else "WAL"


# ---------------------------------------------------------------------- moving
def move_database(conn: sqlite3.Connection, folder: Path | str) -> Path:
    """Copy the open database into `folder` (consistent snapshot via SQLite's backup API),
    check the copy, and make it the database Reelay opens next time. The old file is kept."""
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise LocationError("Pick a folder that exists.")
    dest = folder / DB_NAME
    if dest.exists():
        raise LocationError(f"There's already a Reelay database in {folder}. Use “Open Shared Database…” to use "
                            "it, or pick another folder.")
    if dest.resolve() == current_path().resolve():
        raise LocationError("That's where the database already is.")
    part = dest.with_name(DB_NAME + ".part")
    part.unlink(missing_ok=True)
    try:
        target = sqlite3.connect(str(part))
        try:
            conn.backup(target)
            target.execute(f"PRAGMA journal_mode = {journal_mode(dest)}")
            ok = target.execute("PRAGMA integrity_check").fetchone()[0]
            version = target.execute("PRAGMA user_version").fetchone()[0]
            same = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            target.close()
        if ok != "ok" or version != same:
            raise LocationError("The copy didn't check out, so nothing was changed.")
        part.replace(dest)
    except (OSError, sqlite3.Error) as exc:
        part.unlink(missing_ok=True)
        raise LocationError(f"Couldn't copy the database there ({exc}). Nothing was changed.") from None
    set_location(dest)
    return dest


def open_existing(path: Path | str) -> Path:
    """Use a Reelay database that's already in a shared folder (e.g. a teammate's)."""
    path = Path(path).expanduser()
    if path.is_dir():
        path = path / DB_NAME
    if not path.is_file():
        raise LocationError("There's no Reelay database there.")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
    except sqlite3.Error:
        raise LocationError("That file isn't a Reelay database.") from None
    if not {"profiles", "videos", "settings"} <= tables:
        raise LocationError("That file isn't a Reelay database.")
    set_location(path)
    return path


# ---------------------------------------------------------------------- one editor at a time
@dataclass
class LockInfo:
    holder: str          # "lydia on Lydia's MacBook Air"
    host: str
    instance: str
    heartbeat: datetime

    @property
    def age(self) -> timedelta:
        return datetime.now(timezone.utc) - self.heartbeat

    @property
    def fresh(self) -> bool:
        return self.fresh_at(datetime.now(timezone.utc))

    def fresh_at(self, now: datetime) -> bool:
        return now - self.heartbeat < timedelta(seconds=LOCK_EXPIRES_SECONDS)

    def describe(self) -> str:
        secs = max(0, int(self.age.total_seconds()))
        ago = "just now" if secs < 60 else f"{secs // 60} min ago"
        return f"{self.holder} (last active {ago})"


def _me() -> tuple[str, str]:
    host = socket.gethostname().removesuffix(".local")
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "someone"
    return f"{user} on {host}", host


class Lease:
    """The editing lock for one database file."""

    def __init__(self, db_path: Path | str, instance: str, clock=lambda: datetime.now(timezone.utc)):
        self.db_path = Path(db_path)
        self.path = self.db_path.with_name(self.db_path.name + LOCK_SUFFIX)
        self.instance = instance
        self.clock = clock
        self.held = False

    def read(self) -> LockInfo | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return LockInfo(data["holder"], data.get("host", ""), data["instance"],
                            datetime.fromisoformat(data["heartbeat"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def other(self) -> LockInfo | None:
        """Someone else editing right now, or None."""
        info = self.read()
        if info is None or info.instance == self.instance or not info.fresh_at(self.clock()):
            return None
        return info

    def acquire(self, force: bool = False) -> LockInfo | None:
        """Take the lock. Returns who holds it instead (and takes nothing) unless it's free or `force`."""
        other = self.other()
        if other is not None and not force:
            return None if self.held else other
        self._write()
        self.held = True
        return None

    def beat(self) -> bool:
        """Refresh the heartbeat. False if someone took over (this computer should stop editing)."""
        if not self.held:
            return False
        info = self.read()
        if info is not None and info.instance != self.instance and info.fresh_at(self.clock()):
            self.held = False
            return False
        self._write()
        return True

    def release(self) -> None:
        if self.held:
            info = self.read()
            if info is None or info.instance == self.instance:
                self.path.unlink(missing_ok=True)
            self.held = False

    def _write(self) -> None:
        holder, host = _me()
        data = {"holder": holder, "host": host, "instance": self.instance, "pid": os.getpid(),
                "heartbeat": self.clock().isoformat()}
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            tmp.unlink(missing_ok=True)
