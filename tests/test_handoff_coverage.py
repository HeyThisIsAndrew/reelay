"""Prepare to Publish (the guided hand-off) and the coverage map on cards.
Nothing opens a real browser or Finder; nothing leaves this machine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reelay import coverage, handoff
from reelay.models import Platform, Post, Stage
from reelay.pipeline import PipelineError
from tests.fakes import disable_publishing, make_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    disable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch)
    yield w
    w.store.close()


# ------------------------------------------------------------------ plan
def test_caption_adds_the_shared_hashtags_once():
    post = Post(video_row=1, description="desc", tiktok_text="Sunnies for every adventure #summer",
                hashtags="#summer #polarized")
    assert handoff.caption_for(post, "tiktok") == "Sunnies for every adventure #summer\n\n#polarized"
    assert handoff.caption_for(post, "instagram") == "desc\n\n#summer #polarized"     # falls back, with tags
    assert handoff.caption_for(post, "youtube") == "desc"


def test_prepare_checks_file_caption_and_limits(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    ok = handoff.prepare(Post(video_row=1, instagram_text="Hello"), "instagram", video, 30)
    assert not ok.blocked and ok.video == video and ok.url.startswith("https://www.instagram.com")
    fallback = handoff.prepare(Post(video_row=1, description="From YouTube"), "tiktok", video)
    assert not fallback.blocked and fallback.warnings[0].label == "Caption"
    too_many = handoff.prepare(Post(video_row=1, instagram_text=" ".join(f"#t{i}" for i in range(31))),
                               "instagram", video)
    assert too_many.blocked and "hashtags" in too_many.checks[1].detail
    missing = handoff.prepare(Post(video_row=1, tiktok_text="x"), "tiktok", tmp_path / "gone.mp4")
    assert missing.blocked and missing.checks[0].status == handoff.BLOCK and missing.video is None


def test_post_links_must_be_the_right_platform():
    assert handoff.check_post_url("tiktok", "") is None
    assert handoff.check_post_url("tiktok", "https://www.tiktok.com/@livingood.lifestyle/video/123") is None
    assert handoff.check_post_url("tiktok", "https://vm.tiktok.com/ZMabc/") is None
    assert "TikTok link" in handoff.check_post_url("tiktok", "https://www.instagram.com/reel/abc/")
    assert "https://" in handoff.check_post_url("instagram", "instagram.com/reel/abc")
    assert "Instagram link" in handoff.check_post_url("instagram", "https://notinstagram.com/reel/abc")


def test_reveal_selects_the_file(tmp_path, monkeypatch):
    calls = []
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr(handoff.sys, "platform", "darwin")
    assert handoff.reveal_in_file_manager(video, runner=calls.append)
    assert calls == [["open", "-R", str(video)]]
    assert not handoff.reveal_in_file_manager(tmp_path / "nope.mp4", runner=calls.append)


# ------------------------------------------------------------------ confirm
def test_confirm_records_link_time_and_moves_the_card(world):
    row = world.rows[0]
    world.pipeline.update_metadata(row, {"target_platforms": ["tiktok"], "tiktok_text": "hi"})
    earlier = datetime.now(timezone.utc) - timedelta(hours=2)
    with pytest.raises(PipelineError, match="TikTok link"):
        world.pipeline.confirm_handoff(row, "tiktok", "https://youtube.com/shorts/x")
    world.pipeline.confirm_handoff(row, "tiktok", "https://www.tiktok.com/@x/video/1", when=earlier)
    [mp] = world.store.manual_posts(row)
    assert (mp.kind, mp.url, mp.note) == ("posted", "https://www.tiktok.com/@x/video/1", "Prepare to Publish")
    assert abs(datetime.fromisoformat(mp.posted_at.replace("Z", "+00:00")) - earlier) < timedelta(seconds=1)
    assert world.store.get_video(row).stage == Stage.PUBLISHED
    later = datetime.now(timezone.utc) + timedelta(days=1)
    world.pipeline.confirm_handoff(row, "tiktok", "", when=later, scheduled=True)
    [mp] = world.store.manual_posts(row)                  # replaces, never duplicates
    assert mp.kind == "scheduled" and world.store.get_video(row).stage == Stage.SCHEDULED


# ------------------------------------------------------------------ coverage
def test_coverage_states(world):
    row = world.rows[0]
    world.pipeline.update_metadata(row, {"target_platforms": ["youtube", "tiktok", "instagram", "bluesky"],
                                         "tiktok_text": "ready to go", "instagram_text": ""})
    world.pipeline.set_platform_state(row, "youtube", "posted")
    cov = world.pipeline.card(row).coverage
    assert list(cov) == list(coverage.ORDER)
    assert cov["youtube"].state == coverage.PUBLISHED
    assert cov["tiktok"].state == coverage.READY
    assert cov["instagram"].state == coverage.READY               # falls back to the description
    assert cov["bluesky"].state == coverage.READY                 # bluesky_text written by make_world
    assert cov["threads"].state == cov["facebook"].state == coverage.NOT_APPLICABLE
    post = world.store.get_post(row)                  # e.g. edited elsewhere: over Instagram's hashtag limit
    post.instagram_text = " ".join(f"#t{i}" for i in range(31))
    world.store.save_post(post)
    cov = world.pipeline.card(row).coverage
    assert cov["instagram"].state == coverage.NEEDS_WORK and "hashtags" in cov["instagram"].reason
    future = datetime.now(timezone.utc) + timedelta(days=1)
    world.pipeline.set_platform_state(row, "tiktok", "scheduled", future)
    assert world.pipeline.card(row).coverage["tiktok"].state == coverage.SCHEDULED
    assert coverage.summary(world.pipeline.card(row).coverage) == \
        "1 published · 1 scheduled · 1 ready to publish · 1 needs work"


def test_missing_file_needs_work(world):
    row = world.rows[0]
    world.pipeline.update_metadata(row, {"target_platforms": ["tiktok"], "tiktok_text": "hi"})
    world.pipeline.video_path(world.store.get_video(row)).unlink()
    cov = world.pipeline.card(row).coverage["tiktok"]
    assert cov.state == coverage.NEEDS_WORK and "missing" in cov.reason


# ------------------------------------------------------------------ in the ticket
@pytest.fixture
def ticket(qtbot, tmp_path):
    from PySide6.QtWidgets import QApplication

    from reelay.gui.app import create_window
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    from tests.test_gui import seed_board
    store = Store(db_path())
    profile = store.create_profile("Lydia", tmp_path / "lib")
    store.set_active_profile(profile.id)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    items, ids = seed_board(store, window.profile, None)
    row = ids[items[0].key]
    store.save_post(Post(video_row=row, title="Sunnies", description="d", target_platforms=["tiktok"],
                         tiktok_text="Sunnies for every adventure"))
    window.go("board")
    b = window.board
    col = b.columns[Stage.BACKLOG]
    b._on_select(col, next(col.model.index(r) for r, c in enumerate(col.model.cards) if c.id == row))
    yield window, store, b, row
    window.close()
    store.close()


def test_publish_button_runs_the_handoff(ticket, monkeypatch):
    from reelay.gui import handoff_dialog
    _window, store, board, row = ticket
    tiktok = board.panel.sections["tiktok"]                                # Publish… is in its ⋯ menu
    assert tiktok.act_publish.isVisible()
    assert not board.panel.sections["youtube"].act_publish.isVisible()     # Reelay posts YouTube itself
    seen = {}

    def run(self):
        opened, revealed, copied = [], [], []
        self._open_url, self._reveal = opened.append, lambda p: revealed.append(p) or True
        self._clipboard = type("C", (), {"setText": lambda _s, t: copied.append(t)})()
        self.start()
        seen.update(opened=opened, revealed=revealed, copied=copied, progress=self.progress.text())
        self.url.setText("https://www.instagram.com/reel/x")
        seen["blocked"] = not self.ok.isEnabled()
        self.url.setText("https://www.tiktok.com/@livingood.lifestyle/video/42")
        return handoff_dialog.HandoffDialog.Accepted
    monkeypatch.setattr(handoff_dialog.HandoffDialog, "exec", run)
    board.panel.blocks["tiktok_text"].set_text("Sunnies for every adventure ☀️")   # unsaved edit is used
    tiktok.act_publish.trigger()
    assert seen["copied"] == ["Sunnies for every adventure ☀️"]
    assert seen["opened"] == [handoff.UPLOAD_URLS["tiktok"]] and seen["revealed"][0].name.endswith(".mp4")
    assert "✓ Caption copied" in seen["progress"] and seen["blocked"]
    [mp] = store.manual_posts(row)
    assert mp.url.endswith("/video/42") and store.get_post(row).tiktok_text == "Sunnies for every adventure ☀️"
    assert store.get_video(row).stage == Stage.PUBLISHED
    assert not board.panel.sections["tiktok"].act_publish.isVisible()     # live now: nothing left to hand off


def test_card_tooltip_lists_coverage(ticket):
    from PySide6.QtCore import Qt
    _window, _store, board, row = ticket
    col = board.columns[Stage.BACKLOG]
    idx = next(col.model.index(r) for r, c in enumerate(col.model.cards) if c.id == row)
    tip = idx.data(Qt.ToolTipRole)
    assert "●  TikTok: Ready to publish" in tip and "YouTube" not in tip
    assert Platform.LABELS["tiktok"] in tip
