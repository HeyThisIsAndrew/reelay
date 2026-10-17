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
    assert "at most 10 minutes" in long.platforms["bluesky"].problems[0]       # raised from 3 in Aug 2026
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


# ------------------------------------------------------------------ the Library grid
def test_library_grid_is_the_default_and_shares_the_selection(app_window, qtbot, monkeypatch):
    window, store, _ = app_window
    page = fetch_fake(window, qtbot, monkeypatch, make_items(4))
    assert page.view_toggle.mode == "grid" and page.list_stack.currentWidget() is page.grid
    first = page.grid.model().index(0, 0)
    page.grid.checkToggled.emit(first)                           # the tick on a card
    assert len(page.model.checked_items()) == 1 and not page.split.is_open   # ticking never opens details
    page.grid.selectionModel().select(first, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
    assert page.split.is_open and not page.inspector.title.isVisibleTo(page)
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


# ------------------------------------------------------------------ pagination
def _many(n):
    from datetime import date
    return [VideoItem(platform="TikTok", video_id=str(5000 + i), url=f"https://www.tiktok.com/@x/video/{5000 + i}",
                      title=f"clip {i}", description=f"clip {i} #tag", duration=10 + i,
                      upload_date=(date(2026, 9, 30) - timedelta(days=i)).strftime("%Y%m%d"))
            for i in range(n)]


def test_library_pages(app_window, qtbot, monkeypatch):
    window, store, _ = app_window
    page = fetch_fake(window, qtbot, monkeypatch, _many(60))
    page.page_size.setCurrentIndex(page.page_size.findData(24))
    assert store.get_setting("ui.library.page_size") == "24"
    assert page.pager.rowCount() == 24 and page.pager.pages == 3 and page.page_label.text() == "Page 1 of 3"
    assert page.page_count.text().startswith("1–24 of 60")             # which ones this page shows
    assert page.pager_row.indexOf(page.prev_btn) >= 0 and page.pager_row.indexOf(page.next_btn) >= 0   # centred row
    assert not page.prev_btn.isEnabled() and page.next_btn.isEnabled()
    page.next_btn.click()
    assert page.page_count.text().startswith("25–48 of 60")
    page.next_btn.click()
    assert page.pager.page == 2 and page.pager.rowCount() == 12 and not page.next_btn.isEnabled()
    assert page.page_count.text().startswith("49–60 of 60")
    newest_on_last = page.model.item(page.pager.to_model_row(page.pager.index(0, 0)))
    assert newest_on_last.video_id == "5048"                      # newest first, 49th video starts page 3
    page.search.setPlainText("clip 1")                             # a new search starts at page 1
    assert page.pager.page == 0
    page.search.setPlainText("")
    page.select_all_btn.click()                                    # Select All covers every page
    assert len(page.model.checked_items()) == 60
    page.page_size.setCurrentIndex(page.page_size.findData(0))     # All
    assert page.pager.rowCount() == 60 and page.prev_btn.isHidden()


def test_library_page_survives_background_refreshes(app_window, qtbot, monkeypatch):
    # Product and hashtag menus rebuild in the background (600 ms after details or tagged products
    # arrive); that mustn't throw you back to page 1 when the filter didn't actually change.
    window, _, _ = app_window
    page = fetch_fake(window, qtbot, monkeypatch, _many(60))
    page.page_size.setCurrentIndex(page.page_size.findData(24))
    page.next_btn.click()
    assert page.pager.page == 1
    page._refresh_products()
    page._products_timer.timeout.emit()
    page._tags_timer.timeout.emit()
    page.reload()
    assert page.pager.page == 1 and page.page_label.text() == "Page 2 of 3"
    window.go("board")
    window.go("download")
    assert page.pager.page == 1
    page.sort_box.setCurrentIndex(page.sort_box.findData("oldest"))   # a new order does start at page 1
    assert page.pager.page == 0


# ------------------------------------------------------------------ tile size
def test_tile_size_presets_slider_and_narrow_views(app_window, qtbot, monkeypatch):
    window, store, _ = app_window
    page = fetch_fake(window, qtbot, monkeypatch, make_items(4))
    assert page.grid.card_min_width == 240 and page.tile_controls.isVisibleTo(page)
    page.step_tile_size(1)
    assert page.grid.card_min_width == 320 and page.tile_slider.value() == 320
    assert store.get_setting("ui.library.tile_size") == "320"
    page.step_tile_size(1)
    page.step_tile_size(1)                                         # XL is the top preset
    assert page.grid.card_min_width == 400
    page.tile_slider.setValue(190)
    assert page.grid.card_min_width == 190
    page.step_tile_size(-1)
    assert page.grid.card_min_width == 180
    page.grid.set_card_min_width(10_000)                           # clamped to TILE_MAX
    assert page.grid.card_min_width == 400
    page.grid.set_card_min_width(-10_000)                          # clamped to TILE_MIN
    assert page.grid.card_min_width == 180
    assert page.tile_slider.minimum() == 180 and page.tile_slider.maximum() == 400
    # max zoom in windowed and fullscreen does not leave an empty column on the far right
    page.grid.set_card_min_width(400)
    window.resize(1400, 900)
    page.grid._relayout()
    r0_y = page.grid.visualRect(page.pager.index(0, 0)).y()
    first_row_cols = sum(1 for i in range(4) if page.grid.visualRect(page.pager.index(i, 0)).y() == r0_y)
    assert first_row_cols >= 2                                     # fits multiple columns without dropping to 1
    page.grid.resize(260, 400)                                     # narrower than one tile
    page.grid._relayout()
    assert page.grid.card_size.width() <= page.grid.viewport().width()
    page.view_toggle.set_mode("list", emit=True)
    assert not page.tile_controls.isVisibleTo(page)               # tile size only applies to the grid


def test_tile_slider_depletion_and_max_zoom_no_empty_columns(app_window, qtbot, monkeypatch):
    window, store, _ = app_window
    items = make_items(8)
    page = fetch_fake(window, qtbot, monkeypatch, items)

    # 1. Zoom in and out fills / depletes the slider
    for _ in range(5):
        page.step_tile_size(-1)
    assert page.grid.card_min_width == 180
    assert page.tile_slider.value() == page.tile_slider.minimum() == 180

    for _ in range(5):
        page.step_tile_size(1)
    assert page.grid.card_min_width == 400
    assert page.tile_slider.value() == page.tile_slider.maximum() == 400

    # 2. At max zoom in windowed and fullscreen, rows fit expected columns with no empty column
    for win_w in (1200, 1400, 1600, 1920):
        window.resize(win_w, 900)
        page.grid.set_card_min_width(400)
        page.grid._relayout()
        avail = max(1, page.grid.viewport().width() - 8)
        expected_cols = max(1, avail // (400 + 16))
        r0_y = page.grid.visualRect(page.pager.index(0, 0)).y()
        actual_cols = sum(1 for i in range(len(items)) if page.grid.visualRect(page.pager.index(i, 0)).y() == r0_y)
        assert actual_cols == expected_cols, f"Width {win_w}: expected {expected_cols} cols, got {actual_cols}"


# ------------------------------------------------------------------ Downloaded Videos (in-app list)
def _downloaded(tmp_path, n=3):
    items = []
    for i in range(n):
        item = VideoItem(platform="TikTok", video_id=str(800 + i), url=f"https://www.tiktok.com/@x/video/{800 + i}",
                         title=f"sunnies {i}" if i else "desert road trip", duration=30 + i, view_count=100 * (i + 1),
                         upload_date=f"2026090{i + 1}", filename=f"clip{i}.mp4", folder=str(tmp_path))
        (tmp_path / item.filename).write_bytes(b"v" * (1000 * (i + 1)))
        item.status = "downloaded"
        items.append(item)
    return items


def test_video_list_dialog_search_copy_and_export(app_window, qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from reelay.gui import video_list_dialog as vld
    dialog = vld.VideoListDialog(_downloaded(tmp_path), tmp_path)
    qtbot.addWidget(dialog)
    assert dialog.table.rowCount() == 3 and dialog.item_at(0).upload_date == "20260903"   # newest first
    dialog.search.setText("sunnies")
    assert len(dialog.visible_items()) == 2 and "2 shown" in dialog.count.text()
    dialog.search.setText("")
    dialog.table.selectRow(0)
    dialog.copy_links()
    assert QApplication.clipboard().text() == dialog.item_at(0).url
    revealed = []
    monkeypatch.setattr(vld, "reveal_in_file_manager", lambda path: revealed.append(Path(path)) or True)
    dialog.reveal()
    assert revealed == [tmp_path / dialog.item_at(0).filename]
    out = dialog.export_csv(str(tmp_path / "list.csv"))
    rows = out.read_text().splitlines()
    assert rows[0].startswith("Date,Title") and len(rows) == 4 and "https://www.tiktok.com/@x/video/802" in out.read_text()
    md = dialog.export_markdown(str(tmp_path / "manifest.md"))
    assert "clip0.mp4" in md.read_text()


def test_open_video_list_stays_in_the_app(app_window, qtbot, monkeypatch):
    window, store, _ = app_window
    page = fetch_fake(window, qtbot, monkeypatch, make_items(2))
    from PySide6.QtGui import QDesktopServices
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url))
    page.manifest_btn.click()
    assert page.video_list.isVisible() and opened == []
    page.video_list.close()


# ------------------------------------------------------------------ splitter bounds
def test_inspector_can_neither_crush_the_library_nor_cover_it(qtbot):
    from PySide6.QtWidgets import QLabel, QWidget
    from reelay.gui.inspector import ContextualSplitter, Inspector
    main = QWidget()
    split = ContextualSplitter(main, Inspector(QLabel("details")), default_width=900)
    qtbot.addWidget(split)
    split.resize(1300, 700)
    split.show()
    split.reveal()
    assert split.sizes()[1] == 600                                  # wanted 900: capped at 600
    split.moveSplitter(50, 1)                                        # drag the edge all the way left
    assert split.sizes()[0] >= 320 and split.sizes()[1] <= 600
    split.resize(900, 700)                                           # a narrower window: 55% at most
    split.moveSplitter(100, 1)
    assert split.sizes()[1] <= int(900 * 0.55) and split.sizes()[0] >= 320
    split.dismiss()
    assert split.slide.isHidden()


# ------------------------------------------------------------------ Automations page
def test_automations_empty_state_recipes_and_rule_preview(app_window, monkeypatch):
    window, store, _ = app_window
    seed_board(store, window.profile, None)
    page = window.automations_page
    window.go("automations")
    assert page.empty.isVisibleTo(page) and not page.log.isVisibleTo(page)
    page.recipe_buttons["cross_post"].click()
    assert not page.empty.isVisibleTo(page) and page.log.isVisibleTo(page)
    from reelay import rules as R
    rule = R.load(store, window.profile.id)[0]
    assert rule.name == "Cross-post short-form"
    from reelay.columns import load as load_columns
    from reelay.gui.automations_page import RuleDialog
    dialog = RuleDialog(rule, load_columns(store, window.profile.id), page, window.pipeline, window.profile.id)
    assert dialog.test_video.count() == 3
    text = dialog.preview_text.text()
    assert "Trigger matches" in text and "Conditions met" in text and "Platforms" in text
    dialog.close()


# ------------------------------------------------------------------ Snapchat
def test_snapchat_is_a_hand_off_platform_and_a_download_source(app_window):
    from reelay.models import Platform
    from reelay.platforms import detect_platform, is_supported_url
    from reelay.rules import parse_platforms
    assert Platform.SNAPCHAT in Platform.HAND_ONLY and Platform.LABELS[Platform.SNAPCHAT] == "Snapchat"
    assert Platform.PINTEREST in Platform.PLANNED and Platform.LINKEDIN in Platform.PLANNED
    assert detect_platform("https://www.snapchat.com/spotlight/W7_EDlXWTBiXAEEniNoMPwAAYYmtnbm9xbXZ3AYyH") == "Snapchat"
    assert detect_platform("https://example.com/x", "SnapchatSpotlight") == "Snapchat"
    assert is_supported_url("https://www.snapchat.com/spotlight/abc")
    assert parse_platforms("TikTok, Snapchat") == ["tiktok"]          # not a ticket platform yet
    window, *_ = app_window
    window.go("accounts")
    card = window.accounts_page.platform_cards["snapchat"]
    assert card.isVisibleTo(window.accounts_page)
