"""SQLite persistence: profiles, connected accounts, videos, board stages, posts.

OAuth tokens are deliberately NOT stored here; they live in the OS keychain
(see tokens.py), so copying the database never leaks account access.

The Store is used from the GUI thread only. Background workers send results
back via signals and the GUI thread writes them.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .models import Account, Post, PostStatus, Profile, Stage, VideoItem, VideoRow

SCHEMA_VERSION = 1

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
}

PROFILE_COLORS = ["#8a8984", "#b5835a", "#7a9a7e", "#7f8fb0", "#a77fa0", "#b07a7a", "#9a9a6a"]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

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
        return profile

    def update_profile(self, profile: Profile) -> None:
        with self.tx() as c:
            c.execute("UPDATE profiles SET name=?, color=?, output_folder=?, template=?, quality=?, "
                      "cookies_browser=?, last_source=? WHERE id=?",
                      (profile.name, profile.color, profile.output_folder, profile.template,
                       profile.quality, profile.cookies_browser, profile.last_source, profile.id))

    def delete_profile(self, profile_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        if self.get_setting("active_profile") == str(profile_id):
            self.set_setting("active_profile", "")

    def active_profile(self, default_folder: Path) -> Profile:
        """The profile last used, creating a first one if none exist."""
        active = self.get_setting("active_profile")
        profile = self.get_profile(int(active)) if active.isdigit() else None
        if profile is None:
            profiles = self.list_profiles()
            profile = profiles[0] if profiles else self.create_profile("My Channel", default_folder)
            self.set_active_profile(profile.id)
        return profile

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
        return account

    def delete_account(self, account_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM accounts WHERE id = ?", (account_id,))

    def account_in_use_elsewhere(self, account: Account) -> bool:
        """Is the same remote account connected in another profile (shares its token)?"""
        row = self.conn.execute("SELECT COUNT(*) FROM accounts WHERE platform=? AND external_id=? AND id != ?",
                                (account.platform, account.external_id, account.id)).fetchone()
        return row[0] > 0

    @staticmethod
    def _account(row: sqlite3.Row) -> Account:
        return Account(id=row["id"], profile_id=row["profile_id"], platform=row["platform"],
                       external_id=row["external_id"], display_name=row["display_name"],
                       handle=row["handle"], avatar_url=row["avatar_url"], connected_at=row["connected_at"])

    # ================================================================ videos
    def save_videos(self, profile_id: int, items: Iterable[VideoItem]) -> dict[str, int]:
        """Insert or update videos (metadata only; stage/notes untouched).

        Returns {item.key: row id}."""
        now = utcnow()
        ids: dict[str, int] = {}
        with self.tx() as c:
            for item in items:
                c.execute(
                    "INSERT INTO videos (profile_id, platform, video_id, data, updated_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(profile_id, platform, video_id) DO UPDATE SET data=excluded.data, "
                    "updated_at=excluded.updated_at",
                    (profile_id, item.platform, item.video_id, json.dumps(item.to_record()), now))
                row = c.execute("SELECT id FROM videos WHERE profile_id=? AND platform=? AND video_id=?",
                                (profile_id, item.platform, item.video_id)).fetchone()
                ids[item.key] = row["id"]
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

    def reorder(self, ordered_row_ids: list[int]) -> None:
        with self.tx() as c:
            for pos, row_id in enumerate(ordered_row_ids, 1):
                c.execute("UPDATE videos SET position=? WHERE id=?", (float(pos), row_id))

    def set_notes(self, row_id: int, notes: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE videos SET notes=?, updated_at=? WHERE id=?", (notes, utcnow(), row_id))

    @staticmethod
    def _video_row(row: sqlite3.Row) -> VideoRow:
        item = VideoItem.from_record(json.loads(row["data"]))
        return VideoRow(id=row["id"], item=item, stage=row["stage"], position=row["position"], notes=row["notes"])

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
        values = (post.account_id, post.title, post.description, json.dumps(post.tags), post.privacy,
                  post.publish_at, int(post.made_for_kids), post.status, post.remote_id, post.remote_url,
                  post.error, post.updated_at)
        with self.tx() as c:
            c.execute(
                "INSERT INTO posts (account_id, title, description, tags, privacy, publish_at, made_for_kids, "
                "status, remote_id, remote_url, error, updated_at, video_row) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(video_row) DO UPDATE SET account_id=excluded.account_id, title=excluded.title, "
                "description=excluded.description, tags=excluded.tags, privacy=excluded.privacy, "
                "publish_at=excluded.publish_at, made_for_kids=excluded.made_for_kids, status=excluded.status, "
                "remote_id=excluded.remote_id, remote_url=excluded.remote_url, error=excluded.error, "
                "updated_at=excluded.updated_at", (*values, post.video_row))
            post.id = c.execute("SELECT id FROM posts WHERE video_row=?", (post.video_row,)).fetchone()["id"]
        return post

    def reset_interrupted_uploads(self) -> int:
        """Uploads that were running when the app quit go back to draft."""
        with self.tx() as c:
            cur = c.execute("UPDATE posts SET status=?, error=? WHERE status=?",
                            (PostStatus.DRAFT, "Upload was interrupted. Try again.", PostStatus.UPLOADING))
        return cur.rowcount

    @staticmethod
    def _post(row: sqlite3.Row) -> Post:
        return Post(id=row["id"], video_row=row["video_row"], account_id=row["account_id"], title=row["title"],
                    description=row["description"], tags=json.loads(row["tags"] or "[]"),
                    privacy=row["privacy"], publish_at=row["publish_at"],
                    made_for_kids=bool(row["made_for_kids"]), status=row["status"], remote_id=row["remote_id"],
                    remote_url=row["remote_url"], error=row["error"], updated_at=row["updated_at"])
