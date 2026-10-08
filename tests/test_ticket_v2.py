"""Ticket v2: schema v5 migration, readiness, the Ready gate (422 + force),
revision history, derived_from, unified publishing history, provenance.
Every platform is a fake; nothing leaves this machine."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
import requests

from reelay.models import DispatchStatus, Platform, Post, Stage, VideoItem
from reelay.pipeline import PipelineError
from reelay.readiness import NOT_CHECKED, NOT_READY, READY, evaluate
from tests.fakes import SECRETS, connect_platform, disable_publishing, enable_publishing, make_world


def future(days=2):
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0)


@pytest.fixture
def world(tmp_path, monkeypatch):
    disable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch, ready=False)
    yield w
    w.store.close()


# ------------------------------------------------------------------ migration v4 → v5
def _v4_database(tmp_path, monkeypatch):
    """A real v4 database written with v4's own SQL (not today's Store code)."""
    import reelay.store as store_module
    current = store_module.SCHEMA_VERSION
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", 4)
    old = store_module.Store(tmp_path / "v4.db")
    profile = old.create_profile("Lydia Livingood", tmp_path / "lib")
    c = old.conn

    def video(vid, stage, upload_date="20261002"):
        item = VideoItem(platform="TikTok", video_id=vid, url=f"https://www.tiktok.com/@livingood.lifestyle/video/{vid}",
                         upload_date=upload_date, uploader="livingood.lifestyle", description="caption #growth")
        c.execute("INSERT INTO videos (profile_id, platform, video_id, data, stage, updated_at) VALUES (?,?,?,?,?,?)",
                  (profile.id, "TikTok", vid, json.dumps(item.to_record()), stage, "2026-10-01T12:00:00Z"))
        return c.execute("SELECT id FROM videos WHERE video_id=?", (vid,)).fetchone()[0]

    backlog, scheduled = video("111", "backlog"), video("222", "scheduled", upload_date="")
    c.execute("INSERT INTO posts (video_row, title, status, publish_at, target_platforms, updated_at) "
              "VALUES (?, 'Old title', 'scheduled', '2026-12-01T16:00:00Z', '[\"youtube\",\"bluesky\"]', 'now')",
              (scheduled,))
    c.execute("INSERT INTO dispatches (video_row, platform, status, due_at, publish_at, created_at, updated_at) "
              "VALUES (?, 'bluesky', 'pending', '2026-10-01T00:00:00Z', '2026-12-01T16:00:00Z', 'now', 'now')",
              (scheduled,))
    c.commit()
    old.close()
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", current)
    return tmp_path / "v4.db", profile, backlog, scheduled


def test_v4_to_v5_migration_keeps_everything_and_adds_provenance(tmp_path, monkeypatch):
    from reelay.store import SCHEMA_VERSION, Store
    path, _profile, backlog, scheduled = _v4_database(tmp_path, monkeypatch)
    store = Store(path)
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 5
    assert store.get_video(backlog).archived_at is None    # v6: nothing is archived by the upgrade
    a, b = store.get_video(backlog), store.get_video(scheduled)
    assert (a.stage, b.stage) == ("backlog", "scheduled") and a.item.description == "caption #growth"
    assert store.get_post(scheduled).title == "Old title" and store.dispatches_for(scheduled)[0].status == "pending"
    t = a.ticket
    assert (t.source_platform, t.source_url, t.creator, t.original_publish_date, t.imported_at) == (
        "tiktok", "https://www.tiktok.com/@livingood.lifestyle/video/111", "livingood.lifestyle",
        "2026-10-02T00:00:00Z", "2026-10-01T12:00:00Z")
    assert t.authorization is None and t.transcript is None and t.derived_from is None   # never invented
    assert b.ticket.original_publish_date is None                                        # unknown stays unknown
    assert (a.ready_gate_exempt, b.ready_gate_exempt) == (False, True)                    # grandfathered
    assert store.revisions(backlog) == [] and store.manual_posts(backlog) == []
    store.close()
    again = Store(path)   # re-opening doesn't migrate twice
    assert again.get_video(backlog).ticket.source_platform == "tiktok"
    again.close()


def test_grandfathered_scheduled_items_keep_dispatching(tmp_path, monkeypatch):
    """A card scheduled before the gate existed isn't ready (no Bluesky copy), and still goes out."""
    from reelay.accounts import AccountService
    from reelay.pipeline import Pipeline
    from reelay.publishers.base import PublishResult
    from reelay.scheduler import Dispatcher
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    from tests.fakes import FakeUploads
    enable_publishing(monkeypatch, Platform.BLUESKY)
    path, profile, _backlog, scheduled = _v4_database(tmp_path, monkeypatch)
    store = Store(path)
    accounts = AccountService(store, MemoryTokenStore())
    account = connect_platform(accounts, profile.id, Platform.BLUESKY)
    store.conn.execute("UPDATE dispatches SET account_id=?", (account.id,))
    store.conn.commit()
    pipeline = Pipeline(store, accounts, FakeUploads(), instance_id="x")
    assert pipeline.card(scheduled).readiness.platforms["bluesky"].state == NOT_READY

    class Pub:
        calls = []

        def publish(self, job):
            self.calls.append(job)
            return PublishResult(DispatchStatus.PUBLISHED, remote_id="b1", remote_url="https://bsky.app/b1")
    pub = Pub()
    dispatcher = Dispatcher(store, pipeline, {Platform.BLUESKY: pub}, interval=0.05,
                            clock=lambda: datetime(2026, 12, 2, tzinfo=timezone.utc))
    monkeypatch.setattr(pipeline, "build_job", lambda d: type("J", (), {"dispatch": d})())
    assert dispatcher.run_once() == 1 and pub.calls
    assert store.dispatches_for(scheduled)[0].status == "published"
    store.close()


