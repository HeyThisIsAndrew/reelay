"""Shared fakes and a small world builder for API / dispatcher tests.
No network, no Qt event loop, no keychain."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reelay.accounts import AccountService
from reelay.flags import FLAG_PLATFORMS, env_var
from reelay.models import Account, Platform, VideoItem
from reelay.pipeline import Pipeline
from reelay.publishing import default_post
from reelay.store import Store
from reelay.tokens import MemoryTokenStore
from reelay.youtube import OAuthClient, Tokens

SECRETS = {
    "agent": "agent-token-0123456789abcdef",
    "bluesky": "bsky-app-pass-9999",
    "threads": "threads-token-SECRET-777",
    "facebook": "fb-page-token-SECRET-555",
}

# The first bytes of a real MP4 (an "ftyp" box), so Reelay treats the file as a video it may post.
FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\0" * 2036


class FakeUploads:
    """Stands in for UploadManager: records jobs; tests finish them by hand."""

    def __init__(self):
        self.completion_handler = None
        self.progress_handler = None
        self.jobs: list[tuple] = []
        self.cancelled: list[int] = []

    def enqueue(self, row_id, client, path, resource, thumbnail=None):
        if any(j[0] == row_id for j in self.jobs):
            raise ValueError("This video is already uploading.")
        self.thumbnails = {**getattr(self, "thumbnails", {}), row_id: thumbnail}
        self.jobs.append((row_id, client, Path(path), resource))

    def cancel(self, row_id):
        self.cancelled.append(row_id)
        return True

    def progress_of(self, row_id):
        return 0.0 if any(j[0] == row_id for j in self.jobs) else None

    def is_queued(self, row_id):
        return any(j[0] == row_id for j in self.jobs[1:])

    def queue_position(self, row_id):
        return next((n for n, j in enumerate(self.jobs) if j[0] == row_id), None)

    def finish(self, row_id, ok, payload):
        self.jobs = [j for j in self.jobs if j[0] != row_id]
        return self.completion_handler(row_id, ok, payload)


class FakeYouTubeClient:
    def __init__(self):
        self.statuses: dict = {}

    def video_statuses(self, ids):
        return {i: self.statuses[i] for i in ids if i in self.statuses}


@dataclass
class World:
    store: Store
    tokens: MemoryTokenStore
    accounts: AccountService
    uploads: FakeUploads
    pipeline: Pipeline
    profile: object
    youtube: Account
    rows: list[int]
    folder: Path
    youtube_client: FakeYouTubeClient


def make_world(tmp_path: Path, monkeypatch, downloaded: int = 3, fetched_only: int = 1,
               connect: tuple[str, ...] = (), ready: bool = True) -> World:
    store = Store(tmp_path / "world.db")
    tokens = MemoryTokenStore()
    accounts = AccountService(store, tokens)
    profile = store.create_profile("Lydia Livingood", tmp_path / "library")
    folder = Path(profile.output_folder)
    folder.mkdir(parents=True)
    items = []
    for n in range(downloaded + fetched_only):
        item = VideoItem(platform="TikTok", video_id=f"7{n:03d}", url=f"https://www.tiktok.com/@x/video/7{n:03d}",
                         title=f"caption number {n}...", upload_date=f"202610{10 - n:02d}", duration=20 + n,
                         description=f"caption number {n} is about letting go #growth #tag{n}",
                         view_count=1000 * (n + 1), like_count=10 * (n + 1), comment_count=n,
                         uploader="livingood.lifestyle", width=1080, height=1920)
        if n < downloaded:
            item.filename, item.folder = f"video{n}.mp4", str(folder)
            (folder / item.filename).write_bytes(FAKE_MP4)
        items.append(item)
    ids = store.save_videos(profile.id, items)
    accounts.set_oauth_client(OAuthClient("123-abc.apps.googleusercontent.com", "client-secret"))
    youtube = store.save_account(Account(profile_id=profile.id, platform="youtube", external_id="UCchannel",
                                         display_name="Lydia Livingood", handle="@livingood"))
    tokens.set(youtube.token_key, Tokens("access", "refresh", 9e12).to_json())
    client = FakeYouTubeClient()
    monkeypatch.setattr(accounts, "client_for", lambda account: client)
    uploads = FakeUploads()
    pipeline = Pipeline(store, accounts, uploads, instance_id="this-run")
    for platform in connect:
        connect_platform(accounts, profile.id, platform)
    if ready:   # tickets with every platform's own copy written, so the Ready gate passes
        for item in items[:downloaded]:
            row = ids[item.key]
            post = default_post(row, item, youtube.id)
            post.bluesky_text = post.threads_text = post.facebook_text = f"Post for {item.video_id} #growth"
            store.save_post(post)
    return World(store, tokens, accounts, uploads, pipeline, profile, youtube,
                 [ids[i.key] for i in items], folder, client)


def enable_publishing(monkeypatch, *platforms: str) -> None:
    """Turn automatic posting on the way a user would: REELAY_PUBLISH_<PLATFORM>=1."""
    for platform in platforms or FLAG_PLATFORMS:
        monkeypatch.setenv(env_var(platform), "1")


def disable_publishing(monkeypatch) -> None:
    for platform in FLAG_PLATFORMS:
        monkeypatch.delenv(env_var(platform), raising=False)


def connect_platform(accounts: AccountService, profile_id: int, platform: str) -> Account:
    identity, secret = {
        Platform.BLUESKY: ({"external_id": "did:plc:lydia", "display_name": "lydia.bsky.social",
                            "handle": "@lydia.bsky.social"},
                           {"handle": "lydia.bsky.social", "app_password": SECRETS["bluesky"]}),
        Platform.THREADS: ({"external_id": "17841400000", "display_name": "livingood", "handle": "@livingood"},
                           {"access_token": SECRETS["threads"], "user_id": "17841400000"}),
        Platform.FACEBOOK: ({"external_id": "1029384756", "display_name": "Livin'Good", "handle": ""},
                            {"page_id": "1029384756", "page_access_token": SECRETS["facebook"]}),
    }[platform]
    return accounts.connect_platform(profile_id, platform, identity, secret)


def approve_and_run(store, pipeline, response):
    """A person approves an agent's request (the 202 response), then the app carries it out.
    Returns the finished approval request (status executed / failed / stale)."""
    from reelay import approvals
    assert response.status_code == 202, response.text
    approval_id = response.json()["approval_id"]
    approvals.approve(store, approval_id, "test person")
    approvals.execute_approved(pipeline)
    return store.get_approval(approval_id)
