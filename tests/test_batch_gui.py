"""GUI tests for BatchReviewDialog and Batch Repurpose workflow.

Uses pytest-qt / qtbot.
Post-hackathon product work for Reelay (branch batch-v2).
Founder: Andrew Baxter. Local-first desktop tool for video creators.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

from reelay.gui.batch_dialog import (
    JOB_STATUS_APPLIED,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_READY,
    JOB_STATUS_SAVED,
    BatchReviewDialog,
    BatchTicket,
)
from reelay.gui.thumbnails import ThumbnailCache
from reelay.models import Post, Stage, Ticket, VideoItem, VideoRow
from reelay.privacy import DISCLOSURE_TEXT, is_cloud_ai_enabled, set_cloud_ai_enabled
from reelay.repurpose_engine import RepurposeNetworkError
from reelay.store import Store

SYNTHETIC_PROPOSAL = {
    "title": "Building Local AI Desktop Apps on macOS",
    "description": "Walking through local-first media management and agent APIs with Meta Muse Spark.",
    "tags": "macos, local ai, pyside6, muse spark",
    "tiktok_text": "How we built a local-first AI desktop app on macOS.",
    "instagram_text": "Local-first video tooling on macOS powered by Meta Muse Spark.",
    "bluesky_text": "Testing our local-first macOS video workflow with Meta Muse Spark (under 300 graphemes).",
    "threads_text": "Building local-first creator tools on macOS where AI proposes and the human decides.",
    "facebook_text": "Full walkthrough of our local-first macOS desktop workflow using Meta Muse Spark.",
    "hashtags": "#macOS #LocalAI #MuseSpark",
}

OVER_LIMIT_PROPOSAL = {
    "title": "A" * 150,  # YouTube title limit is 100 characters -> OVER LIMIT!
    "description": "Short description",
    "tags": "tags",
    "tiktok_text": "Short hook",
    "instagram_text": "Short caption",
    "bluesky_text": "Short bluesky text",
    "threads_text": "Short threads text",
    "facebook_text": "Short facebook text",
    "hashtags": "#Test",
}


def _make_andrew_card(row_id: int = 1, profile_id: int = 1):
    item = VideoItem(
        platform="YouTube",
        video_id=f"andrew-demo-{row_id:03d}",
        url=f"https://youtube.com/watch?v=andrew-demo-{row_id:03d}",
        title=f"Andrew Video Title {row_id}",
        description=f"Original Video Description {row_id}",
        uploader="Andrew Baxter",
        upload_date="20261104",
        duration=120,
    )
    ticket = Ticket(
        transcript=f"Hey everyone, today we are testing video {row_id} with Meta Muse Spark on macOS.",
        topic="Local AI desktop tools",
        keywords="macos, python, ai",
        creator="Andrew Baxter",
    )
    row = VideoRow(
        id=row_id,
        profile_id=profile_id,
        stage=Stage.BACKLOG,
        item=item,
        ticket=ticket,
    )
    post = Post(
        video_row=row_id,
        title=f"Saved Title {row_id}",
        description="Saved Description",
        tags=["saved"],
    )
    return SimpleNamespace(
        id=row_id,
        row=row,
        post=post,
        sounds={},
        readiness=None,
        dispatches=[],
        queued=False,
        progress=0.0,
        account=None,
    )


# ======================================================================
# 1. Dialog Instantiation and Display with Queued Tickets
# ======================================================================
def test_batch_review_dialog_instantiation(qtbot, tmp_path):
    """BatchReviewDialog instantiates cleanly with queued tickets and proper creator header."""
    store = Store(tmp_path / "test_gui_batch.db")
    try:
        cards = [_make_andrew_card(1), _make_andrew_card(2), _make_andrew_card(3)]
        dialog = BatchReviewDialog(cards=cards, store=store)
        qtbot.addWidget(dialog)

        # Header asserts
        assert "Repurpose these videos (3 videos)" in dialog.windowTitle() or any(
            "Repurpose these videos (3 videos)" in w.text()
            for w in dialog.findChildren(QLabel)
        )
        # Opt-in checkbox starts unchecked by default
        assert not dialog.opt_in_box.isChecked()

        # All 3 tickets present in queue
        assert len(dialog.tickets) == 3
        assert len(dialog.row_widgets) == 3
        for cid in [1, 2, 3]:
            row_w = dialog.row_widgets[cid]
            assert row_w.ticket.title == f"Saved Title {cid}"
            assert row_w.status_pill.text() == "Queued"
            assert row_w.review_pill.text() == "Unreviewed"
            assert row_w.persistence_pill.text() == "In Memory Only"
    finally:
        store.close()


# ======================================================================
# 2. Platform Logo Chips Visible At All Times
# ======================================================================
def test_platform_logo_chips_visible(qtbot, tmp_path):
    """Platform logo chips (YouTube, TikTok, Instagram, Bluesky, Threads, Facebook) are visible in each row."""
    store = Store(tmp_path / "test_chips.db")
    try:
        cards = [_make_andrew_card(1)]
        dialog = BatchReviewDialog(cards=cards, store=store)
        qtbot.addWidget(dialog)

        row_w = dialog.row_widgets[1]
        # Inspect child labels of row widget for pixmaps
        pixmap_labels = [lbl for lbl in row_w.findChildren(QLabel) if lbl.pixmap() and not lbl.pixmap().isNull()]
        # We expect at least the thumbnail + 6 platform logo chips = 7 pixmaps
        assert len(pixmap_labels) >= 7

        # Check tooltips on platform chips
        tooltips = [lbl.toolTip() for lbl in pixmap_labels]
        assert any("YouTube" in t for t in tooltips)
        assert any("TikTok" in t for t in tooltips)
        assert any("Instagram" in t for t in tooltips)
        assert any("Bluesky" in t for t in tooltips)
        assert any("Threads" in t for t in tooltips)
        assert any("Facebook" in t for t in tooltips)
    finally:
        store.close()


# ======================================================================
# 3. PASS and OVER LIMIT Pills Rendered Properly
# ======================================================================
def test_compliance_pills_pass_and_over_limit(qtbot, tmp_path):
    """PASS and OVER LIMIT pills rendered properly based on character and grapheme counts."""
    store = Store(tmp_path / "test_pills.db")
    try:
        cards = [_make_andrew_card(1), _make_andrew_card(2)]
        dialog = BatchReviewDialog(cards=cards, store=store)
        qtbot.addWidget(dialog)
        dialog.show()

        row_1 = dialog.row_widgets[1]
        row_2 = dialog.row_widgets[2]

        # Initially compliance pill is hidden before proposal
        assert row_1.compliance_pill.isHidden()

        # Ticket 1 receives valid proposal -> PASS
        dialog._on_worker_ticket_succeeded(1, SYNTHETIC_PROPOSAL, None)
        assert not row_1.compliance_pill.isHidden()
        assert row_1.compliance_pill.text() == "PASS"
        assert row_1.compliance_pill.property("state") == "pass"

        # Ticket 2 receives over-limit proposal -> OVER LIMIT
        dialog._on_worker_ticket_succeeded(2, OVER_LIMIT_PROPOSAL, None)
        assert not row_2.compliance_pill.isHidden()
        assert row_2.compliance_pill.text() == "OVER LIMIT"
        assert row_2.compliance_pill.property("state") == "fail"
    finally:
        store.close()


# ======================================================================
# 4. Status Updates as Jobs Complete (Ready, Failed)
# ======================================================================
def test_status_updates_as_jobs_complete(qtbot, tmp_path):
    """Status updates as jobs complete (Ready, Failed, Cancelled)."""
    store = Store(tmp_path / "test_status.db")
    try:
        cards = [_make_andrew_card(1), _make_andrew_card(2)]
        dialog = BatchReviewDialog(cards=cards, store=store)
        qtbot.addWidget(dialog)
        dialog.show()

        row_1 = dialog.row_widgets[1]
        row_2 = dialog.row_widgets[2]

        # Simulate worker events
        dialog._on_worker_ticket_started(1)
        assert row_1.status_pill.text() == "Running"

        dialog._on_worker_ticket_succeeded(1, SYNTHETIC_PROPOSAL, None)
        assert row_1.status_pill.text() == "Ready"
        assert row_1.ticket.job.status == JOB_STATUS_READY

        dialog._on_worker_ticket_started(2)
        assert row_2.status_pill.text() == "Running"

        dialog._on_worker_ticket_failed(2, "Rate limit reached (429)")
        assert row_2.status_pill.text() == "Failed"
        assert row_2.ticket.job.status == JOB_STATUS_FAILED
        assert "429" in row_2.status_pill.toolTip()
        assert not row_2.retry_btn.isHidden()
    finally:
        store.close()


# ======================================================================
# 5. Apply Reviewed Only Button State and Safety Logic
# ======================================================================
def test_apply_reviewed_only_button_state_and_safety(qtbot, tmp_path):
    """'Apply Reviewed Only' button is disabled when 0 tickets reviewed, and enabled when >= 1 ticket reviewed.

    Crucially, it applies ONLY reviewed tickets and strictly ignores unreviewed tickets!
    """
    store = Store(tmp_path / "test_reviewed.db")
    try:
        cards = [_make_andrew_card(1), _make_andrew_card(2)]
        dialog = BatchReviewDialog(cards=cards, store=store)
        qtbot.addWidget(dialog)

        # Initially 0 tickets reviewed -> button disabled
        assert not dialog.apply_reviewed_btn.isEnabled()
        assert "Apply Reviewed Only (0 of 2)" in dialog.apply_reviewed_btn.text()

        # Both jobs finish generating and become Ready
        dialog._on_worker_ticket_succeeded(1, SYNTHETIC_PROPOSAL, None)
        dialog._on_worker_ticket_succeeded(2, SYNTHETIC_PROPOSAL, None)

        # Both are Ready, but 0 are reviewed yet -> button still disabled!
        assert not dialog.apply_reviewed_btn.isEnabled()

        # Creator opens and reviews ticket 1 only
        dialog.tickets_by_id[1].reviewed = True
        dialog.row_widgets[1].refresh()
        dialog._update_batch_buttons()

        # Now 1 ticket reviewed -> button ENABLED
        assert dialog.apply_reviewed_btn.isEnabled()
        assert "Apply Reviewed Only (1 of 2)" in dialog.apply_reviewed_btn.text()

        # Click Apply Reviewed Only
        dialog.apply_reviewed_only()

        # Ticket 1 must be APPLIED
        assert dialog.tickets_by_id[1].applied is True
        assert dialog.tickets_by_id[1].job.status == JOB_STATUS_APPLIED
        assert dialog.row_widgets[1].status_pill.text() == "Applied"

        # Ticket 2 must REMAIN UNAPPLIED (strictly ignored because not reviewed!)
        assert dialog.tickets_by_id[2].applied is False
        assert dialog.tickets_by_id[2].job.status == JOB_STATUS_READY
        assert dialog.row_widgets[2].status_pill.text() == "Ready"

        # Apply Reviewed Only button should now be disabled (all reviewed tickets are applied)
        assert not dialog.apply_reviewed_btn.isEnabled()
        assert "Apply Reviewed Only (0 of 2)" in dialog.apply_reviewed_btn.text()
    finally:
        store.close()


# ======================================================================
# 6. Save Changes Per Ticket
# ======================================================================
def test_save_changes_per_ticket(qtbot, tmp_path):
    """Save Changes commits applied in-memory copy to the database ticket record."""
    store = Store(tmp_path / "test_save.db")
    try:
        profile = store.create_profile("Andrew", tmp_path / "library")
        card = _make_andrew_card(row_id=1, profile_id=profile.id)
        row_id = store.save_video(profile.id, card.row.item)
        card.id = row_id
        card.row.id = row_id
        post = store.save_post(Post(video_row=row_id, title="Old Title", description="Old Desc",
                                    target_platforms=["youtube", "tiktok", "bluesky"],
                                    instagram_text="My own Instagram caption"))
        card.post = post

        dialog = BatchReviewDialog(cards=[card], store=store)
        qtbot.addWidget(dialog)

        # Deliver proposal
        dialog._on_worker_ticket_succeeded(row_id, SYNTHETIC_PROPOSAL, None)
        ticket = dialog.tickets_by_id[row_id]
        ticket.reviewed = True
        dialog.apply_reviewed_only()

        assert ticket.applied is True
        assert ticket.saved is False

        # Now save changes
        saved_ok = dialog.save_single_ticket(row_id)
        assert saved_ok is True
        assert ticket.saved is True
        assert ticket.job.status == JOB_STATUS_SAVED
        assert dialog.row_widgets[row_id].persistence_pill.text() == "Saved"

        # Verify persisted post in database has updated copy
        saved_post = store.get_post(row_id)
        assert saved_post is not None
        assert saved_post.title == SYNTHETIC_PROPOSAL["title"]
        assert saved_post.description == SYNTHETIC_PROPOSAL["description"]
        assert saved_post.tiktok_text == SYNTHETIC_PROPOSAL["tiktok_text"]
        assert saved_post.bluesky_text == SYNTHETIC_PROPOSAL["bluesky_text"]
        # Not in Posting to: left exactly as the creator had it
        assert saved_post.instagram_text == "My own Instagram caption"
        assert saved_post.threads_text is None and saved_post.facebook_text is None
    finally:
        store.close()


def test_batch_save_refuses_when_ticket_changed_after_batch_started(qtbot, tmp_path):
    """An edit made elsewhere while the batch is open is never overwritten by Batch Save."""
    store = Store(tmp_path / "test_stale.db")
    try:
        profile = store.create_profile("Andrew", tmp_path / "library")
        card = _make_andrew_card(row_id=1, profile_id=profile.id)
        row_id = store.save_video(profile.id, card.row.item)
        card.id = row_id
        card.row.id = row_id
        card.post = store.save_post(Post(video_row=row_id, title="Old Title", target_platforms=["youtube"]))

        dialog = BatchReviewDialog(cards=[card], store=store)
        qtbot.addWidget(dialog)
        dialog._on_worker_ticket_succeeded(row_id, SYNTHETIC_PROPOSAL, None)
        ticket = dialog.tickets_by_id[row_id]
        ticket.reviewed = True
        dialog.apply_reviewed_only()

        # The creator edits the same ticket in the normal panel while the batch is open.
        from dataclasses import replace
        store.save_post(replace(store.get_post(row_id), description="Edited by hand meanwhile"))

        assert dialog.save_single_ticket(row_id) is False
        assert ticket.saved is False
        latest = store.get_post(row_id)
        assert latest.description == "Edited by hand meanwhile"
        assert latest.title == "Old Title"
        assert "changed after the batch started" in dialog.footer_note.text()
    finally:
        store.close()


# ======================================================================
# 7. Disclosure Pop-over Overlay
# ======================================================================
def test_disclosure_popover_overlay_toggle(qtbot, tmp_path):
    """Clicking 'ⓘ  How it works' toggles technical disclosure overlay with DISCLOSURE_TEXT."""
    store = Store(tmp_path / "test_disc.db")
    try:
        dialog = BatchReviewDialog(cards=[_make_andrew_card(1)], store=store)
        qtbot.addWidget(dialog)
        dialog.show()

        assert not dialog.disclosure_overlay.isVisible()

        # Click info button
        dialog.info_btn.click()
        assert dialog.disclosure_overlay.isVisible()
        assert DISCLOSURE_TEXT in dialog.disclosure_label.text()

        # Click again to close
        dialog._toggle_disclosure()
        assert not dialog.disclosure_overlay.isVisible()
    finally:
        store.close()
