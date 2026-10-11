"""Where Reelay keeps its database, caches and logs.

Set REELAY_HOME to put everything in one folder (used by the tests, and
handy for a portable install).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_ID, APP_NAME


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
