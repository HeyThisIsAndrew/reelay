"""Customizable board columns: rename, add, remove, reorder, per profile.
Reelay's built-in steps always live in some column, so automatic moves keep
working however the board is set up."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reelay.columns import (ColumnError, add, column_for, default_columns, is_custom, load, rank, remove, save,
                            validate)
from reelay.models import Post, Stage
from tests.fakes import disable_publishing, make_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    disable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch, ready=False)
    yield w
    w.store.close()


def test_default_is_the_four_familiar_columns(world):
    cols = load(world.store, world.profile.id)
    assert [c.name for c in cols] == ["Backlog", "Staging", "Scheduled", "Published"]
    assert [c.primary for c in cols] == list(Stage.ALL)


def test_ready_and_posted_only(world):
    """Two columns: rename two, remove two; the built-in steps fold into the neighbours."""
    cols = default_columns()
    remove(cols, "backlog")                       # first column: its step joins the next one
    remove(cols, "scheduled")                     # joins Staging (the one before it)
    cols[0].name, cols[1].name = "Ready", "Posted"
    save(world.store, world.profile.id, cols)
    cols = load(world.store, world.profile.id)
    assert [(c.name, c.stages) for c in cols] == [("Ready", ["staging", "backlog", "scheduled"]),
                                                  ("Posted", ["published"])]
    assert column_for(cols, Stage.SCHEDULED).name == "Ready" and column_for(cols, Stage.PUBLISHED).name == "Posted"


def test_six_custom_columns_and_their_steps(world):
    p, store = world.pipeline, world.store
    cols = default_columns()
    filming = add(cols, "Filming", 1)
    editing = add(cols, "Editing", 2)
    assert len(cols) == 6 and is_custom(filming.primary) and rank(filming.primary) == rank(Stage.STAGING)
    save(store, world.profile.id, cols)
    row = world.rows[0]
    p.move(row, filming.primary)                  # cards can live in your own columns
    assert store.get_video(row).stage == filming.primary and store.get_post(row) is not None
    p.move(row, editing.primary)
    p.set_targets(row, ["tiktok"])
    p.set_platform_state(row, "tiktok", "scheduled", datetime.now(timezone.utc) + timedelta(hours=1))
    assert store.get_video(row).stage == Stage.SCHEDULED      # automatic moves still work from custom columns
    board = p.board(world.profile.id)
    assert row in [c.id for c in board[Stage.SCHEDULED]]


def test_rules_keep_every_step_somewhere(world):
    cols = default_columns()
    cols[1].stages = ["staging"]
    cols[2].stages = []                           # Scheduled lost
    with pytest.raises(ColumnError):
        validate(cols)
    with pytest.raises(ColumnError):
        validate([])
    one = default_columns()
    for cid in ("staging", "scheduled", "published"):
        remove(one, cid)
    with pytest.raises(ColumnError):
        remove(one, "backlog")                    # the last column can't go
    with pytest.raises(ColumnError):
        validate([*default_columns()[:3], default_columns()[3].__class__("x", "  ", "published", ["published"])])
    world.store.set_setting("board_columns.%d" % world.profile.id, "not json")
    assert [c.name for c in load(world.store, world.profile.id)] == ["Backlog", "Staging", "Scheduled", "Published"]


def test_api_reports_columns_and_custom_stages(tmp_path, monkeypatch):
    import requests

    from reelay.api import ApiServer, create_app
    from tests.fakes import SECRETS
    disable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch, ready=False)
    cols = default_columns()
    filming = add(cols, "Filming", 1)
    save(w.store, w.profile.id, cols)
    w.pipeline.move(w.rows[0], filming.primary)
    server = ApiServer(create_app(w.store, w.pipeline, w.accounts, lambda: SECRETS["agent"]), host="127.0.0.1", port=0)
    assert server.start()
    try:
        h = {"Authorization": f"Bearer {SECRETS['agent']}"}
        board = requests.get(f"{server.url}/v1/board", params={"profile": "Lydia Livingood"}, headers=h, timeout=10).json()
        assert [c["name"] for c in board["columns"]][:2] == ["Backlog", "Filming"]
        assert next(v for v in board["videos"] if v["id"] == w.rows[0])["stage"] == filming.primary
        only = requests.get(f"{server.url}/v1/board", params={"profile": "Lydia Livingood", "stage": filming.primary},
                            headers=h, timeout=10).json()
        assert [v["id"] for v in only["videos"]] == [w.rows[0]]
    finally:
        server.stop()
        w.store.close()


# ------------------------------------------------------------------ desktop
@pytest.fixture
def board(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

    from reelay.gui.app import create_window
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    from tests.test_gui import seed_board
    disable_publishing(monkeypatch)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    store = Store(db_path())
    profile = store.create_profile("Lydia", tmp_path / "lib")
    store.set_active_profile(profile.id)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    window.show()
    items, ids = seed_board(store, window.profile, None, n=3)
    window.go("board")
    yield window, store, window.board, [ids[i.key] for i in items], monkeypatch, QInputDialog
    window.close()
    store.close()


def test_rename_add_and_remove_from_the_board(board):
    window, store, b, rows, monkeypatch, QInputDialog = board
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Ideas", True))
    b._rename_column("backlog")
    assert b.columns["backlog"].title.text() == "IDEAS"
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Filming", True))
    b._add_column("backlog")
    names = [c.name for c in b.column_config]
    assert names == ["Ideas", "Filming", "Staging", "Scheduled", "Published"] and len(b.columns) == 5
    filming = b.column_config[1]
    b.pipeline.move(rows[0], filming.primary)
    b.reload()
    assert [c.id for c in b.columns[filming.id].model.cards] == [rows[0]]
    b._remove_column(filming.id)                                    # its card moves to the neighbour
    assert store.get_video(rows[0]).stage == Stage.BACKLOG and len(b.columns) == 4
    assert b.columns["backlog"].model.rowCount() == 3
    window.switch_profile(store.create_profile("Andrew", store.get_profile(window.profile.id).output_folder))
    assert [c.name for c in b.column_config] == ["Backlog", "Staging", "Scheduled", "Published"]   # per profile


def test_edit_columns_dialog(board, monkeypatch):
    from reelay.gui.board_tools import ColumnsDialog
    _window, _store, b, _rows, *_ = board
    dialog = ColumnsDialog(b.column_config, {"backlog": 3})
    assert dialog.list.count() == 8                                 # name + "holds" hint per column
    assert "Holds Reelay's Backlog step · 3 cards" in dialog.list.item(1).text()
    dialog.list.setCurrentRow(4)                                    # Scheduled
    dialog._remove()
    dialog.list.setCurrentRow(0)
    dialog._add()
    columns, moves = dialog.result_columns()
    assert [c.name for c in columns] == ["Backlog", "New column", "Staging", "Published"] and moves == []
    assert "scheduled" in columns[2].stages                         # handed to its neighbour
    monkeypatch.setattr(ColumnsDialog, "exec", lambda self: ColumnsDialog.Accepted)
    monkeypatch.setattr(ColumnsDialog, "result_columns", lambda self: (columns, []))
    b.edit_columns()
    assert [c.name for c in b.column_config] == ["Backlog", "New column", "Staging", "Published"]
    assert b.columns["staging"].title.text() == "STAGING" and "Scheduled" not in [
        col.title.text().title() for col in b.columns.values()]


def test_dropping_within_a_merged_column_only_reorders(board):
    _window, store, b, rows, *_ = board
    cols = default_columns()
    remove(cols, "scheduled")                                       # Staging now also shows Scheduled cards
    b._apply_columns(cols)
    store.save_post(Post(video_row=rows[0], title="t"))
    store.set_stage([rows[0]], Stage.SCHEDULED)                     # e.g. scheduled by Reelay
    b.reload()
    assert rows[0] in [c.id for c in b.columns["staging"].model.cards]
    b._on_move([rows[0]], Stage.STAGING, 0)                         # dragged within the same column
    assert store.get_video(rows[0]).stage == Stage.SCHEDULED        # not "un-scheduled"
