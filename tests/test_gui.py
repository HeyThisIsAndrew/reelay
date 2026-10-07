"""End-to-end GUI tests (headless). Network pieces are replaced with fakes."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox

from reelay.models import Account, Post, PostStatus, Stage, VideoItem
from reelay.paths import db_path
from reelay.store import Store
from reelay.tokens import MemoryTokenStore
from reelay.youtube import Tokens

pytestmark = pytest.mark.gui


def make_items(n=5, platform="TikTok"):
    return [VideoItem(platform=platform, video_id=str(1000 + i), url=f"https://www.tiktok.com/@x/video/{1000 + i}",
                      title=f"caption number {i}...", description=f"caption number {i} is great #tag{i}",
                      upload_date=f"202610{10 - i:02d}", duration=10 + i, view_count=100 * i)
            for i in range(n)]


@pytest.fixture
def app_window(qtbot, tmp_path, monkeypatch):
    from reelay.gui import app as gui_app
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    store = Store(db_path())
    tokens = MemoryTokenStore()
    profile = store.create_profile("Lydia", tmp_path / "lib")
    store.set_active_profile(profile.id)
    window = gui_app.create_window(QApplication.instance(), store, tokens)
    qtbot.addWidget(window)
    window.show()
    yield window, store, tokens
    window.close()
    store.close()


def fetch_fake(window, qtbot, monkeypatch, items):
    import reelay.gui.workers as workers
    monkeypatch.setattr(workers, "list_videos", lambda url, opts, limit=None: ("x", [VideoItem(**{
        k: v for k, v in i.__dict__.items()}) for i in items]))
    page = window.download
    page.url.setText("https://www.tiktok.com/@livingood.lifestyle")
    page._fetch()
    qtbot.waitUntil(lambda: page.fetch_btn.isEnabled() and page.model.rowCount() == len(items), timeout=5000)
    return page


def fake_download(monkeypatch):
    import reelay.gui.workers as workers
    from reelay.models import Status

    def download_item(item, folder, quality, opts, name_for, on_progress=None, cancel=None):
        result = VideoItem(**item.__dict__)
        name = name_for(result) + ".mp4"
        (Path(folder) / name).write_bytes(b"video")
        result.filename, result.status = name, Status.DOWNLOADED
        return result

    monkeypatch.setattr(workers, "download_item", download_item)


# ------------------------------------------------------------------ shell
def test_pages_and_themes(app_window, qtbot):
    window, store, _ = app_window
    from reelay.gui import theme
    for key in ("download", "board", "accounts", "settings"):
        window.go(key)
        assert window.nav_buttons[key].isChecked()
    for name in ("light", "black", "dark"):
        window.set_theme(name)
        assert theme.P.name == name and store.get_setting("theme") == name


def test_profile_create_switch_delete(app_window, monkeypatch):
    window, store, _ = app_window
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Andrew's Channel", True))
    window.new_profile()
    assert window.profile.name == "Andrew's Channel"
    assert Path(window.profile.output_folder).name == "Andrew's Channel"
    assert window.download.model.rowCount() == 0
    lydia = next(p for p in store.list_profiles() if p.name == "Lydia")
    window.switch_profile(lydia)
    assert store.active_profile(Path(".")).id == lydia.id
    window.delete_profile()
    assert [p.name for p in store.list_profiles()] == ["Andrew's Channel"]
    assert window.profile.name == "Andrew's Channel"


# ------------------------------------------------------------------ download page
def test_fetch_name_download_and_manifest(app_window, qtbot, monkeypatch):
    window, store, _ = app_window
    page = fetch_fake(window, qtbot, monkeypatch, make_items(4))
    assert len(store.load_videos(window.profile.id)) == 4  # fetched list is remembered

    # include only the newest 2, name them from captions, use the custom template
    page.model.set_checked([i.key for i in page.model.items()], False)
    newest = sorted(page.model.items(), key=lambda i: i.upload_date, reverse=True)[:2]
    page.model.set_checked([i.key for i in newest], True)
    page.model.set_custom_names({i.key: f"name {i.video_id}" for i in newest})
    page.template.setEditText("{date}_{custom}")
    assert page.model.planned_filename(newest[0]) == "20261010_name 1000.mp4"

    fake_download(monkeypatch)
    page._start_download()
    qtbot.waitUntil(lambda: not page.busy and sum(i.is_downloaded for i in page.model.items()) == 2, timeout=10000)

    folder = Path(window.profile.output_folder)
    assert sorted(p.name for p in folder.glob("*.mp4")) == ["20261009_name 1001.mp4", "20261010_name 1000.mp4"]
    manifest = (folder / "manifest.md").read_text()
    assert "20261010_name 1000.mp4" in manifest and "| name 1000 |" in manifest
    assert (folder / "metadata" / "TikTok_1000.json").is_file()
    assert store.get_profile(window.profile.id).template == "{date}_{custom}"

    # downloaded videos appear in the Board backlog
    window.go("board")
    assert window.board.columns[Stage.BACKLOG].model.rowCount() == 2


def test_rename_applies_new_template_to_files(app_window, qtbot, monkeypatch):
    window, store, _ = app_window
    page = fetch_fake(window, qtbot, monkeypatch, make_items(2))
    fake_download(monkeypatch)
    page._start_download()
    qtbot.waitUntil(lambda: not page.busy and all(i.is_downloaded for i in page.model.items()), timeout=10000)
    from reelay.gui import download_page
    monkeypatch.setattr(download_page.RenamePreviewDialog, "exec", lambda self: download_page.RenamePreviewDialog.Accepted)
    page.model.set_custom_names({i.key: f"clip-{i.video_id}" for i in page.model.items()})
    page.template.setEditText("{custom}")
    page._apply_names()
    folder = Path(window.profile.output_folder)
    assert sorted(p.name for p in folder.glob("*.mp4")) == ["clip-1000.mp4", "clip-1001.mp4"]
    stored = {r.item.video_id: r.item.filename for r in store.load_videos(window.profile.id)}
    assert stored == {"1000": "clip-1000.mp4", "1001": "clip-1001.mp4"}


def test_custom_names_survive_restart(app_window, qtbot, monkeypatch):
    window, store, _ = app_window
    page = fetch_fake(window, qtbot, monkeypatch, make_items(3))
    key = page.model.items()[0].key
    page.model.set_custom_names({key: "keep me"})
    page._persist_names()
    page.fetched = []
    page.reload()
    assert page.model.find(key).custom_name == "keep me"


# ------------------------------------------------------------------ board
def seed_board(store, profile, tmp_path, n=3):
    folder = Path(profile.output_folder)
    folder.mkdir(parents=True, exist_ok=True)
    items = make_items(n)
    for i in items:
        i.filename, i.folder = f"{i.video_id}.mp4", str(folder)
        (folder / i.filename).write_bytes(b"v")
    ids = store.save_videos(profile.id, items)
    return items, ids


def drop(board, ids, stage, row=0):
    board._on_move(ids, stage, row)


def test_board_moves_and_rules(app_window):
    window, store, _ = app_window
    items, ids = seed_board(store, window.profile, None)
    board = window.board
    window.go("board")
    assert board.columns[Stage.BACKLOG].model.rowCount() == 3

    drop(board, [ids[items[0].key]], Stage.STAGING)
    assert board.columns[Stage.STAGING].model.rowCount() == 1
    assert store.get_post(ids[items[0].key]).title.startswith("Caption number 0")  # draft created

    drop(board, [ids[items[1].key]], Stage.PUBLISHED)  # manual "posted elsewhere"
    assert store.get_video(ids[items[1].key]).stage == Stage.PUBLISHED
    drop(board, [ids[items[1].key]], Stage.BACKLOG)    # published can't move back
    assert store.get_video(ids[items[1].key]).stage == Stage.PUBLISHED


def test_board_mime_roundtrip():
    from reelay.gui.board_page import MIME, BoardModel
    from reelay.gui.post_panel import Card
    from reelay.models import VideoRow
    model = BoardModel(Stage.BACKLOG)
    model.set_cards([Card(row=VideoRow(id=7, item=make_items(1)[0]))])
    data = model.mimeData([model.index(0)])
    assert json.loads(bytes(data.data(MIME)).decode()) == [7]
    assert model.canDropMimeData(data, Qt.MoveAction, 0, 0, model.index(0).parent())
    assert not model.canDropMimeData(QMimeData(), Qt.MoveAction, 0, 0, model.index(0).parent())


class FakeYouTube:
    def __init__(self):
        self.uploaded = []
        self.statuses = {}

    def upload(self, path, resource, on_progress=None, cancel=None):
        on_progress(50, 100)
        on_progress(100, 100)
        self.uploaded.append(resource)
        return {"id": f"YT{len(self.uploaded)}"}

    def video_statuses(self, ids):
        return {i: self.statuses[i] for i in ids if i in self.statuses}


def connect_fake_channel(window, store, tokens, monkeypatch):
    account = store.save_account(Account(profile_id=window.profile.id, platform="youtube",
                                         external_id="UC1", display_name="Lydia Channel"))
    tokens.set(account.token_key, Tokens("a", "r", 9e12).to_json())
    fake = FakeYouTube()
    monkeypatch.setattr(window.accounts, "client_for", lambda acc: fake)
    return account, fake


def test_schedule_upload_then_sync_to_published(app_window, qtbot, monkeypatch):
    window, store, tokens = app_window
    items, ids = seed_board(store, window.profile, None)
    account, fake = connect_fake_channel(window, store, tokens, monkeypatch)
    board = window.board
    window.go("board")
    row = ids[items[0].key]
    when = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.save_post(Post(video_row=row, account_id=account.id, title="My Short", publish_at=when))
    store.set_stage([row], Stage.STAGING)
    board.reload()

    board._request_upload(store.get_post(row), True)
    qtbot.waitUntil(lambda: store.get_post(row).status == PostStatus.SCHEDULED, timeout=5000)
    post = store.get_post(row)
    assert post.remote_id == "YT1" and post.remote_url.endswith("/YT1")
    assert store.get_video(row).stage == Stage.SCHEDULED
    sent = fake.uploaded[0]
    assert sent["status"]["privacyStatus"] == "private" and sent["status"]["publishAt"] == when
    assert sent["snippet"]["description"].endswith("#Shorts")

    fake.statuses["YT1"] = {"privacy": "public", "publish_at": None}
    board.sync(force=True)
    qtbot.waitUntil(lambda: store.get_video(row).stage == Stage.PUBLISHED, timeout=5000)
    assert store.get_post(row).status == PostStatus.PUBLISHED


def test_upload_blocked_with_clear_errors_when_no_channel(app_window, qtbot):
    window, store, _ = app_window
    items, ids = seed_board(store, window.profile, None)
    board = window.board
    window.go("board")
    row = ids[items[0].key]
    drop(board, [row], Stage.SCHEDULED)
    assert store.get_video(row).stage == Stage.STAGING  # stays put
    assert "Choose a YouTube channel" in board.panel.errors.text()
    assert board.upload_worker is None


def test_failed_upload_returns_to_staging(app_window, qtbot, monkeypatch):
    window, store, tokens = app_window
    items, ids = seed_board(store, window.profile, None)
    account, fake = connect_fake_channel(window, store, tokens, monkeypatch)
    from reelay.youtube import YouTubeError

    def boom(*a, **k):
        raise YouTubeError("YouTube's daily API quota is used up.")
    fake.upload = boom
    row = ids[items[0].key]
    store.save_post(Post(video_row=row, account_id=account.id, title="T"))
    window.go("board")
    window.board._request_upload(store.get_post(row), False)
    qtbot.waitUntil(lambda: store.get_post(row).status == PostStatus.FAILED, timeout=5000)
    assert store.get_video(row).stage == Stage.STAGING
    assert "quota" in store.get_post(row).error


# ------------------------------------------------------------------ accounts
def test_accounts_import_connect_disconnect(app_window, tmp_path, monkeypatch):
    window, store, tokens = app_window
    page = window.accounts_page
    window.go("accounts")
    assert page.setup_box.isVisible()

    secret = tmp_path / "client_secret.json"
    secret.write_text(json.dumps({"installed": {"client_id": "123-abc.apps.googleusercontent.com",
                                                "client_secret": "s"}}))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(secret), "JSON"))
    page._import_client()
    assert not page.setup_box.isVisible() and window.accounts.oauth_client().client_id.startswith("123")

    page._on_signed_in(({"id": "UCx", "title": "Lydia", "handle": "@lydia"}, Tokens("a", "r", 9e12)))
    account = store.list_accounts(window.profile.id)[0]
    assert account.display_name == "Lydia" and tokens.get("youtube:UCx")
    assert page.schedule_account.count() == 1

    monkeypatch.setattr("reelay.accounts.revoke", lambda *a, **k: None)
    page._disconnect(account)
    assert store.list_accounts(window.profile.id) == [] and tokens.get("youtube:UCx") is None
