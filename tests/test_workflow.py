"""Workflow features: spreadsheet import, fill from the original post,
transcripts, bulk edit, archive, undo from history, the Ready filter, and
editing sources through the API. Every network call is faked."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import requests

from reelay.models import Post, Stage
from reelay.pipeline import PipelineError
from reelay.sheet_import import (SheetError, convert, fetch_google_sheet, google_export_url, guess_mapping,
                                 parse_date, read_csv, video_key_from_url)
from reelay.transcripts import TranscriptError, captions_to_text, fetch_transcript, pick_track
from tests.conftest import FakeResponse
from tests.fakes import approve_and_run, SECRETS, disable_publishing, make_world


def future(days=2):
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0)


@pytest.fixture
def world(tmp_path, monkeypatch):
    disable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch, ready=False)
    yield w
    w.store.close()


# ------------------------------------------------------------------ sheet import: reading & mapping
SHEET = """﻿Video Link,YouTube Title,TikTok Caption,Instagram,Bluesky,Hashtags,Topic,Type,Date Posted,Notes
https://www.tiktok.com/@x/video/7000?lang=en,Letting Go,letting go 🦋 #growth,ig caption,bsky post,#growth #healing,identity,Short,10/02/2026,ignored
7001,,second tiktok,,,,,clip,2026-10-01,
https://www.tiktok.com/@x/video/9999,Nobody,,,,,,,,
"""


def test_read_csv_and_guess_mapping():
    headers, rows = read_csv(SHEET)
    assert headers[0] == "Video Link" and len(rows) == 3 and rows[1][0] == "7001"
    mapping = guess_mapping(headers)
    assert mapping == {"Video Link": "match_url", "YouTube Title": "title", "TikTok Caption": "tiktok_text",
                       "Instagram": "instagram_text", "Bluesky": "bluesky_text", "Hashtags": "hashtags",
                       "Topic": "topic", "Type": "content_type", "Date Posted": "original_publish_date",
                       "Notes": None}
    semi = read_csv("Title;Topic\nHello;growth\n")
    assert semi == (["Title", "Topic"], [["Hello", "growth"]])
    with pytest.raises(SheetError):
        read_csv("Title\n")


def test_value_conversion_and_link_ids():
    assert video_key_from_url("https://www.tiktok.com/@x/video/7692175258450677023?lang=en") == "7692175258450677023"
    assert video_key_from_url("https://youtube.com/shorts/abcDEF12345") == "abcDEF12345"
    assert video_key_from_url("https://www.instagram.com/reel/C1xYz/") == "C1xYz"
    assert parse_date("10/02/2026") == parse_date("2026-10-02") == parse_date("Oct 2, 2026") == "2026-10-02T00:00:00Z"
    assert parse_date("someday") is None
    assert convert("tags", "#growth, healing;  #self") == (["growth", "healing", "self"], None)
    assert convert("target_platforms", "YouTube, Bluesky") == (["youtube", "bluesky"], None)
    assert convert("target_platforms", "TikTok, Instagram") == (["tiktok", "instagram"], None)   # posted by hand
    assert convert("target_platforms", "MySpace")[1]
    assert convert("content_type", "Long Form") == ("long-form", None) and convert("content_type", "movie")[1]


def test_google_sheet_links(fake_session):
    link = "https://docs.google.com/spreadsheets/d/1AbC-dEf/edit#gid=42"
    assert google_export_url(link) == "https://docs.google.com/spreadsheets/d/1AbC-dEf/export?format=csv&gid=42"
    assert google_export_url("https://example.com/sheet") is None
    fake_session.on("GET", "/export", FakeResponse(200, headers={"Content-Type": "text/csv"}, text="Title\nHi\n"))
    assert fetch_google_sheet(link, fake_session) == (["Title"], [["Hi"]])
    fake_session.on("GET", "/export", FakeResponse(200, headers={"Content-Type": "text/html"}, text="<html>"))
    with pytest.raises(SheetError, match="Anyone with the link"):
        fetch_google_sheet(link, fake_session)


# ------------------------------------------------------------------ sheet import: plan & apply
def _plan(world, text=SHEET, overwrite=False, mapping=None):
    headers, rows = read_csv(text)
    return world.pipeline.plan_sheet(world.profile.id, headers, rows, mapping or guess_mapping(headers), overwrite)


def test_import_plan_matches_rows_and_never_clears(world):
    plan = _plan(world)
    first, second, third = plan.rows
    assert (first.video_id, first.matched_by) == (world.rows[0], "link")
    assert set(first.post_changes) == {"title", "tiktok_text", "instagram_text", "bluesky_text", "hashtags"}
    assert first.ticket_changes == {"topic": "identity", "content_type": "short"}
    assert "First published (already filled)" in first.kept               # provenance recorded it on import
    assert (second.video_id, second.matched_by) == (world.rows[1], "video ID")
    assert set(second.fields) == {"tiktok_text", "content_type"}     # empty cells change nothing
    assert third.video_id is None and "No video" in third.problems[0]
    assert len(plan.changing) == 2 and "2 tickets will change" in plan.summary()


def test_import_fill_only_vs_overwrite_and_invalid_rows(world):
    world.pipeline.update_metadata(world.rows[0], {"tiktok_text": "already written", "topic": "kept"})
    plan = _plan(world)
    first = plan.rows[0]
    assert "tiktok_text" not in first.post_changes and "topic" not in first.ticket_changes
    assert any("TikTok caption" in k for k in first.kept)
    assert _plan(world, overwrite=True).rows[0].post_changes["tiktok_text"] == "letting go 🦋 #growth"
    too_long = "Video ID,Bluesky\n7000," + "x" * 301 + "\n7001,fine\n7001,dupe\n"
    plan = _plan(world, too_long)
    assert plan.rows[0].problems[0].startswith("Bluesky post: Bluesky text is 301")   # skipped, never shortened
    assert plan.rows[1].will_change and "Same video as row 3" in plan.rows[2].problems[0]


def test_import_applies_in_one_transaction_with_history(world, monkeypatch):
    plan = _plan(world)
    with world.store.origin("import"):
        assert world.pipeline.apply_sheet(plan) == 2
    post = world.store.get_post(world.rows[0])
    assert post.title == "Letting Go" and post.hashtags == "#growth #healing"
    assert world.store.get_video(world.rows[0]).ticket.topic == "identity"
    assert {r.origin for r in world.store.revisions(world.rows[0])} == {"import"}
    # a failure part-way writes nothing at all
    plan = _plan(world, "Video ID,Topic\n7000,new topic\n7001,other topic\n", overwrite=True)
    real = type(world.store)._log_revisions
    calls = []

    def flaky(self, c, row, diffs):
        calls.append(row)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return real(self, c, row, diffs)
    monkeypatch.setattr(type(world.store), "_log_revisions", flaky)
    with pytest.raises(RuntimeError):
        world.pipeline.apply_sheet(plan)
    assert world.store.get_video(world.rows[0]).ticket.topic == "identity"
    assert world.store.get_video(world.rows[1]).ticket.topic is None


def test_import_leaves_scheduled_copy_alone(world):
    world.pipeline.update_metadata(world.rows[0], {"description": "ready"})
    world.pipeline.schedule(world.rows[0], scheduled_at=future())
    plan = _plan(world, "Video ID,TikTok Caption,Topic\n7000,new caption,new topic\n")
    row = plan.rows[0]
    assert row.ticket_changes == {"topic": "new topic"} and not row.post_changes
    assert any("already scheduled" in k for k in row.kept)


# ------------------------------------------------------------------ fill from the original post
def test_fill_from_original_fills_only_empty_fields_within_limits(world):
    store, pipeline, row = world.store, world.pipeline, world.rows[0]
    pipeline.update_metadata(row, {"instagram_text": "mine, keep it"})
    filled = pipeline.fill_from_original([row])
    post = store.get_post(row)
    caption = store.get_video(row).item.description
    assert post.tiktok_text == caption and post.facebook_text == caption
    assert post.instagram_text == "mine, keep it"                        # written copy is never touched
    assert post.bluesky_text.startswith("Caption number 0") and post.hashtags == "#growth #tag0"
    assert store.get_video(row).ticket.creator == "livingood.lifestyle"
    assert "instagram_text" not in filled[row] and "bluesky_text" in filled[row]
    assert pipeline.fill_from_original([row]) == {}                      # nothing left to fill
    assert pipeline.readiness(row).ready


def test_fill_never_shortens(world):
    store, row = world.store, world.rows[1]
    item = store.get_video(row).item
    item.description = "word " * 900                                     # 4,500 characters: over TikTok's 4,000
    store.save_video(world.profile.id, item)
    world.pipeline.fill_from_original([row])
    post = store.get_post(row)
    assert post.tiktok_text is None and post.instagram_text is None      # left for a person
    assert post.facebook_text == item.description.strip()
    assert post.bluesky_text == post.title                               # the title alone fits


# ------------------------------------------------------------------ transcripts
VTT = """WEBVTT
Kind: captions
Language: en

