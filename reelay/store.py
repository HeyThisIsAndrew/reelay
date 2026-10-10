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

from .models import (TICKET_FIELDS, Account, ApprovalRequest, BrandProfile, Dispatch, DispatchStatus, ManualPost, Post, PostStatus, Profile,
                     Revision, Source, Ticket, VideoItem, VideoRow)

log = logging.getLogger("reelay")

SCHEMA_VERSION = 9

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
    6: """
    ALTER TABLE videos ADD COLUMN archived_at TEXT;
    """,
    7: """
    ALTER TABLE manual_posts ADD COLUMN kind TEXT NOT NULL DEFAULT 'posted';
    """,
    8: """
    -- Archive search: one full-text row per video (rowid = videos.id), kept current by
    -- triggers so every write path (app, agent API, imports) updates it.
    CREATE VIEW video_search_source AS
    SELECT v.id AS id,
           trim(coalesce(p.title, '') || ' ' || coalesce(json_extract(v.data, '$.title'), '') || ' ' ||
                coalesce(json_extract(v.data, '$.custom_name'), '')) AS title,
           coalesce(json_extract(v.data, '$.description'), '') AS caption,
           coalesce(v.transcript, '') AS transcript,
           trim(coalesce(p.description, '') || ' ' ||
                replace(replace(replace(replace(coalesce(p.tags, ''), '[', ''), ']', ''), '"', ''), ',', ' ') || ' ' || coalesce(p.hashtags, '') || ' ' ||
                coalesce(p.tiktok_text, '') || ' ' || coalesce(p.instagram_text, '') || ' ' ||
                coalesce(p.facebook_text, '') || ' ' || coalesce(p.threads_text, '') || ' ' ||
                coalesce(p.bluesky_text, '') || ' ' || coalesce(v.topic, '') || ' ' || coalesce(v.keywords, '')) AS copy,
           coalesce(v.notes, '') AS notes
    FROM videos v LEFT JOIN posts p ON p.video_row = v.id;
    CREATE VIRTUAL TABLE video_search USING fts5(
        title, caption, transcript, copy, notes, tokenize = 'unicode61 remove_diacritics 2');
    INSERT INTO video_search (rowid, title, caption, transcript, copy, notes)
        SELECT id, title, caption, transcript, copy, notes FROM video_search_source;
    CREATE TRIGGER video_search_video_insert AFTER INSERT ON videos BEGIN
        INSERT INTO video_search (rowid, title, caption, transcript, copy, notes)
            SELECT id, title, caption, transcript, copy, notes FROM video_search_source WHERE id = NEW.id;
    END;
    CREATE TRIGGER video_search_video_update AFTER UPDATE OF data, transcript, notes, topic, keywords ON videos BEGIN
        DELETE FROM video_search WHERE rowid = NEW.id;
        INSERT INTO video_search (rowid, title, caption, transcript, copy, notes)
            SELECT id, title, caption, transcript, copy, notes FROM video_search_source WHERE id = NEW.id;
    END;
    CREATE TRIGGER video_search_video_delete AFTER DELETE ON videos BEGIN
        DELETE FROM video_search WHERE rowid = OLD.id;
    END;
    CREATE TRIGGER video_search_post_insert AFTER INSERT ON posts BEGIN
        DELETE FROM video_search WHERE rowid = NEW.video_row;
        INSERT INTO video_search (rowid, title, caption, transcript, copy, notes)
            SELECT id, title, caption, transcript, copy, notes FROM video_search_source WHERE id = NEW.video_row;
    END;
    CREATE TRIGGER video_search_post_update AFTER UPDATE ON posts BEGIN
        DELETE FROM video_search WHERE rowid = NEW.video_row;
        INSERT INTO video_search (rowid, title, caption, transcript, copy, notes)
            SELECT id, title, caption, transcript, copy, notes FROM video_search_source WHERE id = NEW.video_row;
    END;
    CREATE TRIGGER video_search_post_delete AFTER DELETE ON posts BEGIN
        DELETE FROM video_search WHERE rowid = OLD.video_row;
        INSERT INTO video_search (rowid, title, caption, transcript, copy, notes)
            SELECT id, title, caption, transcript, copy, notes FROM video_search_source WHERE id = OLD.video_row;
    END;

    -- Brand voice: how the local AI helper should sound for this creator.
    CREATE TABLE brand_profiles (
        id INTEGER PRIMARY KEY,
        profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        tone TEXT NOT NULL DEFAULT '',
        avoid TEXT NOT NULL DEFAULT '',
        hashtags TEXT NOT NULL DEFAULT '',
        guidance TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX brand_profiles_by_profile ON brand_profiles (profile_id);
    """,
    9: """
    -- Things an agent asked to do that only a person may allow (scoped_pipeline.py).
    CREATE TABLE approval_requests (
        id INTEGER PRIMARY KEY,
        profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        operation TEXT NOT NULL,
        payload TEXT NOT NULL,
        target_ids TEXT NOT NULL DEFAULT '[]',
        target_hash TEXT,
        status TEXT NOT NULL CHECK (status IN
            ('pending', 'approved', 'rejected', 'expired', 'executed', 'failed', 'stale')),
        requested_by TEXT NOT NULL DEFAULT 'api',
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        decided_at TEXT,
        decided_by TEXT,
        result TEXT
    );
    CREATE INDEX approval_requests_by_status ON approval_requests (profile_id, status);
    """,
}

