"""Phase 1: store change feed, Pipeline rules (no Qt), UploadManager queue."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from reelay.models import Account, Post, PostStatus, Stage, Status, VideoItem
from reelay.pipeline import Pipeline, PipelineError
from reelay.store import Change, Store
from reelay.youtube import YouTubeError

FUTURE = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ fakes
class FakeUploads:
    def __init__(self):
        self.completion_handler = None
        self.jobs = []
        self.cancelled = []

    def enqueue(self, row_id, client, path, resource):
        if any(j[0] == row_id for j in self.jobs):
            raise ValueError("This video is already uploading.")
        self.jobs.append((row_id, client, path, resource))

    def cancel(self, row_id):
        self.cancelled.append(row_id)
        return True

    def progress_of(self, row_id):
        return 0.0 if any(j[0] == row_id for j in self.jobs) else None

    def is_queued(self, row_id):
        return False

    def finish(self, row_id, ok, payload):
        self.jobs = [j for j in self.jobs if j[0] != row_id]
        return self.completion_handler(row_id, ok, payload)


class FakeClient:
    def __init__(self, statuses=None):
        self.statuses = statuses or {}

    def video_statuses(self, ids):
        return {i: self.statuses[i] for i in ids if i in self.statuses}


class FakeAccounts:
    def __init__(self, broken=False):
        self.client = FakeClient()
        self.broken = broken

    def client_for(self, account):
        if self.broken:
            raise YouTubeError("Reconnect Lydia Channel: its sign-in is missing from the keychain.")
        return self.client


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "p.db")
    profile = store.create_profile("Lydia", tmp_path / "lib")
    folder = Path(profile.output_folder)
    folder.mkdir(parents=True)
    items = []
    for n in range(4):
        item = VideoItem(platform="TikTok", video_id=str(n), url=f"https://t/{n}", upload_date="20261001",
                         description=f"caption {n} #tag", title=f"caption {n}...")
        if n < 3:  # three downloaded, one only fetched
            item.filename, item.folder = f"{n}.mp4", str(folder)
            (folder / item.filename).write_bytes(b"v")
        items.append(item)
    ids = store.save_videos(profile.id, items)
    account = store.save_account(Account(profile_id=profile.id, platform="youtube", external_id="UC1",
                                         display_name="Lydia Channel"))
    uploads, accounts = FakeUploads(), FakeAccounts()
    pipeline = Pipeline(store, accounts, uploads)
    rows = [ids[i.key] for i in items]
    yield pipeline, store, uploads, accounts, profile, account, rows
    store.close()


# ------------------------------------------------------------------ change feed
def test_change_feed_reports_writes_with_profile_and_origin(env):
    pipeline, store, *_rest = env
    profile, rows = env[4], env[6]
    seen: list[Change] = []
    unsubscribe = store.subscribe(seen.append)
    store.set_stage([rows[0]], Stage.STAGING)
    with store.origin("remote"):
        store.set_notes(rows[1], "film a hook")
    store.save_post(Post(video_row=rows[0], title="t"))
    store.set_setting("theme", "black")
    assert [(c.entity, c.ids, c.profile_id, c.origin) for c in seen] == [
        ("video", (rows[0],), profile.id, "local"),
        ("video", (rows[1],), profile.id, "remote"),
        ("post", (rows[0],), profile.id, "local"),
        ("setting", ("theme",), None, "local"),
    ]
    unsubscribe()
    store.set_setting("theme", "dark")
    assert len(seen) == 4


def test_broken_listener_never_breaks_writes(env):
    store, rows = env[1], env[6]
    store.subscribe(lambda change: 1 / 0)
    store.set_stage([rows[0]], Stage.STAGING)
    assert store.get_video(rows[0]).stage == Stage.STAGING


def test_account_and_profile_changes_are_announced(env):
    store, profile, account = env[1], env[4], env[5]
    seen = []
    store.subscribe(seen.append)
    store.delete_account(account.id)
    store.update_profile(profile)
    assert [(c.entity, c.profile_id) for c in seen] == [("account", profile.id), ("profile", profile.id)]


# ------------------------------------------------------------------ board + moves
def test_board_hides_undownloaded_backlog_and_orders_cards(env):
    pipeline, profile = env[0], env[4]
    columns = pipeline.board(profile.id)
    assert [c.row.item.video_id for c in columns[Stage.BACKLOG]] == ["0", "1", "2"]
    assert all(not columns[s] for s in (Stage.STAGING, Stage.SCHEDULED, Stage.PUBLISHED))


def test_move_rules(env):
    pipeline, store, rows = env[0], env[1], env[6]
    pipeline.move(rows[0], Stage.STAGING)
    assert store.get_video(rows[0]).stage == Stage.STAGING
    assert store.get_post(rows[0]).title == "Caption 0"  # draft created (hashtags stay in tags)
    assert store.get_post(rows[0]).tags == ["tag"]
    assert store.get_post(rows[0]).account_id is not None
    with pytest.raises(PipelineError) as err:
        pipeline.move(rows[1], Stage.SCHEDULED)
    assert err.value.code == "needs_upload"
    pipeline.move(rows[1], Stage.PUBLISHED)  # "posted outside Reelay"
    with pytest.raises(PipelineError) as err:
        pipeline.move(rows[1], Stage.BACKLOG)
    assert err.value.code == "locked"
    with pytest.raises(PipelineError) as err:
        pipeline.move(rows[0], "somewhere")
    assert err.value.code == "invalid_stage"
    with pytest.raises(PipelineError) as err:
        pipeline.move(99999, Stage.STAGING)
    assert err.value.code == "not_found"


def test_reorder_within_column(env):
    pipeline, profile, rows = env[0], env[4], env[6]
    pipeline.move(rows[2], Stage.BACKLOG, position=0)
    assert [c.id for c in pipeline.board(profile.id)[Stage.BACKLOG]] == [rows[2], rows[0], rows[1]]
    pipeline.move(rows[2], Stage.BACKLOG, position=99)
    assert [c.id for c in pipeline.board(profile.id)[Stage.BACKLOG]] == [rows[0], rows[1], rows[2]]


def test_save_draft_moves_backlog_card_to_staging(env):
    pipeline, store, rows = env[0], env[1], env[6]
    post = pipeline.save_draft(Post(video_row=rows[0], title="Hello", status=PostStatus.FAILED, error="old"))
    assert post.status == PostStatus.DRAFT and post.error == ""
    assert store.get_video(rows[0]).stage == Stage.STAGING


# ------------------------------------------------------------------ uploads
def _ready_post(env, row, **kw):
    account = env[5]
    return Post(video_row=row, account_id=account.id, title="Ready", publish_at=FUTURE, **kw)


def test_start_upload_queues_and_marks_scheduled(env):
    pipeline, store, uploads, rows = env[0], env[1], env[2], env[6]
    pipeline.start_upload(_ready_post(env, rows[0]), add_shorts=True)
    assert store.get_post(rows[0]).status == PostStatus.UPLOADING
    assert store.get_video(rows[0]).stage == Stage.SCHEDULED
    row_id, _client, path, resource = uploads.jobs[0]
    assert row_id == rows[0] and path.name == "0.mp4"
    assert resource["status"]["publishAt"] == FUTURE and resource["snippet"]["description"].endswith("#Shorts")
    with pytest.raises(PipelineError):  # can't upload twice
        pipeline.start_upload(_ready_post(env, rows[0]))


def test_start_upload_lists_every_problem_and_keeps_typed_text(env):
    pipeline, store, uploads, rows = env[0], env[1], env[2], env[6]
    with pytest.raises(PipelineError) as err:
        pipeline.start_upload(Post(video_row=rows[3], title="Typed title"))  # not downloaded, no channel
    assert err.value.code == "invalid_post"
    assert any("channel" in p for p in err.value.problems) and any("missing" in p for p in err.value.problems)
    assert store.get_post(rows[3]).title == "Typed title" and not uploads.jobs


def test_signed_out_channel_blocks_upload(env):
    pipeline, rows, accounts = env[0], env[6], env[3]
    accounts.broken = True
    assert any("Reconnect" in p for p in pipeline.upload_problems(_ready_post(env, rows[0])))


def test_upload_completion_and_failure(env):
    pipeline, store, uploads, rows = env[0], env[1], env[2], env[6]
    pipeline.start_upload(_ready_post(env, rows[0]))
    message = uploads.finish(rows[0], True, {"id": "YT9"})
    post = store.get_post(rows[0])
    assert post.status == PostStatus.SCHEDULED and post.remote_url.endswith("YT9") and "Uploaded" in message

    pipeline.start_upload(Post(video_row=rows[1], account_id=env[5].id, title="Now"))  # publish immediately
    uploads.finish(rows[1], True, {"id": "YT10"})
    assert store.get_video(rows[1]).stage == Stage.PUBLISHED

    pipeline.start_upload(_ready_post(env, rows[2]))
    message = uploads.finish(rows[2], False, "Network problem")
    assert store.get_post(rows[2]).status == PostStatus.FAILED and store.get_video(rows[2]).stage == Stage.STAGING
    assert "Network problem" in message


def test_sync_publishes_and_flags_stuck_private_videos(env):
    pipeline, store, uploads, accounts, profile, account, rows = env
    for row, remote in ((rows[0], "A"), (rows[1], "B")):
        pipeline.start_upload(_ready_post(env, row))
        uploads.finish(row, True, {"id": remote})
    accounts.client.statuses = {"A": {"privacy": "public"}, "B": {"privacy": "private", "publish_at": None}}
    plan = pipeline.sync_plan(profile.id)
    assert plan.jobs and sorted(plan.jobs[0][1]) == ["A", "B"]
    after_publish_time = datetime.now(timezone.utc) + timedelta(days=4)  # both were due in 3 days
    assert pipeline.apply_sync(plan, pipeline.fetch_remote(plan), now=after_publish_time) == 1
    assert store.get_video(rows[0]).stage == Stage.PUBLISHED
    assert "audit" in store.get_post(rows[1]).error


def test_cancel_goes_through_the_queue(env):
    pipeline, uploads, rows = env[0], env[2], env[6]
    assert pipeline.cancel_upload(rows[0]) and uploads.cancelled == [rows[0]]


# ------------------------------------------------------------------ video edits
def test_custom_name_updates_store_sidecar_and_manifest(env):
    pipeline, store, profile, rows = env[0], env[1], env[4], env[6]
    pipeline.set_custom_name(rows[0], "  letting-go  ")
    assert store.get_video(rows[0]).item.custom_name == "letting-go"
    folder = Path(profile.output_folder)
    assert '"custom_name": "letting-go"' in (folder / "metadata" / "TikTok_0.json").read_text()
    assert "| letting-go |" in (folder / "manifest.md").read_text()
    pipeline.set_notes(rows[0], "needs captions")
    assert store.get_video(rows[0]).notes == "needs captions"


# ------------------------------------------------------------------ UploadManager (Qt)
class FakeWorker:
    """Stands in for UploadWorker: finishes as soon as it starts."""
    def __new__(cls, row_id, client, path, resource, parent):
        from PySide6.QtCore import QObject, Signal

        class W(QObject):
            progress = Signal(int, float)
            done = Signal(int, dict)
            failed = Signal(int, str)
            finished = Signal()

            def __init__(self):
                super().__init__(parent)
                self.row_id, self.cancelled = row_id, False
                W.started.append(row_id)

            def start(self):
                self.progress.emit(row_id, 50.0)
                if client == "fail":
                    self.failed.emit(row_id, "boom")
                else:
                    self.done.emit(row_id, {"id": f"V{row_id}"})
                self.finished.emit()

            def cancel(self):
                self.cancelled = True

            def wait(self, *_):
                return True
        W.started = cls.started
        return W()
    started: list = []


@pytest.mark.gui
def test_upload_manager_runs_jobs_in_order(qtbot):
    from reelay.uploads import UploadManager
    FakeWorker.started = []
    manager = UploadManager(worker_factory=FakeWorker)
    handled, finished, progress = [], [], []
    manager.completion_handler = lambda row, ok, payload: handled.append((row, ok, payload)) or f"msg {row}"
    manager.finished.connect(lambda row, ok, msg: finished.append((row, ok, msg)))
    manager.progress.connect(lambda row, pct: progress.append((row, pct)))
    manager.enqueue(1, "ok", Path("a.mp4"), {})
    manager.enqueue(2, "fail", Path("b.mp4"), {})
    qtbot.waitUntil(lambda: len(finished) == 2, timeout=3000)
    assert FakeWorker.started == [1, 2]
    assert handled == [(1, True, {"id": "V1"}), (2, False, "boom")]
    assert finished == [(1, True, "msg 1"), (2, False, "msg 2")]
    assert (1, 50.0) in progress and not manager.busy


@pytest.mark.gui
def test_upload_manager_cancel_queued_job(qtbot):
    from reelay.uploads import CANCELLED, UploadManager
    manager = UploadManager(worker_factory=FakeWorker)
    manager._worker = object.__new__(type("Busy", (), {"row_id": 1, "cancel": lambda self: None}))  # occupy
    manager._kick.disconnect(manager._start_next)  # keep the fake "busy" worker in place
    finished = []
    manager.finished.connect(lambda row, ok, msg: finished.append((row, ok, msg)))
    manager.enqueue(2, "ok", Path("b.mp4"), {})
    assert manager.is_queued(2) and manager.progress_of(2) == 0.0
    with pytest.raises(ValueError):
        manager.enqueue(2, "ok", Path("b.mp4"), {})
    assert manager.cancel(2) and finished == [(2, False, CANCELLED)]
    assert not manager.is_queued(2) and manager.progress_of(2) is None
    assert not manager.cancel(42)


# ------------------------------------------------------------------ cross-surface refresh
@pytest.mark.gui
def test_remote_edits_show_up_in_the_desktop_ui(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from reelay.gui.app import create_window
    from reelay.paths import db_path
    from reelay.tokens import MemoryTokenStore
    store = Store(db_path())
    profile = store.create_profile("Lydia", tmp_path / "lib2")
    store.set_active_profile(profile.id)
    folder = Path(profile.output_folder)
    folder.mkdir(parents=True)
    item = VideoItem(platform="TikTok", video_id="7", url="https://t/7", upload_date="20261001",
                     filename="7.mp4", folder=str(folder), status=Status.DOWNLOADED)
    (folder / "7.mp4").write_bytes(b"v")
    row = store.save_video(profile.id, item)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    window.go("board")
    assert window.board.columns[Stage.BACKLOG].model.rowCount() == 1

    with store.origin("remote"):  # what the companion server will do
        window.pipeline.set_custom_name(row, "from-the-phone")
        window.pipeline.move(row, Stage.STAGING)
    qtbot.waitUntil(lambda: window.board.columns[Stage.STAGING].model.rowCount() == 1, timeout=2000)
    assert window.download.model.find("TikTok_7").custom_name == "from-the-phone"
    window.close()
    store.close()
