"""Unit tests for the Repurpose with Muse Spark GUI button and review dialog."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from PySide6.QtWidgets import QLabel

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
    assert panel.repurpose_label.text() == "Repurpose"
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
    assert not dialog.opt_in_box.isChecked(), "Consent must start unticked even when the env var is 1"

    dialog.opt_in_box.setChecked(True)
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
        # Live "Posting to" selection (not saved yet): YouTube + Bluesky only.
        for p, chip in panel.platform_boxes.items():
            chip.setChecked(p in ("youtube", "bluesky"))
        panel.blocks["tiktok_text"].set_text("")
        panel.blocks["hashtags"].set_text("#existing")
        unsaved_before = panel.has_unsaved_changes()

        seen_platforms: list[tuple[str, ...]] = []

        def auto_accept(self):
            seen_platforms.append(self.platforms)
            self.populate_proposal(SYNTHETIC_PROPOSAL)
            self._accept_proposal()
            return RepurposeDialog.Accepted

        monkeypatch.setattr(RepurposeDialog, "exec", auto_accept)
        panel._repurpose()

        assert seen_platforms == [("youtube", "bluesky")]
        for key in ("title", "description", "tags", "bluesky_text"):
            assert panel.blocks[key].text() == SYNTHETIC_PROPOSAL[key]
        for key in ("tiktok_text", "instagram_text", "threads_text", "facebook_text"):
            assert panel.blocks[key].text() != SYNTHETIC_PROPOSAL[key], f"{key} overwritten though unselected"
        assert panel.blocks["hashtags"].text() == "#existing", "hidden shared hashtags were changed"
        _ = unsaved_before

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
        assert save_signals[0].tiktok_text != SYNTHETIC_PROPOSAL["tiktok_text"]
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


def test_python_m_reelay_entrypoint_delegates_to_gui_main(monkeypatch):
    import runpy
    import sys
    import pytest
    from reelay.gui import app as gui_app

    called: list[bool] = []
    monkeypatch.setattr(gui_app, "main", lambda: called.append(True) or 0)
    sys.modules.pop("reelay.__main__", None)
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("reelay", run_name="__main__")
    assert exc_info.value.code == 0
    assert called == [True]


def test_async_generation_completion_allows_edit_apply_and_regeneration(qtbot, monkeypatch):
    """After a real background run finishes and Qt deletes the CallWorker, the dialog stays usable.

    Regression: self.worker used to keep pointing at the deleted C++ CallWorker, so the next
    isRunning() check raised "Internal C++ object (CallWorker) already deleted".
    """
    from PySide6.QtCore import QCoreApplication, QEvent

    monkeypatch.delenv("REELAY_CLOUD_AI_OPT_IN", raising=False)
    calls: list[int] = []

    def fake_generate(**_kw):
        calls.append(1)
        return dict(SYNTHETIC_PROPOSAL)

    dialog = RepurposeDialog(store={}, title="Example", generate_fn=fake_generate)
    qtbot.addWidget(dialog)
    dialog.opt_in_box.setChecked(True)

    def flush_deferred_deletes():
        qtbot.wait(50)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        qtbot.wait(50)

    dialog._run()
    qtbot.waitUntil(lambda: not dialog._busy, timeout=3000)
    qtbot.waitUntil(lambda: not RepurposeDialog._in_flight_workers, timeout=3000)
    flush_deferred_deletes()

    assert dialog.worker is None
    assert dialog._get_field_text("title") == SYNTHETIC_PROPOSAL["title"]

    # Edit a field, then Apply: must not raise on the deleted worker.
    dialog._set_field_text("title", "Edited by Andrew")
    assert dialog._can_apply()
    assert dialog.apply_btn.isEnabled()
    dialog._accept_proposal()
    assert dialog.proposal is not None
    assert dialog.proposal["title"] == "Edited by Andrew"

    # A second generation must also start and finish cleanly.
    dialog._run()
    qtbot.waitUntil(lambda: len(calls) == 2 and not dialog._busy, timeout=3000)
    qtbot.waitUntil(lambda: not RepurposeDialog._in_flight_workers, timeout=3000)
    flush_deferred_deletes()
    assert dialog.worker is None
    assert dialog._can_apply()


# ---------------------------------------------------------------- destination-filtered review (redesign)
def _dialog_for(qtbot, platforms, generate_fn=None):
    dialog = RepurposeDialog(store={}, title="Example", platforms=platforms,
                             generate_fn=generate_fn or (lambda **_kw: dict(SYNTHETIC_PROPOSAL)))
    qtbot.addWidget(dialog)
    return dialog


def _shown(dialog):
    return [k for k, box in dialog.field_boxes.items() if not box.isHidden()]


def test_results_hidden_until_generation_and_consent_starts_unticked(qtbot):
    dialog = _dialog_for(qtbot, ["youtube"])
    assert not dialog.opt_in_box.isChecked()
    assert dialog.results.isHidden() and not dialog.placeholder.isHidden()
    assert all(section.isHidden() for section in dialog.sections.values())
    assert "Source Snapshot — Read-only" in [lab.text().replace("<b>", "").replace("</b>", "")
                                             for lab in dialog.findChildren(QLabel)]
    assert not dialog.apply_btn.isEnabled()


def test_youtube_only_shows_and_applies_only_youtube_fields(qtbot):
    dialog = _dialog_for(qtbot, ["youtube"])
    dialog.populate_proposal(SYNTHETIC_PROPOSAL)
    assert list(dialog.sections) == ["youtube"]
    assert _shown(dialog) == ["title", "description", "tags"]
    assert "hashtags" not in dialog.editors
    dialog._accept_proposal()
    assert dialog.proposal == {k: SYNTHETIC_PROPOSAL[k] for k in ("title", "description", "tags")}


def test_youtube_and_instagram_group_by_platform_with_shared_hashtags(qtbot):
    dialog = _dialog_for(qtbot, ["instagram", "youtube"])
    dialog.populate_proposal(SYNTHETIC_PROPOSAL)
    assert [k for k, s in dialog.sections.items() if not s.isHidden()] == ["youtube", "instagram", "_shared"]
    assert set(_shown(dialog)) == {"title", "description", "tags", "instagram_text", "hashtags"}
    dialog._accept_proposal()
    assert set(dialog.proposal) == {"title", "description", "tags", "instagram_text", "hashtags"}


def test_bluesky_only_counts_graphemes_and_threads_only_flags_hashtags(qtbot):
    bluesky = _dialog_for(qtbot, ["bluesky"])
    bluesky.populate_proposal(SYNTHETIC_PROPOSAL)
    assert _shown(bluesky) == ["bluesky_text"]
    assert "graphemes · PASS" in bluesky.counters["bluesky_text"].text()

    threads = _dialog_for(qtbot, ["threads"])
    threads.populate_proposal(SYNTHETIC_PROPOSAL)
    assert _shown(threads) == ["threads_text"]
    threads._set_field_text("threads_text", "Short post #tag")
    assert "remove #" in threads.counters["threads_text"].text()
    assert not threads.apply_btn.isEnabled()


def test_no_destination_selected_blocks_generation(qtbot):
    called = []
    dialog = _dialog_for(qtbot, [], generate_fn=lambda **_kw: called.append(1) or SYNTHETIC_PROPOSAL)
    dialog.opt_in_box.setChecked(True)
    assert not dialog.run_btn.isEnabled()
    dialog._run()
    assert called == []
    assert "at least one destination" in dialog.status.text().lower()


def test_empty_returned_field_is_hidden_and_never_applied(qtbot):
    partial = dict(SYNTHETIC_PROPOSAL, tags="", description="   ")
    dialog = _dialog_for(qtbot, ["youtube"])
    dialog.populate_proposal(partial)
    assert _shown(dialog) == ["title"]
    dialog._accept_proposal()
    assert dialog.proposal == {"title": SYNTHETIC_PROPOSAL["title"]}


def test_regeneration_failure_keeps_previous_result_visible(qtbot, monkeypatch):
    monkeypatch.delenv("REELAY_CLOUD_AI_OPT_IN", raising=False)
    gate, started = threading.Event(), threading.Event()

    def failing(**_kw):
        started.set()
        gate.wait(10)
        raise RepurposeNetworkError("Simulated timeout")

    dialog = _dialog_for(qtbot, ["youtube"], generate_fn=failing)
    dialog.populate_proposal(SYNTHETIC_PROPOSAL)
    dialog.opt_in_box.setChecked(True)
    dialog._run()
    qtbot.waitUntil(started.is_set, timeout=2000)
    assert not dialog.busy_bar.isHidden()
    assert dialog._get_field_text("title") == SYNTHETIC_PROPOSAL["title"]
    assert not dialog.apply_btn.isEnabled()
    gate.set()
    qtbot.waitUntil(lambda: not dialog._busy, timeout=3000)
    assert dialog.busy_bar.isHidden()
    assert dialog._get_field_text("title") == SYNTHETIC_PROPOSAL["title"]
    assert "previous proposal is unchanged" in dialog.status.text()
    assert dialog.apply_btn.isEnabled()


def test_launch_button_fits_at_narrow_inspector_width(qtbot, tmp_path):
    panel = PostPanel(ThumbnailCache(tmp_path / "thumbs"))
    qtbot.addWidget(panel)
    panel.resize(340, 900)
    panel.show()
    qtbot.wait(20)
    btn = panel.repurpose_btn
    assert btn.width() >= btn.sizeHint().width(), "Muse Spark button is narrower than its text"
    assert btn.fontMetrics().horizontalAdvance(btn.text()) < btn.width()
