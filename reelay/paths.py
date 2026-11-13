"""Where Reelay keeps its database, caches and logs.

Set REELAY_HOME to put everything in one folder (used by the tests, and
handy for a portable install).
"""

from __future__ import annotations

import logging
import os
import re
import stat
import sys
from pathlib import Path

from . import APP_ID, APP_NAME


log = logging.getLogger("reelay")
_secured: set[str] = set()
_POSIX = os.name == "posix"


def make_private(path: Path | str, directory: bool = False) -> bool:
    """Owner-only permissions (folder 0700, file 0600) for Reelay's own data, on macOS and Linux.

    Left alone (returns False): Windows (its per-user folders are already private, and it has no such
    modes), anything inside a synced folder (Google Drive, Dropbox… manage sharing themselves), files
    owned by someone else, and anything that doesn't exist. Never raises: a failure is only logged."""
    if not _POSIX:
        return False
    path = Path(path)
    try:
        from .dblocation import synced_service
        if synced_service(path):
            return False
        st = os.stat(path)
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            return False
        if stat.S_IMODE(st.st_mode) & 0o077:
            os.chmod(path, 0o700 if directory else 0o600)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("Couldn't make %s private: %s", path, exc)
        return False


def _home_override() -> Path | None:
    value = os.environ.get("REELAY_HOME")
    return Path(value) if value else None


def data_dir() -> Path:
    override = _home_override()
    if override:
        path = override
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / APP_ID
    elif sys.platform == "win32":
        path = Path(os.environ.get("APPDATA", Path.home())) / APP_ID
    else:
        path = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_ID.lower()
    path.mkdir(parents=True, exist_ok=True)
    if str(path) not in _secured:             # database, caches, logs and backups: only this user
        make_private(path, directory=True)
        _secured.add(str(path))
    return path


def db_path() -> Path:
    """The database: the default in the data folder, or the location chosen in Settings
    (e.g. a shared Google Drive folder; see dblocation.py)."""
    from .dblocation import current_path
    return current_path()


def cache_dir() -> Path:
    path = data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cover_cache_file(folder: Path, key: str) -> Path:
    """Where a video's cover image is cached (gui/thumbnails.py), by the video's key."""
    return Path(folder) / (re.sub(r"[^\w.-]", "_", key) + ".img")


def log_dir() -> Path:
    override = _home_override()
    if override:
        path = override / "logs"
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Logs" / APP_ID
    elif sys.platform == "win32":
        path = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_ID / "logs"
    else:
        path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_library_root() -> Path:
    """~/Movies/Reelay on macOS, ~/Videos/Reelay elsewhere (inside REELAY_HOME when that's set, so tests
    and throwaway runs never write into the real Movies folder)."""
    override = _home_override()
    if override is not None:
        return override / "Videos"
    movies = Path.home() / ("Movies" if sys.platform == "darwin" else "Videos")
    return (movies if movies.is_dir() else Path.home()) / APP_NAME