# The approval state machine: status → statuses it may move to. Anything else is refused.
APPROVAL_TRANSITIONS = {
    "pending": ("approved", "rejected", "expired", "stale"),
    "approved": ("executed", "failed", "stale"),     # stale: the video changed between approval and execution
}

PROFILE_COLORS = ["#8a8984", "#b5835a", "#7a9a7e", "#7f8fb0", "#a77fa0", "#b07a7a", "#9a9a6a"]


@dataclass
class SearchHit:
    row_id: int
    spoken: bool          # matched words the creator said (the transcript)
    snippet: str          # the matching words in context, «highlighted»; transcript first


def fts_query(text: str) -> str | None:
    """A safe full-text query: every word must match (as a prefix: "sungl" finds
    "sunglasses"), and "quoted phrases" match as phrases. Punctuation is ignored."""
    import re
    parts = []
    for phrase, word in re.findall(r'"([^"]+)"|(\S+)', text or ""):
        tokens = re.findall(r"\w+", phrase or word)
        if phrase and tokens:
            parts.append('"' + " ".join(tokens) + '"')
        else:
            parts += [f'"{t}"*' for t in tokens]
    return " AND ".join(parts) or None


def _canonical(value) -> str:
    """Stable JSON (sorted keys, no spaces) so equal payloads compare equal."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


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


class ReadOnlyError(Exception):
    """A write was attempted on a database opened view only (someone else is editing it)."""


class Store:
    def __init__(self, path: Path | str, read_only: bool = False):
        self.path = str(path)
        self.read_only = read_only
        self.read_only_reason = ""      # e.g. "lydia on Lydia's MacBook Air is editing it"
        self._lock = threading.RLock()
        self._tls = threading.local()
        self._listeners: list[Callable[[Change], None]] = []
        self.conn = self._connect()
        if not read_only:
            self._migrate()
        elif self.conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            self.conn.close()
            raise ReadOnlyError("This database was made by a different version of Reelay. Open it for editing "
                                "once (when no one else has it open) so it can be updated.")

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False, timeout=30)
        else:
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:" and not self.read_only:
            from .dblocation import journal_mode
            conn.execute(f"PRAGMA journal_mode = {journal_mode(self.path)}")   # one file in synced folders
        return conn

    def reopen(self) -> None:
        """Reconnect (a sync service replaced the file underneath us) and tell everyone to reload."""
        with self._lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
            self.conn = self._connect()
        for entity in ("profile", "video", "post", "dispatch", "setting"):
            self._deliver([Change(entity, (), None, "remote")])

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

    # ================================================================ archive search
    def search(self, profile_id: int, text: str, limit: int = 200, archived: bool | None = False) -> list[SearchHit]:
        """Videos matching every word anywhere: title, original caption, what was SAID
        (transcript), the ticket's copy, topic, keywords and notes. Best matches first."""
        q = fts_query(text)
        if not q:
            return []
        sql = ("SELECT s.rowid AS id, snippet(video_search, -1, '«', '»', '…', 12) AS snip "
               "FROM video_search s JOIN videos v ON v.id = s.rowid "
               "WHERE video_search MATCH ? AND v.profile_id = ?")
        if archived is not None:
            sql += " AND v.archived_at IS NOT NULL" if archived else " AND v.archived_at IS NULL"
        try:
            rows = self.conn.execute(sql + " ORDER BY bm25(video_search, 5.0, 2.0, 3.0, 1.0, 1.0) LIMIT ?",
                                     (q, profile_id, limit)).fetchall()
            spoken = {r["id"]: r["snip"] for r in self.conn.execute(
                "SELECT rowid AS id, snippet(video_search, 2, '«', '»', '…', 12) AS snip FROM video_search "
                "WHERE video_search MATCH ?", (f"transcript : ({q})",)).fetchall()}
        except sqlite3.OperationalError:
            log.warning("Search query couldn't be run", exc_info=True)
            return []
        return [SearchHit(r["id"], r["id"] in spoken, spoken.get(r["id"]) or r["snip"]) for r in rows]

    def rebuild_search_index(self) -> int:
        """Re-index every video (repair; the triggers normally keep it current)."""
        with self.tx() as c:
            c.execute("DELETE FROM video_search")
            c.execute("INSERT INTO video_search (rowid, title, caption, transcript, copy, notes) "
                      "SELECT id, title, caption, transcript, copy, notes FROM video_search_source")
            return c.execute("SELECT COUNT(*) FROM video_search").fetchone()[0]

    # ================================================================ approval requests
    def create_approval(self, req: ApprovalRequest) -> ApprovalRequest:
        req.created_at = req.created_at or utcnow()
        req.status = "pending"
        with self.tx() as c:
            cur = c.execute("INSERT INTO approval_requests (profile_id, operation, payload, target_ids, target_hash, "
                            "status, requested_by, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (req.profile_id, req.operation, _canonical(req.payload), json.dumps(req.target_ids),
                             req.target_hash, req.status, req.requested_by, req.created_at, req.expires_at))
            req.id = cur.lastrowid
        self._emit("approval", (req.id,), req.profile_id)
        return req

    def get_approval(self, approval_id: int) -> ApprovalRequest | None:
        row = self.conn.execute("SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
        return self._approval(row) if row else None

    def list_approvals(self, profile_id: int | None = None, status: str | None = None,
                       limit: int = 100) -> list[ApprovalRequest]:
        sql, args = "SELECT * FROM approval_requests WHERE 1=1", []
        if profile_id is not None:
            sql += " AND profile_id=?"
            args.append(profile_id)
        if status is not None:
            sql += " AND status=?"
            args.append(status)
        rows = self.conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [self._approval(r) for r in rows]

    def find_pending_approval(self, profile_id: int, operation: str, payload: dict,
                              target_ids: list[int], now: str) -> ApprovalRequest | None:
        """An identical request still waiting (asking twice doesn't make two requests)."""
        row = self.conn.execute("SELECT * FROM approval_requests WHERE profile_id=? AND operation=? AND payload=? "
                                "AND target_ids=? AND status='pending' AND expires_at > ? ORDER BY id LIMIT 1",
                                (profile_id, operation, _canonical(payload), json.dumps(target_ids), now)).fetchone()
        return self._approval(row) if row else None

    def count_pending_approvals(self, profile_id: int, now: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM approval_requests WHERE profile_id=? AND status='pending' "
                                 "AND expires_at > ?", (profile_id, now)).fetchone()[0]

    def transition_approval(self, approval_id: int, to: str, decided_by: str | None = None,
                            result: dict | None = None) -> ApprovalRequest | None:
        """Move a request along the state machine, atomically. Returns the updated request, or None
        if it isn't in a state that allows `to` (e.g. someone else already decided)."""
        allowed_from = [frm for frm, tos in APPROVAL_TRANSITIONS.items() if to in tos]
        if not allowed_from:
            raise ValueError(f"no transition leads to {to!r}")
        now = utcnow()
        with self.tx() as c:
            row = c.execute(
                f"UPDATE approval_requests SET status=?, decided_at=COALESCE(decided_at, ?), "
                f"decided_by=COALESCE(?, decided_by), result=COALESCE(?, result) WHERE id=? "
                f"AND status IN ({','.join('?' * len(allowed_from))}) RETURNING *",
                (to, now, decided_by, json.dumps(result) if result is not None else None, approval_id,
                 *allowed_from)).fetchone()
        req = self._approval(row) if row else None
        if req:
            self._emit("approval", (req.id,), req.profile_id)
        return req

    def mark_approval_started(self, approval_id: int) -> bool:
        """Note, before carrying out an approved request, that it has started. If Reelay stops before
        it's finished, the next run sees the mark and never does it a second time."""
        with self.tx() as c:
            cur = c.execute("UPDATE approval_requests SET result=? WHERE id=? AND status='approved' AND result IS NULL",
                            (json.dumps({"started_at": utcnow()}), approval_id))
        return cur.rowcount == 1

    def expire_approvals(self, now: str) -> int:
        """Pending requests past their expiry become expired."""
        with self.tx() as c:
            rows = c.execute("UPDATE approval_requests SET status='expired', decided_at=?, decided_by='expiry' "
                             "WHERE status='pending' AND expires_at <= ? RETURNING id, profile_id",
                             (now, now)).fetchall()
        for r in rows:
            self._emit("approval", (r["id"],), r["profile_id"])
        return len(rows)

    @staticmethod
    def _approval(row) -> ApprovalRequest:
        return ApprovalRequest(profile_id=row["profile_id"], operation=row["operation"],
                               payload=json.loads(row["payload"]), target_ids=json.loads(row["target_ids"]),
                               target_hash=row["target_hash"], status=row["status"], requested_by=row["requested_by"],
                               created_at=row["created_at"], expires_at=row["expires_at"], decided_at=row["decided_at"],
                               decided_by=row["decided_by"],
                               result=json.loads(row["result"]) if row["result"] else None, id=row["id"])

    # ================================================================ brand voice
    def list_brand_profiles(self, profile_id: int) -> list[BrandProfile]:
        rows = self.conn.execute("SELECT * FROM brand_profiles WHERE profile_id=? ORDER BY name COLLATE NOCASE",
                                 (profile_id,)).fetchall()
        return [self._brand(r) for r in rows]

    def get_brand_profile(self, brand_id: int) -> BrandProfile | None:
        row = self.conn.execute("SELECT * FROM brand_profiles WHERE id=?", (brand_id,)).fetchone()
        return self._brand(row) if row else None

    def active_brand_profile(self, profile_id: int) -> BrandProfile | None:
        row = self.conn.execute("SELECT * FROM brand_profiles WHERE profile_id=? AND active=1 LIMIT 1",
                                (profile_id,)).fetchone()
        return self._brand(row) if row else None

    def save_brand_profile(self, brand: BrandProfile) -> BrandProfile:
        brand.name = brand.name.strip() or "Brand voice"
        now = utcnow()
        brand.updated_at = now
        with self.tx() as c:
            if brand.active:
                c.execute("UPDATE brand_profiles SET active=0 WHERE profile_id=?", (brand.profile_id,))
            values = (brand.name, brand.tone.strip(), brand.avoid.strip(), brand.hashtags.strip(),
                      brand.guidance.strip(), int(brand.active), now)
            if brand.id is None:
                brand.created_at = now
                cur = c.execute("INSERT INTO brand_profiles (name, tone, avoid, hashtags, guidance, active, "
                                "updated_at, profile_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                (*values, brand.profile_id, now))
                brand.id = cur.lastrowid
            else:
                c.execute("UPDATE brand_profiles SET name=?, tone=?, avoid=?, hashtags=?, guidance=?, active=?, "
                          "updated_at=? WHERE id=?", (*values, brand.id))
        self._emit("brand", (brand.id,), brand.profile_id)
        return brand

    def set_active_brand_profile(self, profile_id: int, brand_id: int | None) -> None:
        with self.tx() as c:
            c.execute("UPDATE brand_profiles SET active = (id IS ?) WHERE profile_id=?", (brand_id, profile_id))
        self._emit("brand", (brand_id,) if brand_id else (), profile_id)

    def delete_brand_profile(self, brand_id: int) -> None:
        row = self.conn.execute("SELECT profile_id FROM brand_profiles WHERE id=?", (brand_id,)).fetchone()
        with self.tx() as c:
            c.execute("DELETE FROM brand_profiles WHERE id=?", (brand_id,))
        self._emit("brand", (brand_id,), row[0] if row else None)

    @staticmethod
    def _brand(row) -> BrandProfile:
        return BrandProfile(profile_id=row["profile_id"], name=row["name"], tone=row["tone"], avoid=row["avoid"],
                            hashtags=row["hashtags"], guidance=row["guidance"], active=bool(row["active"]),
                            id=row["id"], created_at=row["created_at"], updated_at=row["updated_at"])

    # ================================================================ settings
    def get_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        if self.read_only:      # view only: preferences (theme, column widths…) just aren't saved
            return
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

    def load_videos(self, profile_id: int, stages: Iterable[str] | None = None,
                    archived: bool | None = False) -> list[VideoRow]:
        """archived: False = active videos only (the default), True = archived only, None = both."""
        sql, args = "SELECT * FROM videos WHERE profile_id = ?", [profile_id]
        if archived is not None:
            sql += " AND archived_at IS NOT NULL" if archived else " AND archived_at IS NULL"
        stages = list(stages or [])
        if stages:
            sql += f" AND stage IN ({','.join('?' * len(stages))})"
            args += stages
        rows = self.conn.execute(sql + " ORDER BY position, id", args).fetchall()
        return [self._video_row(r) for r in rows]

    def video_row_id(self, profile_id: int, platform: str, video_id: str) -> int | None:
        row = self.conn.execute("SELECT id FROM videos WHERE profile_id=? AND platform=? AND video_id=?",
                                (profile_id, platform, video_id)).fetchone()
        return row["id"] if row else None

    def get_video(self, row_id: int) -> VideoRow | None:
        row = self.conn.execute("SELECT * FROM videos WHERE id = ?", (row_id,)).fetchone()
        return self._video_row(row) if row else None

    def set_stage(self, row_ids: Iterable[int], stage: str, position: float | None = None) -> None:
        row_ids = list(row_ids)
        from .columns import is_stage
        if not is_stage(stage):
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
        self.update_many([(row_id, changes, post)])
        return self.get_video(row_id).ticket

    def update_many(self, edits: list[tuple[int, dict, Post | None]]) -> None:
        """Several tickets' edits (ticket fields, and the edited post or None) in
        ONE transaction with their revision logs: a bulk edit or a sheet import
        lands completely or not at all."""
        for _row_id, changes, _post in edits:
            unknown = sorted(set(changes) - set(TICKET_FIELDS))
            if unknown:
                raise ValueError(f"unknown ticket field(s): {', '.join(unknown)}")
        touched: dict[int, int] = {}
        with self.tx() as c:
            for row_id, changes, post in edits:
                row = c.execute("SELECT * FROM videos WHERE id=?", (row_id,)).fetchone()
                if row is None:
                    raise KeyError(row_id)
                old = self._ticket(row)
                diffs = [(f, getattr(old, f), v) for f, v in changes.items() if getattr(old, f) != v]
                if diffs:
                    c.execute(f"UPDATE videos SET {', '.join(f'{f}=?' for f, _o, _n in diffs)}, updated_at=? "
                              "WHERE id=?", (*[n for _f, _o, n in diffs], utcnow(), row_id))
                    self._log_revisions(c, row_id, diffs)
                if post is not None:
                    post.updated_at = utcnow()
                    self._write_post(c, post)
                touched[row_id] = row["profile_id"]
        for profile_id in set(touched.values()):
            ids = [r for r, p in touched.items() if p == profile_id]
            self._emit("video", ids, profile_id)
            self._emit("post", ids, profile_id)

    def set_archived(self, row_ids: Iterable[int], archived: bool) -> None:
        """Hide videos from the board and Download list (files and history stay),
        or bring them back. Logged as an "archived" revision."""
        row_ids = list(row_ids)
        now = utcnow()
        with self.tx() as c:
            for row_id in row_ids:
                old = c.execute("SELECT archived_at FROM videos WHERE id=?", (row_id,)).fetchone()
                if old is None:
                    raise KeyError(row_id)
                new = (old["archived_at"] or now) if archived else None
                if new != old["archived_at"]:
                    c.execute("UPDATE videos SET archived_at=?, updated_at=? WHERE id=?", (new, now, row_id))
                    self._log_revisions(c, row_id, [("archived", old["archived_at"], new)])
        if row_ids:
            self._emit("video", row_ids, self._profile_of_videos(row_ids))

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
            cur = c.execute("INSERT INTO manual_posts (video_row, platform, posted_at, url, note, origin, created_at, "
                            "kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (post.video_row, post.platform, post.posted_at, post.url, post.note, post.origin,
                             post.created_at, post.kind))
        post.id = cur.lastrowid
        self._emit("video", (post.video_row,), self._profile_of_videos([post.video_row]))
        return post

    def manual_posts(self, video_row: int) -> list[ManualPost]:
        rows = self.conn.execute("SELECT * FROM manual_posts WHERE video_row=? ORDER BY posted_at, id",
                                 (video_row,)).fetchall()
        return [ManualPost(id=r["id"], video_row=r["video_row"], platform=r["platform"], posted_at=r["posted_at"],
                           url=r["url"], note=r["note"], origin=r["origin"], created_at=r["created_at"],
                           kind=r["kind"]) for r in rows]

    def replace_manual_posts(self, video_row: int, platform: str, kinds: tuple[str, ...],
                             new: ManualPost | None = None) -> None:
        """Remove a platform's hand-made records of these kinds and optionally add
        one, in one transaction (a checkbox on the ticket)."""
        if new is not None:
            new.created_at, new.origin = utcnow(), self._state().origin
        with self.tx() as c:
            c.execute(f"DELETE FROM manual_posts WHERE video_row=? AND platform=? AND kind IN "
                      f"({','.join('?' * len(kinds))})", (video_row, platform, *kinds))
            if new is not None:
                cur = c.execute("INSERT INTO manual_posts (video_row, platform, posted_at, url, note, origin, "
                                "created_at, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (new.video_row, new.platform, new.posted_at, new.url, new.note, new.origin,
                                 new.created_at, new.kind))
                new.id = cur.lastrowid
        self._emit("video", (video_row,), self._profile_of_videos([video_row]))

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
                        ready_gate_exempt=bool(row["ready_gate_exempt"]), archived_at=row["archived_at"])

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

    def interrupt_abandoned(self, instance_id: str, stale_before: str, resumable: Iterable[str],
                            message: str) -> list[Dispatch]:
        """Claims left mid-send by another (crashed) run, or held too long by this one, on
        platforms that can't safely resume: mark them interrupted for a person to decide.
        (Resumable ones are left for recover_stale to requeue.)"""
        resumable = list(resumable)
        skip = f" AND platform NOT IN ({','.join('?' * len(resumable))})" if resumable else ""
        with self.tx() as c:
            before = c.execute(f"SELECT * FROM dispatches WHERE status=? AND (claimed_by IS NULL OR claimed_by != ? "
                               f"OR claimed_at < ?){skip}", (DispatchStatus.DISPATCHING, instance_id, stale_before,
                                                             *resumable)).fetchall()
            for r in before:
                c.execute("UPDATE dispatches SET status=?, claimed_at=NULL, claimed_by=NULL, error=?, updated_at=? "
                          "WHERE id=? AND status=?", (DispatchStatus.INTERRUPTED, message, utcnow(), r["id"],
                                                      DispatchStatus.DISPATCHING))
        held = [self._dispatch(r) for r in before]      # as they were: who held the claim, and since when
        for d in held:
            d.status, d.error = DispatchStatus.INTERRUPTED, message
        self._emit_dispatch_rows(held)
        return held

    def interrupt_missed(self, now: str, due_before: str, platforms: Iterable[str], message: str) -> list[Dispatch]:
        """Pending sends whose time passed while Reelay wasn't running (due before `due_before`)
        and that would now go live late: hold them for a person to decide. Sends that will still
        go live on time (a future publish_at, e.g. YouTube scheduling) and paused uploads of
        "publish now" posts are left to go."""
        platforms = list(platforms)
        if not platforms:
            return []
        with self.tx() as c:
            rows = c.execute(
                "UPDATE dispatches SET status=?, error=?, updated_at=? WHERE status=? AND due_at < ? "
                f"AND platform IN ({','.join('?' * len(platforms))}) "
                "AND ((publish_at IS NULL AND checkpoint != 'paused') OR publish_at < ?) RETURNING *",
                (DispatchStatus.INTERRUPTED, message, utcnow(), DispatchStatus.PENDING, due_before, *platforms,
                 now)).fetchall()
            c.execute("UPDATE dispatches SET checkpoint='' WHERE checkpoint='paused' AND status=?",
                      (DispatchStatus.PENDING,))
        done = [self._dispatch(r) for r in rows]
        self._emit_dispatch_rows(done)
        return done

    def interrupt_dispatch(self, dispatch_id: int, message: str) -> Dispatch | None:
        """One send whose outcome is unknown (e.g. the connection dropped as an upload finished)."""
        with self.tx() as c:
            row = c.execute("UPDATE dispatches SET status=?, claimed_at=NULL, claimed_by=NULL, error=?, "
                            "updated_at=? WHERE id=? RETURNING *",
                            (DispatchStatus.INTERRUPTED, message, utcnow(), dispatch_id)).fetchone()
        done = self._dispatch(row) if row else None
        self._emit_dispatch_rows([done] if done else [])
        return done

    def pause_dispatch(self, dispatch_id: int, message: str) -> Dispatch | None:
        """A send stopped cleanly before anything was posted (Reelay closing mid-upload):
        back to pending, to continue next time. Written before the upload is stopped."""
        with self.tx() as c:
            row = c.execute("UPDATE dispatches SET status=?, claimed_at=NULL, claimed_by=NULL, error=?, "
                            "checkpoint='paused', next_attempt_at=NULL, updated_at=? WHERE id=? AND status=? "
                            "RETURNING *", (DispatchStatus.PENDING, message, utcnow(), dispatch_id,
                                            DispatchStatus.DISPATCHING)).fetchone()
        done = self._dispatch(row) if row else None
        self._emit_dispatch_rows([done] if done else [])
        return done

    def requeue_dispatch(self, dispatch_id: int, due_at: str, publish_at: str | None) -> Dispatch | None:
        """Try an interrupted send again, now (a person decided it wasn't posted)."""
        with self.tx() as c:
            row = c.execute("UPDATE dispatches SET status=?, due_at=?, publish_at=?, next_attempt_at=NULL, "
                            "claimed_at=NULL, claimed_by=NULL, checkpoint='', error='', updated_at=? "
                            "WHERE id=? AND status=? RETURNING *",
                            (DispatchStatus.PENDING, due_at, publish_at, utcnow(), dispatch_id,
                             DispatchStatus.INTERRUPTED)).fetchone()
        done = self._dispatch(row) if row else None
        self._emit_dispatch_rows([done] if done else [])
        return done

    def interrupted_dispatches(self, profile_id: int | None = None) -> list[Dispatch]:
        sql, args = "SELECT d.* FROM dispatches d JOIN videos v ON v.id = d.video_row WHERE d.status = ?", \
            [DispatchStatus.INTERRUPTED]
        if profile_id is not None:
            sql += " AND v.profile_id = ?"
            args.append(profile_id)
        return [self._dispatch(r) for r in self.conn.execute(sql + " ORDER BY d.due_at, d.id", args).fetchall()]

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
                except sqlite3.OperationalError as exc:
                    if self.read_only and "readonly" in str(exc).replace(" ", "").replace("-", ""):
                        raise ReadOnlyError("This database is open view only"
                                            + (f": {self.read_only_reason}" if self.read_only_reason else "")
                                            + ". Changes can't be saved until you have it for editing.") from None
                    raise
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
            "tx", "origin", "subscribe", "_state", "_emit", "_deliver", "_connect", "reopen") and \
            not isinstance(_attr, staticmethod):
        setattr(Store, _name, _locked(_attr))
