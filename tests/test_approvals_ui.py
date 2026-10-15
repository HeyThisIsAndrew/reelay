"""Deciding AI assistants' requests in the app (approvals_dialog.py), and what the Board's
status colours mean. The protections are the ones approvals.py already enforces: a person
decides, an expired request can't be approved, a video that changed makes the request stale,
the app carries an approved request out once, and a request that skips the Ready check
never passes as an ordinary one."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtWidgets import QMessageBox

from reelay import approvals
from reelay.gui import theme
from reelay.models import ApprovalStatus
from reelay.scoped_pipeline import ScopedPipeline, request_approval
from tests.fakes import disable_publishing, make_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    disable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch)
    yield w
    w.store.close()


def future(days=2):
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0)


def forced_schedule(store, profile_id, row):
    """A request to schedule even where the Ready check isn't passing (an agent sent force=true)."""
    return request_approval(store, profile_id, "schedule",
                            {"video_id": row, "scheduled_at": future().isoformat(), "platforms": ["youtube"],
                             "add_shorts": None, "force": True}, [row])


# ------------------------------------------------------------------ plain-language summaries
def test_explain_says_what_approving_would_do(world):
    sp = ScopedPipeline(world.pipeline)
    normal = world.store.get_approval(sp.schedule(world.rows[0], scheduled_at=future()).approval_id)
    assert not approvals.skips_ready_check(normal)
    assert approvals.explain(world.store, normal)[0].startswith("Schedule it on")
    assert not any("Ready check" in line for line in approvals.explain(world.store, normal))

    forced = world.store.get_approval(forced_schedule(world.store, world.profile.id, world.rows[1]).approval_id)
    assert approvals.skips_ready_check(forced)
    assert any(line.startswith("Skips the Ready check") for line in approvals.explain(world.store, forced))

    archive = world.store.get_approval(sp.archive(world.rows[:2]).approval_id)
    assert approvals.explain(world.store, archive) == ["Archive 2 videos."]


# ------------------------------------------------------------------ the window
@pytest.fixture
def dialog_for(qtbot, world):
    from reelay.gui.approvals_dialog import ApprovalsDialog

    def make(**kw):
        d = ApprovalsDialog(world.pipeline, world.profile, **kw)
        qtbot.addWidget(d)
        return d
    return make


def test_approve_in_the_app_then_the_dispatcher_carries_it_out_once(world, dialog_for):
    pending = ScopedPipeline(world.pipeline).archive([world.rows[0]])
    d = dialog_for()
    assert d.list.count() == 1 and not d.force.isVisible() and d.approve_btn.text() == "Approve"
    d.approve_btn.click()
    req = world.store.get_approval(pending.approval_id)
    assert req.status == ApprovalStatus.APPROVED and req.decided_by.endswith("(Reelay app)")
    assert world.store.get_video(world.rows[0]).archived_at is None        # deciding executes nothing
    assert d.list.count() == 0 and d.empty.isVisibleTo(d) and d.decided == 1
    [done] = approvals.execute_approved(world.pipeline)
    assert done.status == ApprovalStatus.EXECUTED and world.store.get_video(world.rows[0]).archived_at is not None
    assert approvals.execute_approved(world.pipeline) == []                # never twice


def test_a_changed_video_makes_the_request_stale_and_nothing_happens(world, dialog_for):
    pending = ScopedPipeline(world.pipeline).bulk_update([world.rows[1]], {"topic": "from the agent"})
    d = dialog_for()
    world.pipeline.update_metadata(world.rows[1], {"title": "Edited while it waited"})
    d.approve_btn.click()
    assert world.store.get_approval(pending.approval_id).status == ApprovalStatus.STALE
    assert d.notice.text() == approvals.STALE_REASON
    assert approvals.execute_approved(world.pipeline) == []
    assert world.store.get_video(world.rows[1]).ticket.topic != "from the agent"


def test_expired_requests_are_not_offered(world, dialog_for):
    pending = ScopedPipeline(world.pipeline).archive([world.rows[0]])
    world.store.conn.execute("UPDATE approval_requests SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
                             (pending.approval_id,))
    world.store.conn.commit()
    d = dialog_for()
    assert d.list.count() == 0 and not d.detail.isVisibleTo(d)
    assert world.store.get_approval(pending.approval_id).status == ApprovalStatus.EXPIRED