1
00:00:00.000 --> 00:00:02.000
<c>Hi, it's Lydia.</c>

00:00:02.000 --> 00:00:04.000
Hi, it's Lydia.
Today we talk about

00:00:04.000 --> 00:00:06.000
Today we talk about letting go &amp; growth.
"""


def test_captions_to_text_and_track_choice():
    assert captions_to_text(VTT) == "Hi, it's Lydia. Today we talk about letting go & growth."
    info = {"automatic_captions": {"en": [{"ext": "json3", "url": "a"}, {"ext": "vtt", "url": "auto-en"}]},
            "subtitles": {"es": [{"ext": "vtt", "url": "creator-es"}], "eng-US": [{"ext": "vtt", "url": "creator-en"}]}}
    assert pick_track(info)["url"] == "creator-en"                        # creator subtitles, English first
    assert pick_track({"automatic_captions": info["automatic_captions"]})["automatic"] is True
    assert pick_track({}) is None


class FakeYDL:
    def __init__(self, info, data=VTT):
        self.info, self.data = info, data

    def __call__(self, opts):
        assert opts["skip_download"] is True
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        return self.info

    def urlopen(self, url):
        return type("R", (), {"read": lambda _self: self.data.encode()})()


def test_fetch_transcript_with_a_fake_ytdlp(world):
    post = "https://www.tiktok.com/@x/video/1"
    text, track = fetch_transcript(post, {}, FakeYDL({"subtitles": {"en": [{"ext": "vtt",
                                                                             "url": "https://151.101.1.1/u.vtt"}]}}))
    assert text.startswith("Hi, it's Lydia.") and track["lang"] == "en"
    with pytest.raises(TranscriptError, match="no captions"):
        fetch_transcript(post, {}, FakeYDL({}))
    row = world.rows[0]
    fake = lambda url, opts: (f"transcript of {url}", {})                 # noqa: E731
    assert world.pipeline.fetch_transcript(row, {}, fetch=fake).startswith("transcript of https://www.tiktok.com")
    with pytest.raises(PipelineError, match="already has"):
        world.pipeline.fetch_transcript(row, {}, fetch=fake)
    world.pipeline.fetch_transcript(row, {}, replace_existing=True, fetch=lambda u, o: ("new", {}))
    assert world.store.get_video(row).ticket.transcript == "new"


# ------------------------------------------------------------------ bulk edit
def test_bulk_update_is_all_or_nothing(world):
    store, pipeline, rows = world.store, world.pipeline, world.rows[:3]
    assert pipeline.bulk_update(rows, {"authorization": "Own content", "hashtags": "#growth"}) == 3
    assert all(store.get_video(r).ticket.authorization == "Own content" for r in rows)
    assert all(store.get_post(r).hashtags == "#growth" for r in rows)
    pipeline.update_metadata(rows[0], {"description": "ready"})
    pipeline.schedule(rows[0], scheduled_at=future())                     # its copy is now locked
    with pytest.raises(PipelineError) as err:
        pipeline.bulk_update(rows, {"hashtags": "#new", "topic": "t"})
    assert err.value.code == "invalid_post" and "copy can't change" in err.value.problems[0]
    assert store.get_post(rows[1]).hashtags == "#growth" and store.get_video(rows[1]).ticket.topic is None
    assert pipeline.bulk_update(rows, {"topic": "t"}) == 3                # ticket fields are fine


# ------------------------------------------------------------------ archive
def test_archive_hides_keeps_and_restores(world):
    store, pipeline, row = world.store, world.pipeline, world.rows[0]
    pipeline.archive([row])
    assert row not in [c.id for cards in pipeline.board(world.profile.id).values() for c in cards]
    assert [r.id for r in pipeline.archived(world.profile.id)] == [row]
    assert store.revisions(row)[0].field == "archived"
    pipeline.archive([row], archived=False)
    assert row in [c.id for c in pipeline.board(world.profile.id)["backlog"]]
    pipeline.update_metadata(world.rows[1], {"description": "ready"})
    pipeline.schedule(world.rows[1], scheduled_at=future())
    with pytest.raises(PipelineError) as err:
        pipeline.archive([world.rows[1]])
    assert err.value.code == "locked"


# ------------------------------------------------------------------ undo from history
def test_restore_a_logged_change(world):
    store, pipeline, row = world.store, world.pipeline, world.rows[0]
    pipeline.update_metadata(row, {"title": "First", "tags": ["a", "b"]})
    pipeline.update_metadata(row, {"title": "Second", "tags": ["c"]})
    title_change = next(r for r in store.revisions(row) if r.field == "title" and r.new_value == "Second")
    tags_change = next(r for r in store.revisions(row) if r.field == "tags" and r.new_value == '["c"]')
    pipeline.restore_revision(row, title_change.id)
    pipeline.restore_revision(row, tags_change.id)
    post = store.get_post(row)
    assert post.title == "First" and post.tags == ["a", "b"]
    assert store.revisions(row)[0].field == "tags"                        # the undo is itself logged
    pipeline.archive([row])
    pipeline.restore_revision(row, store.revisions(row)[0].id)           # undo the archive
    assert store.get_video(row).archived_at is None
    with pytest.raises(PipelineError):
        pipeline.restore_revision(row, 999999)


# ------------------------------------------------------------------ API
@pytest.fixture
def api(tmp_path, monkeypatch):
    from reelay.api import ApiServer, create_app
    disable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch, ready=False)
    server = ApiServer(create_app(w.store, w.pipeline, w.accounts, lambda: SECRETS["agent"]), host="127.0.0.1", port=0)
    assert server.start()
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {SECRETS['agent']}"
    s.base, s.world = server.url, w
    yield s
    server.stop()
    w.store.close()


def test_api_sources_patch_and_delete(api):
    w = api.world
    base = f"{api.base}/v1/sources"
    made = api.post(f"{base}?profile=Lydia%20Livingood", json={"url": "https://www.tiktok.com/@livingood.lifestyle"},
                    timeout=10).json()
    r = api.patch(f"{base}/{made['id']}", json={"name": "Lydia's TikTok", "mine": True}, timeout=10)
    assert r.status_code == 200 and r.json()["name"] == "Lydia's TikTok" and r.json()["mine"] is True
    assert api.patch(f"{base}/{made['id']}", json={"name": ""}, timeout=10).status_code == 422
    assert api.patch(f"{base}/{made['id']}", json={}, timeout=10).status_code == 422
    assert api.patch(f"{base}/999", json={"mine": True}, timeout=10).status_code == 404
    r = api.delete(f"{base}/{made['id']}", timeout=10)
    assert r.status_code == 202 and len(w.store.list_sources(w.profile.id)) == 1     # waits for a person
    assert approve_and_run(w.store, w.pipeline, r).status == "executed"
    assert w.store.list_sources(w.profile.id) == []
    assert api.delete(f"{base}/{made['id']}", timeout=10).status_code == 404


def test_api_workflow_endpoints(api, monkeypatch):
    w = api.world
    v0, v1 = w.rows[0], w.rows[1]
    r = api.post(f"{api.base}/v1/import?profile=Lydia%20Livingood", json={"csv": SHEET}, timeout=10)
    assert r.status_code == 200 and r.json()["applied"] is False and r.json()["changed"] == 2
    assert w.store.get_post(v0) is None                                  # a dry run writes nothing
    r = api.post(f"{api.base}/v1/import?profile=Lydia%20Livingood&dry_run=false", json={"csv": SHEET}, timeout=10)
    assert r.status_code == 202 and w.store.get_post(v0) is None                       # waits for a person
    assert approve_and_run(w.store, w.pipeline, r).result == {"changed": 2}
    assert w.store.get_post(v0).title == "Letting Go"
    assert api.post(f"{api.base}/v1/import?profile=Lydia%20Livingood",
                    json={"csv": SHEET, "mapping": {"Topic": "nope"}}, timeout=10).status_code == 422

    r = api.post(f"{api.base}/v1/videos/fill-from-original", json={"video_ids": [v1]}, timeout=10)
    assert r.status_code == 200 and "facebook_text" in r.json()["filled"][str(v1)]

    r = api.post(f"{api.base}/v1/videos/bulk-update", json={"video_ids": [v0, v1],
                                                           "changes": {"authorization": "Own content"}}, timeout=10)
    assert approve_and_run(w.store, w.pipeline, r).result == {"changed": 2}
    assert api.post(f"{api.base}/v1/videos/bulk-update", json={"video_ids": [v0], "changes": {"stage": "x"}},
                    timeout=10).status_code == 422

    api.patch(f"{api.base}/v1/videos/{v0}", json={"title": "Changed by mistake"}, timeout=10)
    title_rev = api.get(f"{api.base}/v1/videos/{v0}/revisions", timeout=10).json()["revisions"][0]
    r = api.post(f"{api.base}/v1/videos/{v0}/revisions/{title_rev['id']}/restore", timeout=10)
    assert r.status_code == 200 and r.json()["title"] == "Letting Go"

    r = api.post(f"{api.base}/v1/videos/{v0}/archive", timeout=10)
    assert approve_and_run(w.store, w.pipeline, r).status == "executed"
    board = api.get(f"{api.base}/v1/board", params={"profile": "Lydia Livingood"}, timeout=10).json()
    assert v0 not in [v["id"] for v in board["videos"]]
    archived = api.get(f"{api.base}/v1/board", params={"profile": "Lydia Livingood", "archived": "true"},
                       timeout=10).json()
    assert [v["id"] for v in archived["videos"]] == [v0]
    r = api.post(f"{api.base}/v1/videos/{v0}/unarchive", timeout=10)
    assert approve_and_run(w.store, w.pipeline, r).status == "executed"
    assert w.store.get_video(v0).archived_at is None

    import reelay.transcripts as transcripts
    monkeypatch.setattr(transcripts, "fetch_transcript", lambda url, opts: ("hello there", {}))
    r = api.post(f"{api.base}/v1/videos/{v0}/transcript", json={}, timeout=10)
    assert r.status_code == 200 and r.json()["ticket"]["transcript"] == "hello there"
    assert api.post(f"{api.base}/v1/videos/{v0}/transcript", json={}, timeout=10).status_code == 422

    def no_captions(url, opts):
        raise TranscriptError("This post has no captions to make a transcript from.")
    monkeypatch.setattr(transcripts, "fetch_transcript", no_captions)
    r = api.post(f"{api.base}/v1/videos/{v1}/transcript", json={}, timeout=10)
    assert r.status_code == 422 and r.json()["code"] == "no_transcript"


# ------------------------------------------------------------------ desktop board
@pytest.fixture
def board(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox

    from reelay.gui.app import create_window
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    from tests.test_gui import seed_board
    disable_publishing(monkeypatch)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    store = Store(db_path())
    profile = store.create_profile("Lydia", tmp_path / "lib")
    store.set_active_profile(profile.id)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    window.show()
    items, ids = seed_board(store, window.profile, None, n=4)
    rows = [ids[i.key] for i in items]
    store.save_post(Post(video_row=rows[0], title="No description", description=""))
    window.go("board")
    yield window, store, window.board, rows
    window.close()
    store.close()


def test_ready_filter(board):
    _window, _store, b, rows = board
    col = b.columns[Stage.BACKLOG]
    assert col.model.rowCount() == 4
    b.filter_box.setCurrentIndex(b.filter_box.findData("not_ready"))
    assert [c.id for c in col.model.cards] == [rows[0]] and col.count.text() == "1 of 4"
    b.filter_box.setCurrentIndex(b.filter_box.findData("blocking:youtube"))
    assert [c.id for c in col.model.cards] == [rows[0]]
    b.filter_box.setCurrentIndex(b.filter_box.findData("ready"))
    assert rows[0] not in [c.id for c in col.model.cards] and col.model.rowCount() == 3
    b.filter_box.setCurrentIndex(0)
    assert col.count.text() == "4"


def test_multi_select_bulk_edit_and_archive(board):
    from PySide6.QtCore import QItemSelectionModel
    _window, store, b, rows = board
    col = b.columns[Stage.BACKLOG]
    sel = col.view.selectionModel()
    for r in range(3):
        sel.select(col.model.index(r), QItemSelectionModel.Select)
    assert b.side.currentWidget() is b.bulk and b.bulk.title.text() == "3 tickets selected"
    b.bulk.inputs["topic"].setText("letting go")
    b.bulk.permission.setEditText("Own content")
    b.bulk.apply_btn.click()
    chosen = [c.id for c in col.model.cards[:3]]
    assert all(store.get_video(r).ticket.topic == "letting go" for r in chosen)
    assert all(store.get_video(r).ticket.authorization == "Own content" for r in chosen)
    b.bulk.archive_btn.click()
    assert col.model.rowCount() == 1 and len(b.pipeline.archived(b.profile.id)) == 3
    assert b.side.currentWidget() is b.panel


def test_import_dialog_previews_then_imports(board, tmp_path):
    from reelay.gui.board_tools import ImportDialog
    _window, store, b, rows = board
    vid = store.get_video(rows[1]).item.video_id
    sheet = tmp_path / "sheet.csv"
    sheet.write_text(f"Video ID,TikTok Caption,Topic\n{vid},from the sheet,growth\nnope,x,y\n", encoding="utf-8")
    dialog = ImportDialog(b.pipeline, b.profile, b)
    dialog.load_file(str(sheet))
    assert dialog.preview.rowCount() == 2 and dialog.import_btn.text() == "Import 1 Ticket"
    assert "didn't match" in dialog.summary.text() and store.get_post(rows[1]) is None   # nothing written yet
    dialog.import_btn.click()
    assert dialog.applied == 1 and store.get_post(rows[1]).tiktok_text == "from the sheet"
    assert store.revisions(rows[1])[0].origin == "import"


def test_archived_dialog_brings_videos_back(board):
    from reelay.gui.board_tools import ArchivedDialog
    _window, _store, b, rows = board
    b.pipeline.archive(rows[:2])
    dialog = ArchivedDialog(b.pipeline, b.profile, b)
    assert dialog.table.rowCount() == 2
    dialog.table.selectRow(0)
    dialog.restore_btn.click()
    assert dialog.table.rowCount() == 1 and dialog.restored == 1


def test_archived_videos_stay_off_the_download_page(board):
    window, store, b, rows = board
    b.pipeline.archive([rows[0]])
    page = window.download
    page.reload()                                   # the file is still in the folder: it isn't re-imported
    keys = [i.key for i in page.model.items()]
    assert store.get_video(rows[0]).item.key not in keys and len(keys) == 3


def test_restore_from_the_history_dialog(board, monkeypatch):
    from reelay.gui import ticket_widgets
    _window, store, b, rows = board
    b.pipeline.update_metadata(rows[0], {"title": "Changed"})
    before = len(store.revisions(rows[0]))
    captured = {}
    monkeypatch.setattr(ticket_widgets.RevisionDialog, "exec", lambda self: captured.setdefault("d", self))
    b._show_revisions(rows[0])
    dialog = captured["d"]
    dialog.tree.setCurrentItem(dialog.tree.topLevelItem(0))
    dialog.restore_btn.click()
    assert store.get_post(rows[0]).title == "No description" and dialog.tree.topLevelItemCount() == before + 1
