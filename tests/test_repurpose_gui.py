"""Unit tests for the Repurpose with Muse Spark GUI button and review dialog."""

from __future__ import annotations

from types import SimpleNamespace

from reelay.gui.post_panel import PostPanel
from reelay.gui.repurpose_dialog import RepurposeDialog
from reelay.gui.thumbnails import ThumbnailCache
from reelay.models import Post, Stage, Ticket, VideoItem, VideoRow
from reelay.privacy import DISCLOSURE_TEXT, is_cloud_ai_enabled, set_cloud_ai_enabled
from reelay.store import Store

SYNTHETIC_PROPOSAL: dict[str, str] = {
    "title": "Building Local AI Desktop Tools on macOS",
    "description": "Walking through local-first media management and agent APIs with Meta Muse Spark.",
    "tags": "macos, local ai, pyside6, muse spark",
    "tiktok_text": "How we built a local-first AI desktop app on macOS.",
    "instagram_text": "Local-first video tooling on macOS powered by Meta Muse Spark.",
    "bluesky_text": "Testing our local-first macOS video workflow with Meta Muse Spark (under 300 graphemes).",
    "threads_text": "Building local-first creator tools on macOS where AI proposes and the human decides.",
    "facebook_text": "Full walkthrough of our local-first macOS desktop workflow using Meta Muse Spark.",
    "hashtags": "#macOS #LocalAI #MuseSpark",
}


def _make_andrew_card(row_id: int = 1, profile_id: int = 1):
    item = VideoItem(
        platform="YouTube",
        video_id="andrew-demo-001",
        url="https://youtube.com/watch?v=andrew-demo-001",
        title="Original Video Title",
        description="Original Video Description",
        uploader="Andrew Baxter",
        upload_date="20261104",
        duration=120,
    )
    ticket = Ticket(
        transcript="Hey everyone, today we are testing the Reelay Repurpose Engine on macOS.",
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
        title="Saved Title",
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


def test_repurpose_button_present_in_post_panel(qtbot, tmp_path):
    panel = PostPanel(ThumbnailCache(tmp_path / "thumbs"))
    qtbot.addWidget(panel)
    assert hasattr(panel, "repurpose_btn")
    assert panel.repurpose_btn.text() == "Repurpose with Muse Spark"
    assert "Meta Muse Spark" in panel.repurpose_btn.toolTip()


def test_repurpose_dialog_displays_disclosure_and_extracts_inputs(qtbot, tmp_path):
    store = Store(tmp_path / "test.db")
    try:
        card = _make_andrew_card()
        dialog = RepurposeDialog(card, store)
        qtbot.addWidget(dialog)

        assert DISCLOSURE_TEXT in dialog.disclosure_label.text()
        assert not dialog.opt_in_box.isChecked()
        assert dialog.source_title == "Saved Title"
        assert dialog.source_description == "Saved Description"
        assert "Reelay Repurpose Engine" in dialog.source_transcript
        assert "Local AI desktop tools" in dialog.source_notes
        assert not dialog.apply_btn.isEnabled()
    finally:
        store.close()


def test_repurpose_dialog_blocks_when_cloud_ai_disabled(qtbot, tmp_path, monkeypatch):
    monkeypatch.delenv("REELAY_CLOUD_AI_OPT_IN", raising=False)
    store = Store(tmp_path / "test.db")
    called = False

    def fake_generate(**_kwargs):
        nonlocal called
        called = True
        return SYNTHETIC_PROPOSAL

    try:
        card = _make_andrew_card()
        dialog = RepurposeDialog(card, store, generate_fn=fake_generate)
        qtbot.addWidget(dialog)

        dialog._run()
        assert not called
        assert "Cloud AI is disabled" in dialog.status.text()
    finally:
        store.close()


def test_repurpose_dialog_opt_in_and_populate_proposal(qtbot, tmp_path):
    store = Store(tmp_path / "test.db")
    try:
        card = _make_andrew_card()
        dialog = RepurposeDialog(card, store)
        qtbot.addWidget(dialog)

        dialog.opt_in_box.setChecked(True)
        assert is_cloud_ai_enabled(store)

        dialog.populate_proposal(SYNTHETIC_PROPOSAL)
        assert dialog.apply_btn.isEnabled()
        for key, expected in SYNTHETIC_PROPOSAL.items():
            assert dialog._get_field_text(key) == expected
            assert "PASS" in dialog.counters[key].text()

        dialog._accept_proposal()
        assert dialog.proposal == SYNTHETIC_PROPOSAL
    finally:
        store.close()


def test_post_panel_repurpose_applies_to_blocks_without_auto_save(qtbot, tmp_path, monkeypatch):
    store = Store(tmp_path / "test.db")
    try:
        set_cloud_ai_enabled(store, True)
        panel = PostPanel(ThumbnailCache(tmp_path / "thumbs"))
        panel.store = store
        qtbot.addWidget(panel)

        card = _make_andrew_card()
        panel.show_card(card, [], False)
        assert not panel.has_unsaved_changes()

        def auto_accept(self):
            self.populate_proposal(SYNTHETIC_PROPOSAL)
            self._accept_proposal()
            return RepurposeDialog.Accepted

        monkeypatch.setattr(RepurposeDialog, "exec", auto_accept)
        panel._repurpose()

        for key, expected in SYNTHETIC_PROPOSAL.items():
            assert panel.blocks[key].text() == expected

        assert panel.has_unsaved_changes()
        assert "Click Save Changes to commit" in panel.copied.text()
        assert card.post.title == "Saved Title"
    finally:
        store.close()


def test_post_panel_repurpose_cancel_leaves_blocks_unchanged(qtbot, tmp_path, monkeypatch):
    store = Store(tmp_path / "test.db")
    try:
        panel = PostPanel(ThumbnailCache(tmp_path / "thumbs"))
        panel.store = store
        qtbot.addWidget(panel)

        card = _make_andrew_card()
        panel.show_card(card, [], False)

        monkeypatch.setattr(RepurposeDialog, "exec", lambda self: RepurposeDialog.Rejected)
        panel._repurpose()

        assert panel.blocks["title"].text() == "Saved Title"
        assert not panel.has_unsaved_changes()
    finally:
        store.close()
