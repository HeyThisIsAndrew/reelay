"""SQLite persistence: profiles, connected accounts, videos, board stages, posts.

OAuth tokens are deliberately NOT stored here; they live in the OS keychain
(see tokens.py), so copying the database never leaks account access.

Threads: the Store is safe to use from any thread (the GUI, the agent API
server, the dispatcher). One lock serialises access to the single SQLite
connection; change notifications are delivered after the lock is released,
on the thread that made the write. GUI code must hop to the GUI thread
(see gui/bridge.py).

Change feed: every write announces a `Change` to subscribers after it commits,
so the desktop UI (and the companion server) can react to edits made
anywhere. Wrap writes in `with store.origin("remote"):` to tag who made them.
"""

from __future__ import annotations

import functools
import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .models import (TICKET_FIELDS, Account, Dispatch, DispatchStatus, ManualPost, Post, PostStatus, Profile,
                     Revision, Source, Stage, Ticket, VideoItem, VideoRow)

log = logging.getLogger("reelay")

SCHEMA_VERSION = 5

# Post fields whose edits are logical metadata commits (logged as revisions).
# Status, remote ids and errors are publishing state, tracked by dispatches.
REVISED_POST_FIELDS = ("title", "description", "tags", "target_platforms", "tiktok_text", "instagram_text",
                       "bluesky_text", "threads_text", "facebook_text", "hashtags")


@dataclass(frozen=True)
class Change:
    """One committed write. `entity` is profile | account | video | post | setting."""
    entity: str
    ids: tuple = ()
    profile_id: int | None = None
    origin: str = "local"

_MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE profiles (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        color TEXT NOT NULL DEFAULT '#8a8984',
        output_folder TEXT NOT NULL,
        template TEXT NOT NULL DEFAULT '{platform}_{date}_{id}',
        quality TEXT NOT NULL DEFAULT 'best',
        cookies_browser TEXT NOT NULL DEFAULT '',
        last_source TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE TABLE accounts (
        id INTEGER PRIMARY KEY,
        profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        platform TEXT NOT NULL,
        external_id TEXT NOT NULL,
        display_name TEXT NOT NULL,
        handle TEXT NOT NULL DEFAULT '',
        avatar_url TEXT NOT NULL DEFAULT '',
        connected_at TEXT NOT NULL,
        UNIQUE (profile_id, platform, external_id)
    );
    CREATE TABLE videos (
        id INTEGER PRIMARY KEY,
        profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        platform TEXT NOT NULL,
        video_id TEXT NOT NULL,
        data TEXT NOT NULL,
        stage TEXT NOT NULL DEFAULT 'backlog',
        position REAL NOT NULL DEFAULT 0,
        notes TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        UNIQUE (profile_id, platform, video_id)
    );
    CREATE INDEX videos_by_profile ON videos (profile_id, stage);
    CREATE TABLE posts (
        id INTEGER PRIMARY KEY,
        video_row INTEGER NOT NULL UNIQUE REFERENCES videos(id) ON DELETE CASCADE,
        account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
        title TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        tags TEXT NOT NULL DEFAULT '[]',
        privacy TEXT NOT NULL DEFAULT 'public',
        publish_at TEXT,
        made_for_kids INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'draft',
        remote_id TEXT NOT NULL DEFAULT '',
        remote_url TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    );
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
    2: """
    ALTER TABLE posts ADD COLUMN target_platforms TEXT NOT NULL DEFAULT '["youtube"]';
    ALTER TABLE posts ADD COLUMN bluesky_text TEXT;
    ALTER TABLE posts ADD COLUMN threads_text TEXT;
    ALTER TABLE accounts ADD COLUMN last_verified_at TEXT;
    CREATE TABLE dispatches (
        id INTEGER PRIMARY KEY,
        video_row INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        platform TEXT NOT NULL,
        account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
        status TEXT NOT NULL,
        due_at TEXT NOT NULL,
        publish_at TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        claimed_at TEXT,
        claimed_by TEXT,
        remote_id TEXT NOT NULL DEFAULT '',
        remote_url TEXT NOT NULL DEFAULT '',
        checkpoint TEXT NOT NULL DEFAULT '',
        options TEXT NOT NULL DEFAULT '{}',
        error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (video_row, platform)
    );
    CREATE INDEX dispatches_due ON dispatches (status, due_at);
    INSERT INTO dispatches (video_row, platform, account_id, status, due_at, publish_at, remote_id, remote_url,
                            created_at, updated_at)
        SELECT video_row, 'youtube', account_id, status, updated_at, publish_at, remote_id, remote_url,
               updated_at, updated_at
        FROM posts WHERE status IN ('scheduled', 'published') AND remote_id != '';
    """,
    3: """
    ALTER TABLE posts ADD COLUMN tiktok_text TEXT;
    ALTER TABLE posts ADD COLUMN instagram_text TEXT;
    ALTER TABLE posts ADD COLUMN facebook_text TEXT;
    ALTER TABLE posts ADD COLUMN hashtags TEXT;
    """,
    4: """
    CREATE TABLE sources (
        id INTEGER PRIMARY KEY,
        profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        platform TEXT NOT NULL,
        url TEXT NOT NULL,
        mine INTEGER NOT NULL DEFAULT 0,
        last_fetched_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX sources_by_profile ON sources (profile_id);
    """,
    5: """
    ALTER TABLE videos ADD COLUMN transcript TEXT;
    ALTER TABLE videos ADD COLUMN thumbnail_path TEXT;
    ALTER TABLE videos ADD COLUMN content_type TEXT;
    ALTER TABLE videos ADD COLUMN topic TEXT;
    ALTER TABLE videos ADD COLUMN keywords TEXT;
    ALTER TABLE videos ADD COLUMN creator TEXT;
    ALTER TABLE videos ADD COLUMN original_publish_date TEXT;
    ALTER TABLE videos ADD COLUMN source_platform TEXT;
    ALTER TABLE videos ADD COLUMN source_url TEXT;
    ALTER TABLE videos ADD COLUMN imported_at TEXT;
    ALTER TABLE videos ADD COLUMN authorization TEXT;
    ALTER TABLE videos ADD COLUMN derived_from INTEGER REFERENCES videos(id) ON DELETE SET NULL;
    ALTER TABLE videos ADD COLUMN ready_gate_exempt INTEGER NOT NULL DEFAULT 0;
    -- Cards already scheduled or published keep going exactly as before.
    UPDATE videos SET ready_gate_exempt = 1 WHERE stage IN ('scheduled', 'published');
    -- Provenance for videos imported before v5, from what was recorded then.
    UPDATE videos SET
        source_platform = lower(platform),
        source_url = NULLIF(json_extract(data, '$.url'), ''),
        imported_at = updated_at,
        creator = NULLIF(json_extract(data, '$.uploader'), ''),
        original_publish_date = CASE WHEN length(json_extract(data, '$.upload_date')) = 8 THEN
            substr(json_extract(data, '$.upload_date'), 1, 4) || '-' || substr(json_extract(data, '$.upload_date'), 5, 2)
            || '-' || substr(json_extract(data, '$.upload_date'), 7, 2) || 'T00:00:00Z' END;
    CREATE TABLE revisions (
        id INTEGER PRIMARY KEY,
        video_row INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        at TEXT NOT NULL,
        origin TEXT NOT NULL,
        field TEXT NOT NULL,
        old_value TEXT,
        new_value TEXT
    );
    CREATE INDEX revisions_by_video ON revisions (video_row, id);
    CREATE TABLE manual_posts (
        id INTEGER PRIMARY KEY,
        video_row INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        platform TEXT NOT NULL,
        posted_at TEXT NOT NULL,
        url TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        origin TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX manual_posts_by_video ON manual_posts (video_row, posted_at);
    """,
}

