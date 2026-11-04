"""Unit tests for the Repurpose with Muse Spark GUI button and review dialog."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from reelay.gui.post_panel import PostPanel
from reelay.gui.repurpose_dialog import RepurposeDialog
from reelay.gui.thumbnails import ThumbnailCache
from reelay.models import Post, Stage, Ticket, VideoItem, VideoRow
from reelay.privacy import DISCLOSURE_TEXT, is_cloud_ai_enabled, set_cloud_ai_enabled
from reelay.repurpose_engine import RepurposeNetworkError
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
        video_id=f"andrew-demo-{row_id:03d}",
        url=f"https://youtube.com/watch?v=andrew-demo-{row_id:03d}",
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


def test_repurpose_dialog_displays_disclosure_and_source_preview_snapshot(qtbot, tmp_path):
    store = Store(tmp_path / "test.db")
    captured_kwargs: dict[str, object] = {}

    def fake_generate(**kwargs):
        captured_kwargs.update(kwargs)
        return SYNTHETIC_PROPOSAL

    try:
        card = _make_andrew_card()
        dialog = RepurposeDialog(
            card,
            store,
            brand_voice="Direct, technical builder tone; grounded.",
            generate_fn=fake_generate,
        )
        qtbot.addWidget(dialog)

        assert DISCLOSURE_TEXT in dialog.disclosure_label.text()
        assert not dialog.opt_in_box.isChecked()
        assert dialog.source_preview.isReadOnly()
        preview_text = dialog.source_preview.toPlainText()
        assert "Title: Saved Title" in preview_text
        assert "Description: Saved Description" in preview_text
        assert "Local AI desktop tools · macos, python, ai" in preview_text
        assert "Reelay Repurpose Engine on macOS" in preview_text
        assert "Direct, technical builder tone; grounded." in preview_text
        assert not dialog.apply_btn.isEnabled()

        dialog.opt_in_box.setChecked(True)
        dialog._run()
        assert dialog.worker is not None
        dialog.worker.wait(2000)
        qtbot.waitUntil(lambda: dialog.apply_btn.isEnabled(), timeout=2000)

        assert captured_kwargs["title"] == "Saved Title"
        assert captured_kwargs["description"] == "Saved Description"
        assert captured_kwargs["notes"] == "Local AI desktop tools · macos, python, ai"
        assert "Reelay Repurpose Engine" in str(captured_kwargs["transcript"])
        assert captured_kwargs["brand_voice"] == "Direct, technical builder tone; grounded."
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


def test_unchecked_checkbox_blocks_even_when_environment_enables_cloud(qtbot, monkeypatch):
    monkeypatch.setenv("REELAY_CLOUD_AI_OPT_IN", "1")
    called: list[bool] = []
    store_dict: dict[str, str] = {}
    dialog = RepurposeDialog(
        store=store_dict,
        title="Example",
        generate_fn=lambda **_kw: called.append(True) or SYNTHETIC_PROPOSAL,
    )
    qtbot.addWidget(dialog)
    assert dialog.opt_in_box.isChecked()

    dialog.opt_in_box.setChecked(False)
    assert store_dict.get("privacy.cloud_ai_opt_in") == "0"

    dialog._run()
    if dialog.worker:
        dialog.worker.wait(2000)
        qtbot.wait(20)
    assert called == [], "Unchecked consent still starts generation when env opt-in is 1"
    assert "Cloud AI is disabled" in dialog.status.text()


def test_cancel_does_not_block_gui_while_request_is_running(qtbot, monkeypatch):
    monkeypatch.delenv("REELAY_CLOUD_AI_OPT_IN", raising=False)
    gate, started = threading.Event(), threading.Event()

    def slow_generate(**_kw):
        started.set()
        gate.wait(10)
        return SYNTHETIC_PROPOSAL

    dialog = RepurposeDialog(store={}, title="Example", generate_fn=slow_generate)
    qtbot.addWidget(dialog)
    dialog.opt_in_box.setChecked(True)
    dialog._run()
    try:
        qtbot.waitUntil(started.is_set, timeout=2000)
        start = time.monotonic()
        dialog.reject()
        elapsed = time.monotonic() - start
        assert elapsed < 0.2, f"Cancel blocked the GUI thread for {elapsed:.2f}s"
        assert dialog.proposal is None
    finally:
        gate.set()
        if dialog.worker:
            dialog.worker.wait(2000)
        qtbot.wait(20)
    assert dialog.proposal is None
    assert dialog._get_field_text("title") == ""


def test_apply_disabled_during_regeneration_and_preserved_on_failure(qtbot, monkeypatch):
    monkeypatch.delenv("REELAY_CLOUD_AI_OPT_IN", raising=False)
    gate, started = threading.Event(), threading.Event()
    should_fail = False

    def slow_generate(**_kw):
        started.set()
        gate.wait(10)
        if should_fail:
            raise RepurposeNetworkError("Simulated network failure during regeneration")
        return SYNTHETIC_PROPOSAL

    dialog = RepurposeDialog(store={}, title="Example", generate_fn=slow_generate)
    qtbot.addWidget(dialog)
    dialog.opt_in_box.setChecked(True)
    dialog.populate_proposal(SYNTHETIC_PROPOSAL)
    assert dialog.apply_btn.isEnabled()

    should_fail = True
    dialog._run()
    try:
        qtbot.waitUntil(started.is_set, timeout=2000)
        assert not dialog.apply_btn.isEnabled(), "Apply stays enabled while a replacement request is running"
        dialog._accept_proposal()
        assert dialog.proposal is None, "_accept_proposal must be guarded in code while busy"
    finally:
        gate.set()
        if dialog.worker:
            dialog.worker.wait(2000)
        qtbot.waitUntil(lambda: not dialog._busy, timeout=2000)

    assert dialog.apply_btn.isEnabled()
    assert dialog._get_field_text("title") == SYNTHETIC_PROPOSAL["title"]
    assert "Simulated network failure" in dialog.status.text()


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
        profile = store.create_profile("Andrew", tmp_path / "lib")
        card = _make_andrew_card(profile_id=profile.id)
        row_id = store.save_video(profile.id, card.row.item)
        card.id = row_id
        card.row.id = row_id
        saved_post = store.save_post(
            Post(
                video_row=row_id,
                title="Saved Title",
                description="Saved Description",
                tags=["saved"],
            )
        )
        card.post = saved_post

        panel = PostPanel(ThumbnailCache(tmp_path / "thumbs"))
        panel.store = store
        qtbot.addWidget(panel)

        save_signals: list[Post] = []
        field_commit_signals: list[tuple[int, str, object]] = []
        panel.saveRequested.connect(save_signals.append)
        panel.ticketFieldCommitted.connect(
            lambda rid, k, v: field_commit_signals.append((rid, k, v))
        )

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
        assert save_signals == []
        assert field_commit_signals == []

        persisted = store.get_post(row_id)
        assert persisted is not None
        assert persisted.title == "Saved Title"
        assert persisted.description == "Saved Description"
        assert persisted.tags == ["saved"]
        assert persisted.tiktok_text is None

        panel.save_btn.click()
        assert len(save_signals) == 1
        assert save_signals[0].title == SYNTHETIC_PROPOSAL["title"]
        assert save_signals[0].bluesky_text == SYNTHETIC_PROPOSAL["bluesky_text"]
    finally:
        store.close()


def test_changed_ticket_is_not_overwritten_by_old_proposal(qtbot, tmp_path, monkeypatch):
    store = Store(tmp_path / "audit.db")
    panel = PostPanel(ThumbnailCache(tmp_path / "thumbs"))
    panel.store = store
    qtbot.addWidget(panel)
    first, second = _make_andrew_card(row_id=1), _make_andrew_card(row_id=2)
    second.post.title = "Different ticket saved title"
    panel.show_card(first, [], False)

    def switch_ticket_and_accept(dialog):
        panel.show_card(second, [], False)
        dialog.populate_proposal(SYNTHETIC_PROPOSAL)
        dialog._accept_proposal()
        return RepurposeDialog.Accepted

    monkeypatch.setattr(RepurposeDialog, "exec", switch_ticket_and_accept)
    try:
        panel._repurpose()
        assert (
            panel.blocks["title"].text() == "Different ticket saved title"
        ), "Proposal for ticket 1 overwrote ticket 2 editor"
        assert "not applied" in panel.copied.text()
    finally:
        panel.close()
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
