"""The content pipeline: every rule for moving videos through
Backlog → Staging → Scheduled → Published, editing posts, uploading and syncing.

One place for the rules, used by the desktop Board and (Phase 2) the
companion server. No Qt and no dialogs here: callers confirm with the user
first, then call in. Problems come back as `PipelineError`s with a stable
`code` the companion API can return.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .accounts import YOUTUBE, AccountService
from .library import Library
from .models import Account, Post, PostStatus, Profile, Stage, VideoItem, VideoRow
from .publishing import (build_resource, default_post, humanize_name, reconcile, stage_after_upload,
                         validate_post)
from .store import Store
from .text import local_text, short_title
from .youtube import YouTubeError, watch_url

log = logging.getLogger("reelay")

LOCKED_MESSAGE = "Scheduled and published videos are managed on YouTube; they can't be moved back."


class PipelineError(Exception):
    """A request the pipeline refuses. `code` is stable; `message` is for people."""

    def __init__(self, message: str, code: str = "invalid", problems: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.problems = problems or [message]


class UploadQueue(Protocol):
    completion_handler: object

    def enqueue(self, row_id: int, client, path: Path, resource: dict) -> None: ...
    def cancel(self, row_id: int) -> bool: ...
    def progress_of(self, row_id: int) -> float | None: ...
    def is_queued(self, row_id: int) -> bool: ...


@dataclass
class Card:
    """A video as shown on the board."""
    row: VideoRow
    post: Post | None = None
    account: Account | None = None
    progress: float | None = None   # upload percent while uploading
    queued: bool = False

    @property
    def id(self) -> int:
        return self.row.id

    @property
    def title(self) -> str:
        if self.post and self.post.title:
            return self.post.title
        return humanize_name(self.row.item.custom_name) or short_title(self.row.item.hook, 80)

    @property
    def uploading(self) -> bool:
        return bool(self.post and self.post.status == PostStatus.UPLOADING)


@dataclass
class SyncPlan:
    posts: list[Post]
    jobs: list[tuple[object, list[str]]]   # (YouTube client, remote video ids)
    problems: list[str]


class Pipeline:
    def __init__(self, store: Store, accounts: AccountService, uploads: UploadQueue):
        self.store = store
        self.accounts = accounts
        self.uploads = uploads
        uploads.completion_handler = self._on_upload_finished

    # ================================================================ reads
    def _row(self, row_id: int) -> VideoRow:
        row = self.store.get_video(row_id)
        if row is None:
            raise PipelineError("That video no longer exists.", "not_found")
        return row

    def _profile(self, profile_id: int) -> Profile:
        profile = self.store.get_profile(profile_id)
        if profile is None:
            raise PipelineError("That profile no longer exists.", "not_found")
        return profile

    def youtube_accounts(self, profile_id: int) -> list[Account]:
        return self.store.list_accounts(profile_id, YOUTUBE)

    def add_shorts_default(self) -> bool:
        return self.store.get_setting("add_shorts", "1") == "1"

    def video_path(self, row: VideoRow) -> Path | None:
        return row.item.path(self._profile(row.profile_id).output_folder)

    def board(self, profile_id: int) -> dict[str, list[Card]]:
        """Cards per stage, in board order. Fetched-but-not-downloaded videos are
        left out of the Backlog (they live on the Download page)."""
        profile = self._profile(profile_id)
        accounts = {a.id: a for a in self.store.list_accounts(profile_id)}
        columns: dict[str, list[Card]] = {stage: [] for stage in Stage.ALL}
        for row in self.store.load_videos(profile_id):
            row.item.refresh_download_status(profile.output_folder)
            if row.stage == Stage.BACKLOG and not row.item.is_downloaded:
                continue
            columns[row.stage].append(self._card(row, accounts))
        return columns

    def card(self, row_id: int) -> Card:
        row = self._row(row_id)
        row.item.refresh_download_status(self._profile(row.profile_id).output_folder)
        return self._card(row, {a.id: a for a in self.store.list_accounts(row.profile_id)})

    def _card(self, row: VideoRow, accounts: dict[int, Account]) -> Card:
        post = self.store.get_post(row.id)
        return Card(row=row, post=post, account=accounts.get(post.account_id) if post else None,
                    progress=self.uploads.progress_of(row.id), queued=self.uploads.is_queued(row.id))

    # ================================================================ moving cards
    def ensure_post(self, row_id: int) -> Post:
        """The video's post, creating a pre-filled draft if it has none."""
        post = self.store.get_post(row_id)
        if post is None:
            row = self._row(row_id)
            accounts = self.youtube_accounts(row.profile_id)
            post = self.store.save_post(default_post(row_id, row.item, accounts[0].id if accounts else None))
        return post

    def move(self, row_id: int, target: str, position: int | None = None) -> None:
        """Move a card to another column, or reorder it within its column.

        Moving to Scheduled isn't a move: it's an upload (see start_upload).
        Moving to Published means "posted outside Reelay"; confirm first."""
        if target not in Stage.ALL:
            raise PipelineError(f"Unknown stage “{target}”.", "invalid_stage")
        row = self._row(row_id)
        if row.stage == target:
            if position is not None:
                self._reorder(row, position)
            return
        post = self.store.get_post(row_id)
        if row.stage in (Stage.SCHEDULED, Stage.PUBLISHED) or (post and post.status == PostStatus.UPLOADING):
            raise PipelineError(LOCKED_MESSAGE, "locked")
        if target == Stage.SCHEDULED:
            raise PipelineError("Upload the video to schedule it.", "needs_upload")
        if target == Stage.STAGING:
            self.ensure_post(row_id)
        self.store.set_stage([row_id], target)
        if position is not None:
            self._reorder(self._row(row_id), position)

    def _reorder(self, row: VideoRow, position: int) -> None:
        order = [c.id for c in self.board(row.profile_id)[row.stage] if c.id != row.id]
        order.insert(max(0, min(position, len(order))), row.id)
        self.store.reorder(order)

    def prepare_upload(self, row_id: int) -> Post:
        """Get a card ready to schedule: make sure it has a draft and is in Staging."""
        post = self.ensure_post(row_id)
        if self._row(row_id).stage == Stage.BACKLOG:
            self.store.set_stage([row_id], Stage.STAGING)
        return post

    # ================================================================ posts
    def save_post(self, post: Post) -> Post:
        """Persist edits without changing status or stage."""
        self._row(post.video_row)
        return self.store.save_post(post)

    def save_draft(self, post: Post) -> Post:
        """Save as a draft; a Backlog card moves to Staging."""
        post = self.store.save_post(replace(post, status=PostStatus.DRAFT, error=""))
        if self._row(post.video_row).stage == Stage.BACKLOG:
            self.store.set_stage([post.video_row], Stage.STAGING)
        return post

    def upload_problems(self, post: Post) -> list[str]:
        """Everything stopping this post from uploading, in plain words."""
        row = self._row(post.video_row)
        problems = validate_post(post, self.video_path(row))
        current = self.store.get_post(post.video_row)
        if current is not None and current.status in (PostStatus.UPLOADING, PostStatus.SCHEDULED,
                                                      PostStatus.PUBLISHED):
            problems.insert(0, "This video has already been uploaded.")
        if post.account_id is not None:
            account = self.store.get_account(post.account_id)
            if account is None:
                problems.append("That YouTube channel is no longer connected.")
            else:
                try:
                    self.accounts.client_for(account)
                except YouTubeError as exc:
                    problems.append(str(exc))
        return problems

    def describe_upload(self, post: Post) -> str:
        """One line for a confirmation prompt."""
        account = self.store.get_account(post.account_id) if post.account_id else None
        when = (f"goes live {local_text(post.publish_at)}" if post.publish_at
                else f"is published immediately as {post.privacy.capitalize()}")
        return f"Upload “{post.title}” to {account.display_name if account else 'YouTube'}? It {when}."

    def start_upload(self, post: Post, add_shorts: bool | None = None) -> Post:
        """Validate, mark as uploading, move to Scheduled and queue the upload.

        Raises PipelineError(code="invalid_post") with every problem listed."""
        problems = self.upload_problems(post)
        if problems:
            current = self.store.get_post(post.video_row)
            if current is None or current.status in (PostStatus.DRAFT, PostStatus.FAILED):
                self.store.save_post(replace(post, status=PostStatus.DRAFT))  # keep what was typed
            raise PipelineError("This post can't be uploaded yet.", "invalid_post", problems)
        row = self._row(post.video_row)
        client = self.accounts.client_for(self.store.get_account(post.account_id))
        add_shorts = self.add_shorts_default() if add_shorts is None else add_shorts
        post = self.store.save_post(replace(post, status=PostStatus.UPLOADING, error=""))
        self.store.set_stage([row.id], Stage.SCHEDULED)
        try:
            self.uploads.enqueue(row.id, client, self.video_path(row), build_resource(post, add_shorts))
        except ValueError as exc:
            raise PipelineError(str(exc), "already_uploading") from exc
        return post

    def cancel_upload(self, row_id: int) -> bool:
        return self.uploads.cancel(row_id)

    def complete_upload(self, row_id: int, resource: dict) -> Post | None:
        post = self.store.get_post(row_id)
        if post is None:
            return None
        remote_id = resource.get("id", "")
        post = self.store.save_post(replace(
            post, remote_id=remote_id, remote_url=watch_url(remote_id) if remote_id else "",
            status=PostStatus.SCHEDULED if post.publish_at else PostStatus.PUBLISHED, error=""))
        self.store.set_stage([row_id], stage_after_upload(post))
        return post

    def fail_upload(self, row_id: int, reason: str) -> Post | None:
        post = self.store.get_post(row_id)
        if post is None:
            return None
        post = self.store.save_post(replace(post, status=PostStatus.FAILED, error=reason))
        self.store.set_stage([row_id], Stage.STAGING)
        return post

    def _on_upload_finished(self, row_id: int, ok: bool, payload) -> str:
        if ok:
            post = self.complete_upload(row_id, payload)
            if post is None:
                return "Upload finished."
            when = f" It goes live {local_text(post.publish_at)}." if post.publish_at else ""
            return f"Uploaded “{post.title}”.{when}"
        self.fail_upload(row_id, str(payload))
        return f"Upload failed: {payload}"

    # ================================================================ YouTube sync
    def sync_plan(self, profile_id: int) -> SyncPlan:
        """What to ask YouTube about: scheduled posts, grouped per channel."""
        posts = [p for p in self.store.posts_with_status(profile_id, [PostStatus.SCHEDULED]) if p.remote_id]
        jobs, problems = [], []
        for account_id in sorted({p.account_id for p in posts if p.account_id}):
            account = self.store.get_account(account_id)
            if account is None:
                continue
            try:
                client = self.accounts.client_for(account)
            except YouTubeError as exc:
                problems.append(str(exc))
                continue
            jobs.append((client, [p.remote_id for p in posts if p.account_id == account_id]))
        return SyncPlan(posts, jobs, problems)

    @staticmethod
    def fetch_remote(plan: SyncPlan) -> dict[str, dict | None]:
        """Network part of a sync. Safe to run on a background thread."""
        out: dict[str, dict | None] = {}
        for client, ids in plan.jobs:
            statuses = client.video_statuses(ids)
            out.update({vid: statuses.get(vid) for vid in ids})
        return out

    def apply_sync(self, plan: SyncPlan, remote: dict[str, dict | None], now: datetime | None = None) -> int:
        """Apply YouTube's answers. Returns how many videos went live."""
        now = now or datetime.now(timezone.utc)
        published = 0
        for post in plan.posts:
            if post.remote_id not in remote:
                continue  # that channel wasn't checked
            status, note = reconcile(post, remote[post.remote_id], now)
            if status != post.status or note != post.error:
                self.store.save_post(replace(post, status=status, error=note))
            if status == PostStatus.PUBLISHED:
                self.store.set_stage([post.video_row], Stage.PUBLISHED)
                published += 1
        return published

    # ================================================================ video edits
    def set_custom_name(self, row_id: int, name: str) -> VideoItem:
        """Rename a video's custom name (the file is renamed later via Apply Names).
        Keeps the folder's metadata sidecar and manifest.md in step."""
        row = self._row(row_id)
        row.item.custom_name = name.strip()
        self.store.save_video(row.profile_id, row.item)
        profile = self._profile(row.profile_id)
        row.item.refresh_download_status(profile.output_folder)
        if row.item.is_downloaded:
            self._update_library(profile, row.item)
        return row.item

    def set_notes(self, row_id: int, notes: str) -> None:
        self._row(row_id)
        self.store.set_notes(row_id, notes)

    def _update_library(self, profile: Profile, item: VideoItem) -> None:
        folder = Path(item.folder or profile.output_folder)
        library = Library(folder)
        try:
            library.save(item)
            items = []
            for row in self.store.load_videos(profile.id):
                row.item.refresh_download_status(profile.output_folder)
                if Path(row.item.folder or profile.output_folder) == folder:
                    items.append(row.item)
            library.write_manifest(items, [profile.last_source])
        except OSError as exc:
            log.warning("Couldn't update library files in %s: %s", folder, exc)