PROFILE_COLORS = ["#8a8984", "#b5835a", "#7a9a7e", "#7f8fb0", "#a77fa0", "#b07a7a", "#9a9a6a"]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _revision_text(value) -> str | None:
    """How a field value is written in the revision log (lists as JSON)."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), ensure_ascii=False)
    return str(value)


def upload_date_iso(yyyymmdd: str) -> str | None:
    if len(yyyymmdd or "") == 8 and yyyymmdd.isdigit():
        return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}T00:00:00Z"
    return None


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._tls = threading.local()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()
        self._listeners: list[Callable[[Change], None]] = []

    def _state(self):
        st = self._tls
        if not hasattr(st, "depth"):
            st.depth, st.pending, st.origin = 0, [], "local"
        return st

    # ================================================================ change feed
    def subscribe(self, listener: Callable[[Change], None]) -> Callable[[], None]:
        """Call `listener(change)` after every write. Returns an unsubscribe function."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    @contextmanager
    def origin(self, name: str) -> Iterator[None]:
        """Tag writes made inside the block on this thread (e.g. "api", "remote")."""
        st = self._state()
        previous, st.origin = st.origin, name
        try:
            yield
        finally:
            st.origin = previous

    def _emit(self, entity: str, ids: Iterable = (), profile_id: int | None = None) -> None:
        st = self._state()
        change = Change(entity, tuple(ids), profile_id, st.origin)
        if st.depth > 0:
            st.pending.append(change)  # delivered once the outermost call releases the lock
        else:
            self._deliver([change])

    def _deliver(self, changes: list[Change]) -> None:
        for change in changes:
            for listener in list(self._listeners):
                try:
                    listener(change)
                except Exception:  # noqa: BLE001 — a broken listener must never break a write
                    log.exception("Store listener failed for %s", change)

    def _profile_of_videos(self, row_ids: Iterable[int]) -> int | None:
        ids = list(row_ids)
        if not ids:
            return None
        rows = self.conn.execute(f"SELECT DISTINCT profile_id FROM videos WHERE id IN ({','.join('?' * len(ids))})",
                                 ids).fetchall()
        return rows[0][0] if len(rows) == 1 else None

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for target in range(version + 1, SCHEMA_VERSION + 1):
            with self.conn:
                self.conn.executescript(_MIGRATIONS[target])
                self.conn.execute(f"PRAGMA user_version = {target}")

    # ================================================================ settings
    def get_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        self._emit("setting", (key,))

    # ================================================================ profiles
    def list_profiles(self) -> list[Profile]:
        rows = self.conn.execute("SELECT * FROM profiles ORDER BY name COLLATE NOCASE").fetchall()
        return [self._profile(r) for r in rows]

    def get_profile(self, profile_id: int) -> Profile | None:
        row = self.conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        return self._profile(row) if row else None

    def create_profile(self, name: str, output_folder: str | Path, color: str | None = None) -> Profile:
        count = self.conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
        profile = Profile(name=name.strip() or "Untitled", output_folder=str(output_folder),
                          color=color or PROFILE_COLORS[count % len(PROFILE_COLORS)], created_at=utcnow())
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO profiles (name, color, output_folder, template, quality, cookies_browser, "
                "last_source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (profile.name, profile.color, profile.output_folder, profile.template, profile.quality,
                 profile.cookies_browser, profile.last_source, profile.created_at))
        profile.id = cur.lastrowid
        self._emit("profile", (profile.id,), profile.id)
        return profile

    def update_profile(self, profile: Profile) -> None:
        with self.tx() as c:
            c.execute("UPDATE profiles SET name=?, color=?, output_folder=?, template=?, quality=?, "
                      "cookies_browser=?, last_source=? WHERE id=?",
                      (profile.name, profile.color, profile.output_folder, profile.template,
                       profile.quality, profile.cookies_browser, profile.last_source, profile.id))
        self._emit("profile", (profile.id,), profile.id)

    def delete_profile(self, profile_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        self._emit("profile", (profile_id,), profile_id)
        if self.get_setting("active_profile") == str(profile_id):
            self.set_setting("active_profile", "")

    def active_profile(self, default_folder: Path) -> Profile:
        """The profile last used, creating a first one if none exist."""
        active = self.get_setting("active_profile")
        profile = self.get_profile(int(active)) if active.isdigit() else None
        if profile is None:
            profile = self.default_profile() or self.create_profile("My Channel", default_folder)
            self.set_active_profile(profile.id)
        return profile

    def default_profile(self) -> Profile | None:
        """The first profile ever made ("My Channel" on a fresh install): what
        opens when no other profile was last used."""
        row = self.conn.execute("SELECT * FROM profiles ORDER BY id LIMIT 1").fetchone()
        return self._profile(row) if row else None

    def set_active_profile(self, profile_id: int) -> None:
        self.set_setting("active_profile", str(profile_id))

    @staticmethod
    def _profile(row: sqlite3.Row) -> Profile:
        return Profile(id=row["id"], name=row["name"], color=row["color"], output_folder=row["output_folder"],
                       template=row["template"], quality=row["quality"],
                       cookies_browser=row["cookies_browser"], last_source=row["last_source"],
                       created_at=row["created_at"])

    # ================================================================ accounts
    def list_accounts(self, profile_id: int, platform: str | None = None) -> list[Account]:
        sql, args = "SELECT * FROM accounts WHERE profile_id = ?", [profile_id]
        if platform:
            sql += " AND platform = ?"
            args.append(platform)
        rows = self.conn.execute(sql + " ORDER BY display_name COLLATE NOCASE", args).fetchall()
        return [self._account(r) for r in rows]

    def get_account(self, account_id: int) -> Account | None:
        row = self.conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return self._account(row) if row else None

    def save_account(self, account: Account) -> Account:
        account.connected_at = account.connected_at or utcnow()
        with self.tx() as c:
            c.execute(
                "INSERT INTO accounts (profile_id, platform, external_id, display_name, handle, avatar_url, "
                "connected_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(profile_id, platform, external_id) "
                "DO UPDATE SET display_name=excluded.display_name, handle=excluded.handle, "
                "avatar_url=excluded.avatar_url",
                (account.profile_id, account.platform, account.external_id, account.display_name,
                 account.handle, account.avatar_url, account.connected_at))
        row = self.conn.execute("SELECT id FROM accounts WHERE profile_id=? AND platform=? AND external_id=?",
                                (account.profile_id, account.platform, account.external_id)).fetchone()
        account.id = row["id"]
        self._emit("account", (account.id,), account.profile_id)
        return account

    def delete_account(self, account_id: int) -> None:
        account = self.get_account(account_id)
        with self.tx() as c:
            c.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        self._emit("account", (account_id,), account.profile_id if account else None)

    def mark_verified(self, account_id: int, when: str | None = None) -> None:
        """Record a successful live check (or successful post) for an account."""
        with self.tx() as c:
            c.execute("UPDATE accounts SET last_verified_at=? WHERE id=?", (when or utcnow(), account_id))
        account = self.get_account(account_id)
        self._emit("account", (account_id,), account.profile_id if account else None)

    def account_in_use_elsewhere(self, account: Account) -> bool:
        """Is the same remote account connected in another profile (shares its token)?"""
        row = self.conn.execute("SELECT COUNT(*) FROM accounts WHERE platform=? AND external_id=? AND id != ?",
                                (account.platform, account.external_id, account.id)).fetchone()
        return row[0] > 0

    @staticmethod
    def _account(row: sqlite3.Row) -> Account:
        return Account(id=row["id"], profile_id=row["profile_id"], platform=row["platform"],
                       external_id=row["external_id"], display_name=row["display_name"],
                       handle=row["handle"], avatar_url=row["avatar_url"], connected_at=row["connected_at"],
                       last_verified_at=row["last_verified_at"])

    # ================================================================ videos
    def save_videos(self, profile_id: int, items: Iterable[VideoItem],
                    authorization: str | None = None) -> dict[str, int]:
        """Insert or update videos (metadata only; stage/notes untouched).

        New videos get their provenance recorded (source platform and link, when
        they were imported, the original publish date and creator, and the
        `authorization` the importer has). Provenance already recorded is never
        overwritten. Returns {item.key: row id}."""
        now = utcnow()
        ids: dict[str, int] = {}
        with self.tx() as c:
            for item in items:
                c.execute(
                    "INSERT INTO videos (profile_id, platform, video_id, data, updated_at, source_platform, "
                    "source_url, imported_at, creator, original_publish_date, authorization) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(profile_id, platform, video_id) DO UPDATE SET data=excluded.data, "
                    "updated_at=excluded.updated_at, "
                    "source_url=COALESCE(source_url, excluded.source_url), "
                    "creator=COALESCE(creator, excluded.creator), "
                    "original_publish_date=COALESCE(original_publish_date, excluded.original_publish_date), "
                    "authorization=COALESCE(authorization, excluded.authorization)",
                    (profile_id, item.platform, item.video_id, json.dumps(item.to_record()), now,
                     item.platform.lower(), item.url or None, now, item.uploader or None,
                     upload_date_iso(item.upload_date), authorization))
                row = c.execute("SELECT id FROM videos WHERE profile_id=? AND platform=? AND video_id=?",
                                (profile_id, item.platform, item.video_id)).fetchone()
                ids[item.key] = row["id"]
        if ids:
            self._emit("video", ids.values(), profile_id)
        return ids

    def save_video(self, profile_id: int, item: VideoItem) -> int:
        return self.save_videos(profile_id, [item])[item.key]

    def load_videos(self, profile_id: int, stages: Iterable[str] | None = None) -> list[VideoRow]:
        sql, args = "SELECT * FROM videos WHERE profile_id = ?", [profile_id]
        stages = list(stages or [])
        if stages:
            sql += f" AND stage IN ({','.join('?' * len(stages))})"
            args += stages
        rows = self.conn.execute(sql + " ORDER BY position, id", args).fetchall()
        return [self._video_row(r) for r in rows]

    def get_video(self, row_id: int) -> VideoRow | None:
        row = self.conn.execute("SELECT * FROM videos WHERE id = ?", (row_id,)).fetchone()
        return self._video_row(row) if row else None

    def set_stage(self, row_ids: Iterable[int], stage: str, position: float | None = None) -> None:
        row_ids = list(row_ids)
        if stage not in Stage.ALL:
            raise ValueError(f"unknown stage {stage!r}")
        now = utcnow()
        with self.tx() as c:
            for row_id in row_ids:
                if position is None:
                    top = c.execute("SELECT COALESCE(MAX(position), 0) FROM videos WHERE stage = ? AND "
                                    "profile_id = (SELECT profile_id FROM videos WHERE id = ?)",
                                    (stage, row_id)).fetchone()[0]
                    pos = top + 1
                else:
                    pos = position
                c.execute("UPDATE videos SET stage=?, position=?, updated_at=? WHERE id=?", (stage, pos, now, row_id))
        self._emit("video", row_ids, self._profile_of_videos(row_ids))

    def reorder(self, ordered_row_ids: list[int]) -> None:
        with self.tx() as c:
            for pos, row_id in enumerate(ordered_row_ids, 1):
                c.execute("UPDATE videos SET position=? WHERE id=?", (float(pos), row_id))
        self._emit("video", ordered_row_ids, self._profile_of_videos(ordered_row_ids))

    def set_notes(self, row_id: int, notes: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE videos SET notes=?, updated_at=? WHERE id=?", (notes, utcnow(), row_id))
        self._emit("video", (row_id,), self._profile_of_videos([row_id]))

    def delete_video(self, row_id: int) -> None:
        """Remove a video from the board (its post, dispatches, history and manual
        posts go with it). Videos derived from it keep existing, unlinked."""
        profile_id = self._profile_of_videos([row_id])
        with self.tx() as c:
            c.execute("DELETE FROM videos WHERE id=?", (row_id,))
        self._emit("video", (row_id,), profile_id)

    # ---------------------------------------------------------------- ticket fields
    def update_ticket(self, row_id: int, changes: dict, post: Post | None = None) -> Ticket:
        """Write ticket fields (and optionally the edited post) as one logical
        commit: the update and its revision log entries share a transaction, so
        if logging fails nothing is written."""
        unknown = sorted(set(changes) - set(TICKET_FIELDS))
        if unknown:
            raise ValueError(f"unknown ticket field(s): {', '.join(unknown)}")
        with self.tx() as c:
            row = c.execute("SELECT * FROM videos WHERE id=?", (row_id,)).fetchone()
            if row is None:
                raise KeyError(row_id)
            old = self._ticket(row)
            diffs = [(f, getattr(old, f), v) for f, v in changes.items() if getattr(old, f) != v]
            if diffs:
                c.execute(f"UPDATE videos SET {', '.join(f'{f}=?' for f, _o, _n in diffs)}, updated_at=? WHERE id=?",
                          (*[n for _f, _o, n in diffs], utcnow(), row_id))
                self._log_revisions(c, row_id, diffs)
            if post is not None:
                post.updated_at = utcnow()
                self._write_post(c, post)
        if diffs:
            self._emit("video", (row_id,), row["profile_id"])
        if post is not None:
            self._emit("post", (row_id,), row["profile_id"])
        return self.get_video(row_id).ticket

    def _log_revisions(self, c: sqlite3.Connection, video_row: int, diffs: list[tuple[str, object, object]]) -> None:
        """Append (field, old, new) entries for one commit. Runs inside the
        caller's transaction: an error here rolls the whole write back."""
        at, origin = utcnow(), self._state().origin
        c.executemany("INSERT INTO revisions (video_row, at, origin, field, old_value, new_value) "
                      "VALUES (?, ?, ?, ?, ?, ?)",
                      [(video_row, at, origin, f, _revision_text(o), _revision_text(n)) for f, o, n in diffs])

    def revisions(self, video_row: int, limit: int = 500) -> list[Revision]:
        """The ticket's change log, newest first."""
        rows = self.conn.execute("SELECT * FROM revisions WHERE video_row=? ORDER BY id DESC LIMIT ?",
                                 (video_row, limit)).fetchall()
        return [Revision(id=r["id"], video_row=r["video_row"], at=r["at"], origin=r["origin"], field=r["field"],
                         old_value=r["old_value"], new_value=r["new_value"]) for r in rows]

    # ---------------------------------------------------------------- manual posts
    def add_manual_post(self, post: ManualPost) -> ManualPost:
        post.created_at = utcnow()
        post.origin = self._state().origin
        with self.tx() as c:
            cur = c.execute("INSERT INTO manual_posts (video_row, platform, posted_at, url, note, origin, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (post.video_row, post.platform, post.posted_at, post.url, post.note, post.origin,
                             post.created_at))
        post.id = cur.lastrowid
        self._emit("video", (post.video_row,), self._profile_of_videos([post.video_row]))
        return post

    def manual_posts(self, video_row: int) -> list[ManualPost]:
        rows = self.conn.execute("SELECT * FROM manual_posts WHERE video_row=? ORDER BY posted_at, id",
                                 (video_row,)).fetchall()
        return [ManualPost(id=r["id"], video_row=r["video_row"], platform=r["platform"], posted_at=r["posted_at"],
                           url=r["url"], note=r["note"], origin=r["origin"], created_at=r["created_at"])
                for r in rows]

    def delete_manual_post(self, manual_id: int) -> None:
        row = self.conn.execute("SELECT video_row FROM manual_posts WHERE id=?", (manual_id,)).fetchone()
        with self.tx() as c:
            c.execute("DELETE FROM manual_posts WHERE id=?", (manual_id,))
        if row:
            self._emit("video", (row["video_row"],), self._profile_of_videos([row["video_row"]]))

    @staticmethod
    def _ticket(row: sqlite3.Row) -> Ticket:
        return Ticket(**{f: row[f] for f in TICKET_FIELDS})

    @staticmethod
    def _video_row(row: sqlite3.Row) -> VideoRow:
        item = VideoItem.from_record(json.loads(row["data"]))
        return VideoRow(id=row["id"], item=item, stage=row["stage"], position=row["position"], notes=row["notes"],
                        profile_id=row["profile_id"], updated_at=row["updated_at"], ticket=Store._ticket(row),
                        ready_gate_exempt=bool(row["ready_gate_exempt"]))

    # ================================================================ posts
    def get_post(self, video_row: int) -> Post | None:
        row = self.conn.execute("SELECT * FROM posts WHERE video_row = ?", (video_row,)).fetchone()
        return self._post(row) if row else None

    def posts_with_status(self, profile_id: int, statuses: Iterable[str]) -> list[Post]:
        statuses = list(statuses)
        rows = self.conn.execute(
            f"SELECT p.* FROM posts p JOIN videos v ON v.id = p.video_row WHERE v.profile_id = ? "
            f"AND p.status IN ({','.join('?' * len(statuses))})", [profile_id, *statuses]).fetchall()
        return [self._post(r) for r in rows]

    def save_post(self, post: Post) -> Post:
        post.updated_at = utcnow()
        with self.tx() as c:
            self._write_post(c, post)
        self._emit("post", (post.video_row,), self._profile_of_videos([post.video_row]))
        return post

    def _write_post(self, c: sqlite3.Connection, post: Post) -> None:
        old_row = c.execute("SELECT * FROM posts WHERE video_row=?", (post.video_row,)).fetchone()
        old = self._post(old_row) if old_row else None
        diffs = [(f, getattr(old, f) if old else None, getattr(post, f)) for f in REVISED_POST_FIELDS
                 if (getattr(old, f) if old else None) != getattr(post, f)]
        if old is None:   # a new ticket: only what was actually filled in
            diffs = [d for d in diffs if d[2] not in (None, "", [])]
        values = (post.account_id, post.title, post.description, json.dumps(post.tags), post.privacy,
                  post.publish_at, int(post.made_for_kids), post.status, post.remote_id, post.remote_url,
                  post.error, post.updated_at, json.dumps(post.target_platforms), post.bluesky_text,
                  post.threads_text, post.tiktok_text, post.instagram_text, post.facebook_text, post.hashtags)
        c.execute(
            "INSERT INTO posts (account_id, title, description, tags, privacy, publish_at, made_for_kids, "
            "status, remote_id, remote_url, error, updated_at, target_platforms, bluesky_text, threads_text, "
            "tiktok_text, instagram_text, facebook_text, hashtags, "
            "video_row) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(video_row) DO UPDATE SET account_id=excluded.account_id, title=excluded.title, "
            "description=excluded.description, tags=excluded.tags, privacy=excluded.privacy, "
            "publish_at=excluded.publish_at, made_for_kids=excluded.made_for_kids, status=excluded.status, "
            "remote_id=excluded.remote_id, remote_url=excluded.remote_url, error=excluded.error, "
            "updated_at=excluded.updated_at, target_platforms=excluded.target_platforms, "
            "bluesky_text=excluded.bluesky_text, threads_text=excluded.threads_text, "
            "tiktok_text=excluded.tiktok_text, instagram_text=excluded.instagram_text, "
            "facebook_text=excluded.facebook_text, hashtags=excluded.hashtags", (*values, post.video_row))
        post.id = c.execute("SELECT id FROM posts WHERE video_row=?", (post.video_row,)).fetchone()["id"]
        if diffs:
            self._log_revisions(c, post.video_row, diffs)

    def reset_interrupted_uploads(self) -> int:
        """Uploads that were running when the app quit go back to draft."""
        with self.tx() as c:
            # Posts with dispatch rows are recovered by the dispatcher instead.
            cur = c.execute("UPDATE posts SET status=?, error=? WHERE status=? AND "
                            "video_row NOT IN (SELECT video_row FROM dispatches)",
                            (PostStatus.DRAFT, "Upload was interrupted. Try again.", PostStatus.UPLOADING))
        if cur.rowcount:
            self._emit("post")
        return cur.rowcount

    @staticmethod
    def _post(row: sqlite3.Row) -> Post:
        return Post(id=row["id"], video_row=row["video_row"], account_id=row["account_id"], title=row["title"],
                    description=row["description"], tags=json.loads(row["tags"] or "[]"),
                    privacy=row["privacy"], publish_at=row["publish_at"],
                    made_for_kids=bool(row["made_for_kids"]), status=row["status"], remote_id=row["remote_id"],
                    remote_url=row["remote_url"], error=row["error"], updated_at=row["updated_at"],
                    target_platforms=json.loads(row["target_platforms"] or '["youtube"]'),
                    bluesky_text=row["bluesky_text"], threads_text=row["threads_text"],
                    tiktok_text=row["tiktok_text"], instagram_text=row["instagram_text"],
                    facebook_text=row["facebook_text"], hashtags=row["hashtags"])

    # ================================================================ download sources
    def list_sources(self, profile_id: int) -> list[Source]:
        rows = self.conn.execute("SELECT * FROM sources WHERE profile_id=? ORDER BY mine DESC, name COLLATE NOCASE, id",
                                 (profile_id,)).fetchall()
        return [self._source(r) for r in rows]

    def get_source(self, source_id: int) -> Source | None:
        row = self.conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        return self._source(row) if row else None

    def add_source(self, source: Source) -> Source:
        source.created_at = source.created_at or utcnow()
        with self.tx() as c:
            cur = c.execute("INSERT INTO sources (profile_id, name, platform, url, mine, last_fetched_at, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (source.profile_id, source.name, source.platform, source.url, int(source.mine),
                             source.last_fetched_at, source.created_at))
        source.id = cur.lastrowid
        self._emit("source", (source.id,), source.profile_id)
        return source

    def update_source(self, source: Source) -> None:
        with self.tx() as c:
            c.execute("UPDATE sources SET name=?, url=?, platform=?, mine=?, last_fetched_at=? WHERE id=?",
                      (source.name, source.url, source.platform, int(source.mine), source.last_fetched_at, source.id))
        self._emit("source", (source.id,), source.profile_id)

    def delete_source(self, source_id: int) -> None:
        source = self.get_source(source_id)
        with self.tx() as c:
            c.execute("DELETE FROM sources WHERE id=?", (source_id,))
        if source:
            self._emit("source", (source_id,), source.profile_id)

    @staticmethod
    def _source(row: sqlite3.Row) -> Source:
        return Source(id=row["id"], profile_id=row["profile_id"], name=row["name"], platform=row["platform"],
                      url=row["url"], mine=bool(row["mine"]), last_fetched_at=row["last_fetched_at"],
                      created_at=row["created_at"])

    # ================================================================ dispatches
    # Per-platform delivery rows. Claiming is atomic: a row only moves from
    # pending to dispatching in one UPDATE, so two workers (or a restart) can
    # never both send it.

    def schedule_post(self, post: Post, dispatches: list[Dispatch], stage: str,
                      claim: Iterable[str] = (), claimed_by: str = "") -> list[Dispatch]:
        """Save the post, (re)create its dispatch rows and move the card, in one
        transaction. Platforms in `claim` are claimed immediately for `claimed_by`."""
        now = utcnow()
        post.updated_at = now
        claim = set(claim)
        with self.tx() as c:
            self._write_post(c, post)
            for d in dispatches:
                claimed = d.platform in claim
                c.execute(
                    "INSERT INTO dispatches (video_row, platform, account_id, status, due_at, publish_at, attempts, "
                    "next_attempt_at, claimed_at, claimed_by, remote_id, remote_url, checkpoint, options, error, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,0,NULL,?,?,'','','',?,'',?,?) "
                    "ON CONFLICT(video_row, platform) DO UPDATE SET account_id=excluded.account_id, "
                    "status=excluded.status, due_at=excluded.due_at, publish_at=excluded.publish_at, attempts=0, "
                    "next_attempt_at=NULL, claimed_at=excluded.claimed_at, claimed_by=excluded.claimed_by, "
                    "remote_id='', remote_url='', checkpoint='', options=excluded.options, error='', "
                    "updated_at=excluded.updated_at",
                    (d.video_row, d.platform, d.account_id,
                     DispatchStatus.DISPATCHING if claimed else DispatchStatus.PENDING,
                     d.due_at, d.publish_at, now if claimed else None, claimed_by if claimed else None,
                     json.dumps(d.options), now, now))
            top = c.execute("SELECT COALESCE(MAX(position), 0) FROM videos WHERE stage=? AND profile_id="
                            "(SELECT profile_id FROM videos WHERE id=?)", (stage, post.video_row)).fetchone()[0]
            c.execute("UPDATE videos SET stage=?, position=?, updated_at=? WHERE id=? AND stage != ?",
                      (stage, top + 1, now, post.video_row, stage))
        profile_id = self._profile_of_videos([post.video_row])
        self._emit("post", (post.video_row,), profile_id)
        self._emit("dispatch", (post.video_row,), profile_id)
        self._emit("video", (post.video_row,), profile_id)
        return self.dispatches_for(post.video_row)

    def dispatches_for(self, video_row: int) -> list[Dispatch]:
        rows = self.conn.execute("SELECT * FROM dispatches WHERE video_row=? ORDER BY id", (video_row,)).fetchall()
        return [self._dispatch(r) for r in rows]

    def get_dispatch(self, dispatch_id: int) -> Dispatch | None:
        row = self.conn.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
        return self._dispatch(row) if row else None

    def claim_due(self, now: str, claimed_by: str, limit: int = 10,
                  platforms: Iterable[str] | None = None) -> list[Dispatch]:
        """Atomically claim pending dispatches that are due (and past any retry wait).
        `platforms` limits claiming to those platforms; others are left untouched."""
        platform_sql, platform_args = "", []
        if platforms is not None:
            platforms = list(platforms)
            if not platforms:
                return []
            platform_sql = f" AND platform IN ({','.join('?' * len(platforms))})"
            platform_args = platforms
        with self.tx() as c:
            rows = c.execute(
                "UPDATE dispatches SET status=?, claimed_at=?, claimed_by=?, updated_at=? WHERE id IN ("
                "  SELECT id FROM dispatches WHERE status=? AND due_at <= ? "
                f"  AND (next_attempt_at IS NULL OR next_attempt_at <= ?){platform_sql} ORDER BY due_at, id LIMIT ?"
                ") AND status=? RETURNING *",
                (DispatchStatus.DISPATCHING, now, claimed_by, now, DispatchStatus.PENDING, now, now, *platform_args,
                 limit, DispatchStatus.PENDING)).fetchall()
        claimed = sorted((self._dispatch(r) for r in rows), key=lambda d: (d.due_at, d.id))
        self._emit_dispatch_rows(claimed)
        return claimed

    def claim_dispatch(self, dispatch_id: int, claimed_by: str) -> Dispatch | None:
        """Claim one specific pending dispatch; None if someone else has it."""
        now = utcnow()
        with self.tx() as c:
            row = c.execute("UPDATE dispatches SET status=?, claimed_at=?, claimed_by=?, updated_at=? "
                            "WHERE id=? AND status=? RETURNING *",
                            (DispatchStatus.DISPATCHING, now, claimed_by, now, dispatch_id,
                             DispatchStatus.PENDING)).fetchone()
        claimed = self._dispatch(row) if row else None
        self._emit_dispatch_rows([claimed] if claimed else [])
        return claimed

    def heartbeat(self, dispatch_id: int) -> None:
        """Refresh a claim during long work (uploads) so it isn't considered stale."""
        with self.tx() as c:
            c.execute("UPDATE dispatches SET claimed_at=? WHERE id=? AND status=?",
                      (utcnow(), dispatch_id, DispatchStatus.DISPATCHING))

    def recover_stale(self, instance_id: str, stale_before: str) -> list[Dispatch]:
        """Requeue claims left by another (crashed) run, or held too long by this one."""
        now = utcnow()
        with self.tx() as c:
            rows = c.execute(
                "UPDATE dispatches SET status=?, claimed_at=NULL, claimed_by=NULL, updated_at=? "
                "WHERE status=? AND (claimed_by IS NULL OR claimed_by != ? OR claimed_at < ?) RETURNING *",
                (DispatchStatus.PENDING, now, DispatchStatus.DISPATCHING, instance_id, stale_before)).fetchall()
        recovered = [self._dispatch(r) for r in rows]
        self._emit_dispatch_rows(recovered)
        return recovered

    def set_checkpoint(self, dispatch_id: int, checkpoint: str) -> None:
        """Remember progress (e.g. a created container id) so a retry can resume, not repeat."""
        with self.tx() as c:
            c.execute("UPDATE dispatches SET checkpoint=?, updated_at=? WHERE id=?", (checkpoint, utcnow(), dispatch_id))

    def finish_dispatch(self, dispatch_id: int, status: str, remote_id: str = "", remote_url: str = "",
                        error: str = "") -> Dispatch | None:
        if status not in (DispatchStatus.SCHEDULED, DispatchStatus.PUBLISHED, DispatchStatus.FAILED):
            raise ValueError(f"not a final status: {status}")
        with self.tx() as c:
            row = c.execute(
                "UPDATE dispatches SET status=?, remote_id=CASE WHEN ?='' THEN remote_id ELSE ? END, "
                "remote_url=CASE WHEN ?='' THEN remote_url ELSE ? END, error=?, claimed_at=NULL, claimed_by=NULL, "
                "next_attempt_at=NULL, updated_at=? WHERE id=? RETURNING *",
                (status, remote_id, remote_id, remote_url, remote_url, error, utcnow(), dispatch_id)).fetchone()
        done = self._dispatch(row) if row else None
        self._emit_dispatch_rows([done] if done else [])
        return done

    def retry_dispatch(self, dispatch_id: int, error: str, next_attempt_at: str) -> Dispatch | None:
        """A transient failure: count the attempt and put it back in the queue."""
        with self.tx() as c:
            row = c.execute(
                "UPDATE dispatches SET status=?, attempts=attempts+1, error=?, next_attempt_at=?, claimed_at=NULL, "
                "claimed_by=NULL, updated_at=? WHERE id=? RETURNING *",
                (DispatchStatus.PENDING, error, next_attempt_at, utcnow(), dispatch_id)).fetchone()
        again = self._dispatch(row) if row else None
        self._emit_dispatch_rows([again] if again else [])
        return again

    def dispatches_with_status(self, profile_id: int, platform: str, statuses: Iterable[str]) -> list[Dispatch]:
        statuses = list(statuses)
        rows = self.conn.execute(
            f"SELECT d.* FROM dispatches d JOIN videos v ON v.id = d.video_row WHERE v.profile_id = ? "
            f"AND d.platform = ? AND d.status IN ({','.join('?' * len(statuses))}) ORDER BY d.id",
            [profile_id, platform, *statuses]).fetchall()
        return [self._dispatch(r) for r in rows]

    def pending_dispatches(self) -> list[Dispatch]:
        rows = self.conn.execute("SELECT * FROM dispatches WHERE status IN (?, ?) ORDER BY due_at, id",
                                 (DispatchStatus.PENDING, DispatchStatus.DISPATCHING)).fetchall()
        return [self._dispatch(r) for r in rows]

    def _emit_dispatch_rows(self, dispatches: list[Dispatch]) -> None:
        rows = sorted({d.video_row for d in dispatches})
        if rows:
            self._emit("dispatch", rows, self._profile_of_videos(rows))

    @staticmethod
    def _dispatch(row: sqlite3.Row) -> Dispatch:
        return Dispatch(id=row["id"], video_row=row["video_row"], platform=row["platform"],
                        account_id=row["account_id"], status=row["status"], due_at=row["due_at"],
                        publish_at=row["publish_at"], attempts=row["attempts"],
                        next_attempt_at=row["next_attempt_at"], claimed_at=row["claimed_at"],
                        claimed_by=row["claimed_by"], remote_id=row["remote_id"], remote_url=row["remote_url"],
                        checkpoint=row["checkpoint"], options=json.loads(row["options"] or "{}"),
                        error=row["error"], created_at=row["created_at"], updated_at=row["updated_at"])


def _locked(fn):
    """Serialise access to the connection; deliver change events after unlocking."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        st = self._state()
        pending: list[Change] = []
        try:
            with self._lock:
                st.depth += 1
                try:
                    return fn(self, *args, **kwargs)
                finally:
                    st.depth -= 1
                    if st.depth == 0:
                        pending, st.pending = st.pending, []
        finally:
            if pending:
                self._deliver(pending)
    return wrapper


for _name, _attr in list(vars(Store).items()):
    if callable(_attr) and not _name.startswith("__") and _name not in (
            "tx", "origin", "subscribe", "_state", "_emit", "_deliver") and not isinstance(_attr, staticmethod):
        setattr(Store, _name, _locked(_attr))
