"""Which folders this Mac has agreed Reelay may post and serve videos from, kept OUTSIDE the database.

The database says where each profile's videos live, but a database can come from elsewhere (a shared Google
Drive copy someone else also edits, a restored backup). If it alone decided, an edited database could point a
profile at any folder and have a private video there uploaded or served through a public link. So Reelay only
posts from folders this computer trusts, recorded in ``trusted_folders.json`` in the data folder (private to
this user, never synced):

* a folder chosen in the app (a new profile's folder, Download Settings → Where videos go);
* every folder a database names the FIRST time this computer opens that database file (the person chose to
  use it: first run after updating, Open Shared Database, Move Database). Later edits to that database,
  made anywhere, don't add folders.

Subfolders of a trusted folder are trusted too.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from .paths import data_dir, make_private

log = logging.getLogger("reelay")
FILE_NAME = "trusted_folders.json"
_lock = threading.Lock()
_cache: dict = {"key": None, "data": None}


def _file() -> Path:
    return data_dir() / FILE_NAME


def _norm(folder) -> str:
    try:
        return str(Path(folder).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, TypeError, ValueError):
        return ""


def _load() -> dict:
    path = _file()
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return {"folders": [], "databases": []}
    if _cache["key"] == key:
        return _cache["data"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("Couldn't read %s; no folders are trusted until it's fixed.", path)
        data = {}
    data = {"folders": [f for f in data.get("folders", []) if isinstance(f, str)],
            "databases": [d for d in data.get("databases", []) if isinstance(d, str)]}
    _cache.update(key=key, data=data)
    return data


def _save(data: dict) -> None:
    path = _file()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    make_private(tmp)
    os.replace(tmp, path)
    _cache.update(key=None, data=None)


def trust(*folders) -> None:
    """Add folders the person chose on this computer."""
    new = [f for f in (_norm(x) for x in folders if x) if f]
    if not new:
        return
    with _lock:
        data = _load()
        merged = list(dict.fromkeys(data["folders"] + new))
        if merged != data["folders"]:
            _save({**data, "folders": merged})


def is_trusted(folder) -> bool:
    target = _norm(folder)
    if not target:
        return False
    for f in _load()["folders"]:
        if target == f or target.startswith(f.rstrip(os.sep) + os.sep):
            return True
    return False


def first_open(store) -> bool:
    """The first time this computer opens a database file, trust the folders it names. Returns True if it
    was the first time."""
    db = _norm(store.path) if store.path != ":memory:" else ""
    if not db:
        return False
    with _lock:
        data = _load()
        if db in data["databases"]:
            return False
        folders = [p.output_folder for p in store.list_profiles()]
        for profile in store.list_profiles():
            folders += [row.item.folder for row in store.load_videos(profile.id, archived=None) if row.item.folder]
        merged = list(dict.fromkeys(data["folders"] + [f for f in (_norm(x) for x in folders if x) if f]))
        _save({"folders": merged, "databases": data["databases"] + [db]})
    log.info("First time this computer opens %s: trusting the %d folder(s) it names.", db, len(set(folders)))
    return True