# ------------------------------------------------------------------ readiness
def _post(**kw):
    base = dict(video_row=1, title="Letting go", description="A note on growth.", target_platforms=["youtube"])
    return Post(**{**base, **kw})


def test_readiness_ready_case(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v")
    post = _post(target_platforms=["youtube", "bluesky"], bluesky_text="Letting go 🦋")
    r = evaluate(post, {"youtube", "bluesky"}, video, duration=30)
    assert r.ready and r.blocking == [] and r.problems() == []
    assert {p: x.state for p, x in r.platforms.items()} == {"youtube": READY, "bluesky": READY}
    assert all(c.state == "pass" for c in r.platforms["youtube"].checks)


def test_readiness_not_ready_case_lists_every_problem(tmp_path):
    post = _post(description="", target_platforms=["youtube", "threads"], tags=["x" * 600])
    r = evaluate(post, {"youtube", "threads"}, tmp_path / "missing.mp4", duration=400)
    assert not r.ready and r.blocking == ["youtube", "threads"]
    problems = r.problems()
    assert "YouTube: Add a description." in problems
    assert any(p.startswith("YouTube: Tags are") for p in problems)
    assert "Threads: Write the Threads post (otherwise Reelay would fall back to the title)." in problems
    assert "Threads: Threads videos can be at most 5 minutes long." in problems
    assert "YouTube: The video file is missing. Download it first." in problems
    over = evaluate(_post(target_platforms=["bluesky"], bluesky_text="😀" * 301), {"bluesky"}, None)
    assert "Bluesky text is 301 characters" in over.problems()[0]   # the platform's own counting


def test_disabled_platforms_evaluate_as_pass(tmp_path):
    post = _post(description="", target_platforms=["youtube", "bluesky"])   # Bluesky copy missing too
    r = evaluate(post, {"youtube"}, tmp_path / "missing.mp4")
    assert r.platforms["bluesky"].state == NOT_CHECKED and r.platforms["bluesky"].ready
    assert r.platforms["bluesky"].problems == [] and r.blocking == ["youtube"]
    assert r.ready   # READY: at least one target (the hand-posted one) is ready
    only_manual = evaluate(_post(description="", target_platforms=["facebook"]), {"youtube"}, None)
    assert only_manual.ready and only_manual.platforms["facebook"].state == NOT_CHECKED


def test_board_cards_carry_readiness(world):
    card = world.pipeline.card(world.rows[0])    # suggested draft from the caption: title + description
    assert card.readiness.ready and card.readiness.platforms["youtube"].state == READY
    missing = world.pipeline.card(world.rows[3])   # fetched, not downloaded
    assert missing.readiness.blocking == ["youtube"]


# ------------------------------------------------------------------ the Ready gate
def test_gate_refuses_with_ticket_not_ready_and_writes_nothing(world, monkeypatch):
    enable_publishing(monkeypatch, Platform.BLUESKY)
    connect_platform(world.accounts, world.profile.id, Platform.BLUESKY)
    row = world.rows[0]
    with pytest.raises(PipelineError) as err:
        world.pipeline.schedule(row, scheduled_at=future(), platforms=["bluesky"])
    assert err.value.code == "ticket_not_ready"
    assert err.value.problems == ["Bluesky: Write the Bluesky post (otherwise Reelay would fall back to the title)."]
    assert world.store.dispatches_for(row) == [] and world.store.get_video(row).stage == Stage.BACKLOG
    result = world.pipeline.schedule(row, scheduled_at=future(), platforms=["bluesky"], force=True)
    assert [d.platform for d in result.dispatches] == ["bluesky"]
    assert world.store.get_video(row).stage == Stage.SCHEDULED


def test_force_never_bypasses_platform_rules(world, monkeypatch):
    enable_publishing(monkeypatch, Platform.BLUESKY)   # no Bluesky account connected
    with pytest.raises(PipelineError) as err:
        world.pipeline.schedule(world.rows[0], platforms=["bluesky"], force=True)
    assert err.value.code == "invalid_post" and any("Connect a Bluesky account" in p for p in err.value.problems)


def test_hard_problems_are_reported_with_readiness_together(world, monkeypatch):
    enable_publishing(monkeypatch, Platform.BLUESKY)
    with pytest.raises(PipelineError) as err:   # not connected AND not ready: one answer, every problem
        world.pipeline.schedule(world.rows[0], platforms=["bluesky"])
    assert err.value.code == "invalid_post"
    assert any("Connect a Bluesky account" in p for p in err.value.problems)
    assert any(p.startswith("Bluesky: Write the Bluesky post") for p in err.value.problems)


# ------------------------------------------------------------------ revision history
def test_revisions_on_logical_commits_newest_first(world):
    store, pipeline, row = world.store, world.pipeline, world.rows[0]
    pipeline.save_draft(pipeline.post_or_default(store.get_video(row))[0])   # first save: what was filled in
    first = {r.field for r in store.revisions(row)}
    assert {"title", "description", "target_platforms"} <= first and all(r.old_value is None
                                                                            for r in store.revisions(row))
    with store.origin("api"):
        pipeline.update_metadata(row, {"title": "Letting go", "topic": "identity"})
    newest = store.revisions(row)[:2]
    assert {(r.field, r.origin) for r in newest} == {("title", "api"), ("topic", "api")}
    title = next(r for r in newest if r.field == "title")
    assert title.old_value.startswith("Caption number 0") and title.new_value == "Letting go"
    count = len(store.revisions(row))
    pipeline.update_metadata(row, {"title": "Letting go"})          # no change: nothing logged
    store.save_post(replace(store.get_post(row), status="failed", error="x"))   # publishing state isn't metadata
    assert len(store.revisions(row)) == count
    pipeline.update_ticket(row, {"keywords": "growth, healing"})
    assert store.revisions(row)[0].field == "keywords" and store.revisions(row)[0].origin == "local"
    ids = [r.id for r in store.revisions(row)]
    assert ids == sorted(ids, reverse=True)


def test_list_fields_are_logged_as_json(world):
    row = world.rows[0]
    world.pipeline.update_metadata(row, {"tags": ["growth", "healing"]})
    tags = next(r for r in world.store.revisions(row) if r.field == "tags")
    assert json.loads(tags.new_value) == ["growth", "healing"]


def test_log_failure_rolls_back_the_write(world, monkeypatch):
    store, pipeline, row = world.store, world.pipeline, world.rows[0]
    pipeline.update_ticket(row, {"topic": "before"})
    pipeline.update_metadata(row, {"title": "Before"})

    def broken(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(type(store), "_log_revisions", broken)
    with pytest.raises(sqlite3.OperationalError):
        pipeline.update_ticket(row, {"topic": "after"})
    with pytest.raises(sqlite3.OperationalError):
        pipeline.update_metadata(row, {"title": "After", "topic": "after"})
    with pytest.raises(sqlite3.OperationalError):
        store.save_post(replace(store.get_post(row), title="After"))
    assert store.get_video(row).ticket.topic == "before" and store.get_post(row).title == "Before"


# ------------------------------------------------------------------ derived_from
def test_derived_from_is_nulled_when_the_parent_is_deleted(world):
    store, pipeline = world.store, world.pipeline
    parent, child = world.rows[0], world.rows[1]
    pipeline.update_ticket(child, {"derived_from": parent, "content_type": "clip"})
    assert store.get_video(child).ticket.derived_from == parent
    store.delete_video(parent)
    kid = store.get_video(child)
    assert kid is not None and kid.ticket.derived_from is None and kid.ticket.content_type == "clip"
    for bad in (child, 99999, "1"):
        with pytest.raises(PipelineError) as err:
            pipeline.update_ticket(child, {"derived_from": bad})
        assert err.value.code == "invalid_post"


def test_ticket_field_validation(world):
    p, row = world.pipeline, world.rows[0]
    for changes in ({"content_type": "movie"}, {"original_publish_date": "yesterday"},
                    {"original_publish_date": "2026-10-02T15:00:00"}, {"topic": "x" * 201}, {"imported_at": "x"},
                    {"surprise": 1}):
        with pytest.raises(PipelineError):
            p.update_ticket(row, changes)
    t = p.update_ticket(row, {"original_publish_date": "2026-10-02T17:00:00+02:00", "topic": "  growth  ",
                              "keywords": "   "})
    assert (t.original_publish_date, t.topic, t.keywords) == ("2026-10-02T15:00:00Z", "growth", None)


# ------------------------------------------------------------------ unified publishing history
def test_mark_as_posted_joins_automatic_history(world):
    store, pipeline, row = world.store, world.pipeline, world.rows[0]
    pipeline.schedule(row, scheduled_at=future())
    posted = pipeline.mark_posted(row, "tiktok", "2026-10-05T18:30:00-04:00", "https://www.tiktok.com/@x/video/1",
                                  "posted from phone")
    assert posted.posted_at == "2026-10-05T22:30:00Z" and posted.origin == "local"
    history = pipeline.publishing_history(row)
    assert list(history) == ["youtube", "tiktok"]
    assert (history["youtube"][0].method, history["youtube"][0].status) == ("automatic", "dispatching")
    manual = history["tiktok"][0]
    assert (manual.method, manual.status, manual.url, manual.note) == (
        "manual", "posted", "https://www.tiktok.com/@x/video/1", "posted from phone")
    assert store.get_video(row).stage == Stage.SCHEDULED          # marking doesn't move anything
    for bad in (("myspace", None, ""), ("tiktok", future(1).isoformat(), ""), ("tiktok", "nope", ""),
                ("tiktok", None, "ftp://x")):
        with pytest.raises(PipelineError):
            pipeline.mark_posted(row, bad[0], bad[1], bad[2])
    pipeline.unmark_posted(posted.id)
    assert "tiktok" not in pipeline.publishing_history(row)


# ------------------------------------------------------------------ provenance on import
def test_import_records_provenance_without_overwriting(world):
    store = world.store
    item = VideoItem(platform="Instagram", video_id="C1", url="https://www.instagram.com/reel/C1/",
                     upload_date="20260901", uploader="livingood.lifestyle")
    row = store.save_videos(world.profile.id, [item], authorization="Managed account")[item.key]
    t = store.get_video(row).ticket
    assert (t.source_platform, t.source_url, t.authorization, t.original_publish_date, t.creator) == (
        "instagram", "https://www.instagram.com/reel/C1/", "Managed account", "2026-09-01T00:00:00Z",
        "livingood.lifestyle")
    imported = t.imported_at
    world.pipeline.update_ticket(row, {"authorization": "Own content"})
    store.save_videos(world.profile.id, [item], authorization="Something else")   # re-fetch later
    t2 = store.get_video(row).ticket
    assert t2.authorization == "Own content" and t2.imported_at == imported


# ------------------------------------------------------------------ API
@pytest.fixture
def api(tmp_path, monkeypatch):
    from reelay.api import ApiServer, create_app
    disable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch, ready=False)
    server = ApiServer(create_app(w.store, w.pipeline, w.accounts, lambda: SECRETS["agent"]), host="127.0.0.1", port=0)
    assert server.start()
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {SECRETS['agent']}"
    session.base, session.world = server.url, w
    yield session
    server.stop()
    w.store.close()


def test_api_422_ticket_not_ready_then_force(api, monkeypatch):
    w = api.world
    enable_publishing(monkeypatch, Platform.BLUESKY)
    connect_platform(w.accounts, w.profile.id, Platform.BLUESKY)
    vid = w.rows[0]
    r = api.post(f"{api.base}/v1/videos/{vid}/schedule", json={"platforms": ["bluesky"]}, timeout=10)
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "ticket_not_ready" and body["problems"][0].startswith("Bluesky: Write the Bluesky post")
    assert w.store.dispatches_for(vid) == []
    board = api.get(f"{api.base}/v1/board", params={"profile": "Lydia Livingood"}, timeout=10).json()
    video = next(v for v in board["videos"] if v["id"] == vid)
    assert video["ready"] is True and video["readiness"]["youtube"]["state"] == "ready"
    r = api.post(f"{api.base}/v1/videos/{vid}/schedule?force=true", json={"platforms": ["bluesky"]}, timeout=10)
    assert r.status_code == 200, r.text
    assert [j["platform"] for j in r.json()["jobs"]] == ["bluesky"]


def test_api_patch_ticket_fields_logs_revisions_and_marks_posted(api):
    w = api.world
    vid = w.rows[0]
    r = api.patch(f"{api.base}/v1/videos/{vid}", json={"topic": "identity", "content_type": "short",
                                                       "original_publish_date": "2026-10-02T15:00:00Z",
                                                       "authorization": "Own content"}, timeout=10)
    assert r.status_code == 200, r.text
    video = r.json()
    assert video["ticket"]["topic"] == "identity" and video["ticket"]["content_type"] == "short"
    assert video["provenance"]["authorization"] == "Own content"
    assert video["provenance"]["source_platform"] == "tiktok" and video["provenance"]["imported_at"]
    assert api.patch(f"{api.base}/v1/videos/{vid}", json={"imported_at": "2026-01-01T00:00:00Z"},
                     timeout=10).status_code == 422
    revs = api.get(f"{api.base}/v1/videos/{vid}/revisions", timeout=10).json()["revisions"]
    assert {(x["field"], x["origin"]) for x in revs} >= {("topic", "api"), ("authorization", "api")}
    r = api.post(f"{api.base}/v1/videos/{vid}/manual-posts", json={"platform": "instagram"}, timeout=10)
    assert r.status_code == 201 and r.json()["method"] == "manual"
    assert api.post(f"{api.base}/v1/videos/{vid}/manual-posts", json={"platform": "myspace"},
                    timeout=10).status_code == 422
    board = api.get(f"{api.base}/v1/board", params={"profile": "Lydia Livingood"}, timeout=10).json()
    video = next(v for v in board["videos"] if v["id"] == vid)
    assert video["publishing_history"]["instagram"][0]["status"] == "posted"
    assert api.get(f"{api.base}/v1/videos/99999/revisions", timeout=10).status_code == 404


# ------------------------------------------------------------------ desktop ticket
@pytest.fixture
def board(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox

    from reelay.gui.app import create_window
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    from tests.test_gui import seed_board
    disable_publishing(monkeypatch)
    answers = {"Ticket Not Ready": QMessageBox.No}
    monkeypatch.setattr(QMessageBox, "question",
                        lambda _parent, title, *a, **k: answers.get(title, QMessageBox.Yes))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    store = Store(db_path())
    profile = store.create_profile("Lydia", tmp_path / "lib")
    store.set_active_profile(profile.id)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    window.show()
    items, ids = seed_board(store, window.profile, None)
    row = ids[items[0].key]
    store.save_post(Post(video_row=row, title="Letting go", description="", target_platforms=["youtube"]))
    window.go("board")
    b = window.board
    col = b.columns[Stage.BACKLOG]
    b._on_select(col, next(col.model.index(r) for r, c in enumerate(col.model.cards) if c.id == row))
    yield window, store, b, row, answers
    window.close()
    store.close()


def test_ticket_header_shows_ready_state_live(board, qtbot):
    _window, _store, b, _row, _answers = board
    header = b.panel.readiness_header
    assert header.badge.text() == "NOT READY" and "YouTube" in header.summary.text()
    b.panel.blocks["description"].set_text("A note on growth.")       # typing previews, saves nothing
    qtbot.waitUntil(lambda: header.badge.text() == "READY", timeout=2000)


def test_gui_gate_asks_then_forces_only_on_yes(board, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from reelay.models import Account
    from reelay.youtube import OAuthClient, Tokens
    window, store, b, row, answers = board
    account = store.save_account(Account(profile_id=window.profile.id, platform="youtube", external_id="UC1",
                                         display_name="Lydia", handle="@l"))
    window.accounts.tokens.set(account.token_key, Tokens("a", "r", 9e12).to_json())
    window.accounts.set_oauth_client(OAuthClient("i", "s"))
    monkeypatch.setattr(window.accounts, "client_for", lambda acc: object())
    monkeypatch.setattr(window.uploads, "enqueue", lambda *a, **k: None)
    post = replace(store.get_post(row), account_id=account.id)
    b._request_upload(post, False)                                    # answered No
    assert store.dispatches_for(row) == [] and "description" in b.panel.errors.text()
    answers["Ticket Not Ready"] = QMessageBox.Yes
    b._request_upload(post, False)
    assert [d.platform for d in store.dispatches_for(row)] == ["youtube"]


def test_ticket_fields_commit_on_blur_not_keystrokes(board, qtbot):
    _window, store, b, row, _answers = board
    details = b.panel.details
    details.toggle.setChecked(True)
    before = len(store.revisions(row))
    qtbot.keyClicks(details.topic, "letting go")
    assert len(store.revisions(row)) == before and store.get_video(row).ticket.topic is None
    details.topic.editingFinished.emit()                              # leaving the field
    assert store.get_video(row).ticket.topic == "letting go"
    assert [(r.field, r.origin) for r in store.revisions(row)][:1] == [("topic", "local")]
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QFocusEvent
    details.transcript.setPlainText("Hi, it's Lydia.")
    details.transcript.document().setModified(True)
    details.transcript.focusOutEvent(QFocusEvent(QEvent.FocusOut))
    assert store.get_video(row).ticket.transcript == "Hi, it's Lydia."


def test_mark_posted_and_history_from_the_ticket(board, monkeypatch):
    from reelay.gui import ticket_widgets
    _window, store, b, row, _answers = board
    monkeypatch.setattr(ticket_widgets.MarkPostedDialog, "exec", lambda self: ticket_widgets.MarkPostedDialog.Accepted)
    b._mark_posted(row)
    assert [m.platform for m in store.manual_posts(row)] == ["tiktok"]
    b.reload()
    assert "TikTok" in " ".join(w.text() for w in b.panel.history.findChildren(type(b.panel.hook)))
    shown = []
    monkeypatch.setattr(ticket_widgets.RevisionDialog, "exec", lambda self: shown.append(self.tree.topLevelItemCount()))
    b._show_revisions(row)
    assert shown and shown[0] >= 1


# ------------------------------------------------------------------ flags on/off matrix
FLAGGED = (Platform.BLUESKY, Platform.THREADS, Platform.FACEBOOK)


@pytest.mark.parametrize("on", [tuple(p for n, p in enumerate(FLAGGED) if mask >> n & 1) for mask in range(8)],
                         ids=lambda on: "+".join(on) or "none")
def test_flags_matrix_readiness_and_gate(tmp_path, monkeypatch, on):
    """Every combination of the three flags: off platforms are never checked and
    never block; on platforms are checked and gate scheduling."""
    disable_publishing(monkeypatch)
    enable_publishing(monkeypatch, *on) if on else None
    w = make_world(tmp_path, monkeypatch, ready=False, connect=FLAGGED)
    w.store.set_setting("public_url", "https://reelay.example.com")
    row = w.rows[0]
    w.pipeline.update_metadata(row, {"target_platforms": ["youtube", *FLAGGED]})
    readiness = w.pipeline.readiness(row)
    for p in FLAGGED:
        assert readiness.platforms[p].state == (NOT_READY if p in on else NOT_CHECKED), p
    assert readiness.platforms["youtube"].state == READY and readiness.ready
    assert readiness.blocking == list(on)
    if on:
        with pytest.raises(PipelineError) as err:
            w.pipeline.schedule(row, scheduled_at=future())
        assert err.value.code == "ticket_not_ready"
        assert {p.split(":")[0] for p in err.value.problems} == {Platform.LABELS[p] for p in on}
        w.pipeline.update_metadata(row, {f"{p}_text": "Letting go" for p in on})
    result = w.pipeline.schedule(row, scheduled_at=future())
    assert {d.platform for d in result.dispatches} == {"youtube", *on}
    assert set(result.manual) == set(FLAGGED) - set(on)
    w.store.close()
