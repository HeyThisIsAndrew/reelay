"""Reelay Studio: the media grid, Board marks, the player and its preview copies, hover-scrub frames, and the
long-form rules. Real ffmpeg on tiny generated clips where it's available; nothing leaves this machine."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel

from reelay import previews, video_tools as vt
from reelay.models import Post, VideoItem
from reelay.readiness import evaluate, is_short_form
from tests.test_gui import app_window, fetch_fake, make_items, open_ticket, seed_board  # noqa: F401 (fixtures)

FF = vt._bundled_ffmpeg() or vt.find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(not FF, reason="no ffmpeg on this machine")


def make_clip(path: Path, seconds: int = 3, size: str = "180x320") -> Path:
    subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=15:duration={seconds}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


# ------------------------------------------------------------------ long-form rules
def test_short_form_and_the_shorts_default(tmp_path, monkeypatch):
    assert is_short_form(1080, 1920, 45) and is_short_form(None, None, 120)
    assert not is_short_form(1920, 1080, 45)              # landscape isn't a Short
    assert not is_short_form(1080, 1920, 600)             # over 3 minutes
    from tests.fakes import make_world
    w = make_world(tmp_path, monkeypatch)
    vertical = w.store.get_video(w.rows[0])
    long = w.store.get_video(w.rows[1])
    long.item.duration, long.item.width, long.item.height = 1800, 1920, 1080
    assert w.pipeline.add_shorts_default_for(vertical) and not w.pipeline.add_shorts_default_for(long)
    w.store.set_setting("add_shorts", "0")
    assert not w.pipeline.add_shorts_default_for(vertical)


def test_readiness_knows_platform_length_limits(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v")
    post = Post(video_row=1, title="Desert road trip", description="The full cut.", bluesky_text="New video",
                target_platforms=["youtube", "bluesky"])
    long = evaluate(post, {"youtube", "bluesky"}, video, duration=3600)
    assert long.platforms["youtube"].ready
    assert any(c.id == "format" and "regular YouTube video" in c.message for c in long.platforms["youtube"].checks)
    assert not long.platforms["bluesky"].ready
    assert "at most 3 minutes" in long.platforms["bluesky"].problems[0]
    short = evaluate(post, {"youtube", "bluesky"}, video, duration=40)
    assert short.platforms["bluesky"].ready and not any(c.id == "format" for c in short.platforms["youtube"].checks)


# ------------------------------------------------------------------ preview copies and scrub frames
@needs_ffmpeg
def test_preview_copy_plays_h264_and_is_reused(tmp_path):
    src = make_clip(tmp_path / "4k.mp4")
    assert previews.existing_preview(src) is None
    out = previews.make_preview(src, ffmpeg=FF)
    assert out.is_file() and out.parent == previews.previews_dir() and out != src
    info = subprocess.run([FF, "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
    assert "h264" in info
    assert previews.make_preview(src, ffmpeg=FF) == out == previews.existing_preview(src)
    src.write_bytes(src.read_bytes() + b"\0")           # the file changed: the old copy no longer counts
    assert previews.existing_preview(src) is None


def test_preview_cache_is_pruned_oldest_first(tmp_path):
    d = previews.previews_dir()
    for n in range(4):
        (d / f"p{n}.mp4").write_bytes(b"x" * 100)
    assert previews.prune_previews(keep=d / "p0.mp4", limit=250) == 2
    assert sorted(p.name for p in d.glob("*.mp4")) == ["p0.mp4", "p3.mp4"]


@needs_ffmpeg
def test_scrub_frames_spread_across_the_video(tmp_path):
    src = make_clip(tmp_path / "clip.mp4", seconds=4)
    frames = previews.make_scrub_frames(src, 4.0, count=4, ffmpeg=FF)
    assert len(frames) == 4 and all(f.stat().st_size > 0 for f in frames)
    assert previews.existing_scrub_frames(src) == frames
    with pytest.raises(vt.VideoToolError):
        previews.make_scrub_frames(tmp_path / "missing.mp4", 4.0, ffmpeg=FF)


def test_scrub_frames_need_a_length(tmp_path):
    src = tmp_path / "v.mp4"
    src.write_bytes(b"v")
    with pytest.raises(vt.VideoToolError, match="length"):
        previews.make_scrub_frames(src, None, ffmpeg="ffmpeg")


# ------------------------------------------------------------------ chrome
def test_up_next_countdown():
    from reelay.gui.chrome import countdown
    now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    assert countdown(now + timedelta(seconds=30), now) == "any moment"
    assert countdown(now + timedelta(minutes=42), now) == "in 42m"
    assert countdown(now + timedelta(hours=2, minutes=14), now) == "in 2h 14m"
    far = countdown(now + timedelta(days=3), now)
    assert far[-2:] in ("am", "pm") and "%" not in far


def test_media_labels():
    from reelay.gui.cards import media_label
    short = VideoItem(platform="TikTok", video_id="1", url="u", duration=20, width=1080, height=1920)
    long = VideoItem(platform="YouTube", video_id="2", url="u", duration=3700, width=3840, height=2160)
    assert media_label(short) == "Short · 1080p" and media_label(long) == "Long-form · 4K"


# ------------------------------------------------------------------ the Library grid
def test_library_grid_is_the_default_and_shares_the_selection(app_window, qtbot, monkeypatch):
    window, store, _ = app_window
    page = fetch_fake(window, qtbot, monkeypatch, make_items(4))
    assert page.view_toggle.mode == "grid" and page.list_stack.currentWidget() is page.grid
    first = page.grid.model().index(0, 0)
    page.grid.checkToggled.emit(first)                           # the tick on a card
    assert len(page.model.checked_items()) == 1 and not page.split.is_open   # ticking never opens details
    page.grid.selectionModel().select(first, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
    assert page.split.is_open and page.inspector.title.text() == "VIDEO"
    page.view_toggle.set_mode("list", emit=True)                 # same selection in the list
    assert page.list_stack.currentWidget() is page.table and page.table.selectionModel().hasSelection()
    assert store.get_setting("ui.view.library") == "list"
    page.close_inspector()
    assert not page.split.is_open


def test_library_filter_and_sort(app_window, qtbot, monkeypatch):
    window, store, _ = app_window
    page = fetch_fake(window, qtbot, monkeypatch, make_items(4))
    page.sort_box.setCurrentIndex(page.sort_box.findData("oldest"))
    first = page.model.item(page.proxy.mapToSource(page.proxy.index(0, 0)).row())
    assert first.upload_date == min(i.upload_date for i in page.model.items())
    page.show_filter.setCurrentIndex(page.show_filter.findData("downloaded"))
    assert page.proxy.rowCount() == 0 and page.filter_btn.text() == "Filter · 1"
    page.show_filter.setCurrentIndex(0)
    assert page.proxy.rowCount() == 4 and page.filter_btn.text() == "Filter"


def test_library_cards_paint_in_every_theme(app_window, qtbot, monkeypatch):
    window, store, _ = app_window
    page = fetch_fake(window, qtbot, monkeypatch, make_items(3))
    for mode in ("light", "dark", "black", "custom"):
        window.set_theme(mode)
        image = page.grid.viewport().grab()
        assert not image.isNull()


# ------------------------------------------------------------------ the Board
def test_board_cards_show_what_happened_to_them(app_window, monkeypatch):
    window, store, _ = app_window
    items, ids = seed_board(store, window.profile, None)
    a, b, c = (ids[i.key] for i in items)
    store.set_setting(f"rules_log.{window.profile.id}", json.dumps(
        [{"at": "2026-10-10T10:00:00Z", "rule": "Sunglasses template", "video": a, "title": "x",
          "done": ["filled TikTok caption"]},
         {"at": "2026-10-10T10:00:00Z", "rule": "Quiet rule", "video": b, "title": "x", "done": ["nothing to do"]}]))
    from reelay.models import ApprovalRequest
    store.create_approval(ApprovalRequest(
        profile_id=window.profile.id, operation="archive", payload={"video_ids": [b]}, target_ids=[b],
        target_hash="hash", requested_by="assistant",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")))
    with store.origin("api"):
        store.save_post(Post(video_row=c, title="Written by an assistant"))
    window.go("board")
    cards = window.board.cards
    assert cards[a].flags == {"automation": ["Sunglasses template"], "approval": False, "ai": False}
    assert cards[b].flags["approval"] and not cards[b].flags["automation"]
    assert cards[c].flags["ai"]
    from reelay.gui.board_page import card_notes
    assert "waiting for your approval" in " ".join(card_notes(cards[b]))


def test_board_density_and_up_next(app_window, monkeypatch):
    window, store, _ = app_window
    board = window.board
    open_ticket(window, store, monkeypatch)
    from reelay.gui.board_page import CardDelegate
    assert CardDelegate.density == "large"
    board.density_box.setCurrentIndex(board.density_box.findData("compact"))
    assert CardDelegate.density == "compact" and store.get_setting("ui.board_density") == "compact"
    board.density_box.setCurrentIndex(board.density_box.findData("large"))
    assert board.next_up() == ("", None)
    card = next(iter(board.cards.values()))
    later = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    from reelay.models import Dispatch
    card.dispatches.append(Dispatch(video_row=card.id, platform="bluesky", due_at=later, publish_at=later))
    label, when = board.next_up()
    assert label == "Bluesky" and when is not None


# ------------------------------------------------------------------ the player
def test_ticket_opens_with_the_player_and_the_action_dock(app_window, monkeypatch):
    window, store, _ = app_window
    board = window.board
    panel, row = open_ticket(window, store, monkeypatch)
    assert board.player.isVisibleTo(board) and board.player.original is not None   # the downloaded file
    assert not panel.cover.isVisibleTo(panel)                    # the player shows the video instead
    assert panel.dock.isVisibleTo(panel) and "YouTube" in panel.dock_summary.text()
    assert panel.readiness_header.ring.total >= 1
    board.close_inspector()
    assert board.player.path is None                             # the file is let go


@needs_ffmpeg
def test_player_offers_a_preview_copy_when_a_format_cant_play(app_window, qtbot, tmp_path):
    from reelay.gui.player import MULTIMEDIA, PlayerWidget
    player = PlayerWidget()
    qtbot.addWidget(player)
    src = make_clip(tmp_path / "clip.mp4")
    player.set_video(src, None)
    if MULTIMEDIA:
        assert player.path == src and player.play_btn.isEnabled()
    player._cant_play()                                         # what an undecodable file reports
    assert player.path is None and not player.preview_btn.isHidden()
    player.make_preview()
    qtbot.waitUntil(lambda: not player._workers, timeout=30000)
    assert player.path is not None and player.path.parent == previews.previews_dir()
    assert player.original == src                               # QuickTime still opens the original
    player.shutdown()


def test_player_without_a_file_says_so(app_window, qtbot):
    from reelay.gui.player import PlayerWidget
    player = PlayerWidget()
    qtbot.addWidget(player)
    player.set_video(None, None)
    assert "Download this video" in player.notice_text.text() and not player.play_btn.isEnabled()