def test_a_request_decided_elsewhere_is_not_decided_twice(world, dialog_for):
    pending = ScopedPipeline(world.pipeline).archive([world.rows[0]])
    d = dialog_for()
    approvals.reject(world.store, pending.approval_id, "someone at the terminal")   # while the window is open
    d.approve_btn.click()
    req = world.store.get_approval(pending.approval_id)
    assert req.status == ApprovalStatus.REJECTED and req.decided_by == "someone at the terminal"
    assert "not waiting" in d.notice.text()


def test_reject_with_a_reason(world, dialog_for):
    pending = ScopedPipeline(world.pipeline).archive([world.rows[0]])
    d = dialog_for()
    d.reason.setText("Not this one")
    d.reject_btn.click()
    req = world.store.get_approval(pending.approval_id)
    assert req.status == ApprovalStatus.REJECTED and req.result == {"reason": "Not this one"}
    assert approvals.execute_approved(world.pipeline) == []


def test_skipping_the_ready_check_is_labelled_and_needs_a_second_yes(world, dialog_for, monkeypatch):
    pending = forced_schedule(world.store, world.profile.id, world.rows[0])
    d = dialog_for()
    assert d.list.item(0).text().startswith("⚠ ")
    assert d.force.isVisibleTo(d) and d.approve_btn.text() == "Approve Anyway…"
    assert "Skips the Ready check" in d.force_title.text()
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a) or QMessageBox.Cancel)
    d.approve_btn.click()
    assert asked and world.store.get_approval(pending.approval_id).status == ApprovalStatus.PENDING
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    d.approve_btn.click()
    assert world.store.get_approval(pending.approval_id).status == ApprovalStatus.APPROVED


def test_a_view_only_database_cannot_decide(world, dialog_for):
    pending = ScopedPipeline(world.pipeline).archive([world.rows[0]])
    world.store.read_only = True
    try:
        d = dialog_for()
        assert d.list.count() == 1 and d.view_only.isVisibleTo(d)
        assert not d.approve_btn.isEnabled() and not d.reject_btn.isEnabled()
        d.approve_btn.click()
    finally:
        world.store.read_only = False
    assert world.store.get_approval(pending.approval_id).status == ApprovalStatus.PENDING


def test_dialog_opens_on_the_selected_videos_request(world, dialog_for):
    sp = ScopedPipeline(world.pipeline)
    sp.archive([world.rows[0]])
    second = sp.bulk_update([world.rows[2]], {"topic": "x"})
    d = dialog_for(focus_video=world.rows[2])
    assert d.current().id == second.approval_id


# ------------------------------------------------------------------ the Board
@pytest.mark.gui
def test_board_shows_waiting_requests(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from reelay.gui.app import create_window
    from reelay.gui.board_page import CardDelegate
    from reelay.models import Stage
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    from tests.test_gui import seed_board
    disable_publishing(monkeypatch)
    store = Store(db_path())
    profile = store.create_profile("Lydia", tmp_path / "lib")
    store.set_active_profile(profile.id)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    window.show()
    try:
        items, ids = seed_board(store, window.profile, None, n=2)
        row = ids[items[0].key]
        window.go("board")
        b = window.board
        assert not b.approvals_btn.isVisible()
        ScopedPipeline(b.pipeline).archive([row])
        qtbot.waitUntil(lambda: b.approvals_btn.isVisible(), timeout=3000)     # the store's change feed
        assert b.approvals_btn.text() == "Approvals · 1"
        card = next(c for c in b.columns[Stage.BACKLOG].model.cards if c.id == row)
        text, color = CardDelegate._status(card)
        assert text == "⏳ Waiting for your approval: Archive / unarchive" and color == theme.P.warning_text
        col = b.columns[Stage.BACKLOG]
        col.view.setCurrentIndex(col.model.index(col.model.cards.index(card)))
        assert b.approval_bar.isVisible() and "Archive / unarchive" in b.approval_text.text()
        approvals.reject(store, store.list_approvals()[0].id, "test")
        qtbot.waitUntil(lambda: not b.approvals_btn.isVisible(), timeout=3000)
    finally:
        window.close()
        store.close()


def test_a_waiting_request_shows_amber_on_the_card(world):
    from reelay.gui.board_page import CardDelegate
    card = world.pipeline.card(world.rows[0])
    card.pending_approvals = ["Schedule"]
    text, color = CardDelegate._status(card)
    assert text == "⏳ Waiting for your approval: Schedule" and color == theme.P.warning_text
