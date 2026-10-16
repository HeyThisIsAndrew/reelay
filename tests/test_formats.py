"""Short-form vs long-form, the source-platform badge, and the Format filter in the Library and on the Board."""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

from reelay.models import VideoItem
from reelay.readiness import LONG_FORM, SHORT_FORM, video_form


def _item(platform="YouTube", vid="a", url="", w=None, h=None, seconds=None):
    return VideoItem(platform=platform, video_id=vid, url=url or f"https://www.youtube.com/watch?v={vid}",
                     width=w, height=h, duration=seconds, upload_date="20261001")


def test_video_form_follows_youtubes_rule_and_trusts_a_shorts_link():
    assert video_form(_item(w=1080, h=1920, seconds=45)) == SHORT_FORM
    assert video_form(_item(w=1080, h=1080, seconds=170)) == SHORT_FORM            # square counts
    assert video_form(_item(w=1080, h=1920, seconds=181)) == LONG_FORM             # over 3 minutes
    assert video_form(_item(w=1920, h=1080, seconds=40)) == LONG_FORM              # landscape is never a Short
    assert video_form(_item(w=1920, h=1080, seconds=900)) == LONG_FORM
    assert video_form(_item(url="https://www.youtube.com/shorts/abc", w=1920, h=1080, seconds=40)) == SHORT_FORM
    assert video_form(_item(seconds=30)) == SHORT_FORM                             # unknown size: like #Shorts
    assert video_form(_item(seconds=1200)) == LONG_FORM


def test_source_badge_uses_the_shorts_mark_for_youtube_shorts():
    from reelay.gui.cards import source_badge
    key, tip = source_badge(_item(w=1080, h=1920, seconds=45))
    assert key == "youtube_shorts" and tip.startswith("From YouTube") and "Short" in tip and "1080p" in tip
    key, tip = source_badge(_item(w=3840, h=2160, seconds=900))
    assert key == "youtube" and "Long-form" in tip and "4K" in tip
    key, tip = source_badge(_item("TikTok", url="https://www.tiktok.com/@x/video/1", w=1080, h=1920, seconds=30))
    assert key == "tiktok" and tip.startswith("From TikTok")
    for platform in ("Instagram", "Threads", "Facebook", "Bluesky", "Snapchat"):
        assert source_badge(_item(platform, url="https://example.com/v"))[0] == platform.lower()


def test_every_badge_logo_draws(qtbot):
    from reelay.gui.logos import DRAW, platform_pixmap
    assert "youtube_shorts" in DRAW
    for key in DRAW:
        image = platform_pixmap(key, 24).toImage()
        assert any(image.pixelColor(x, y).alpha() > 0 for x in range(0, image.width(), 3)
                   for y in range(0, image.height(), 3)), key


def test_library_format_filter(qtbot, tmp_path, monkeypatch):
    import reelay.gui.workers as workers
    from reelay.gui.app import create_window
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    store = Store(db_path())
    profile = store.create_profile("Be Unconventional", tmp_path / "lib")
    store.set_active_profile(profile.id)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    window.show()
    window.go("download")
    items = [_item(vid="s1", w=1080, h=1920, seconds=40), _item(vid="s2", w=1080, h=1920, seconds=55),
             _item(vid="l1", w=1920, h=1080, seconds=800)]
    monkeypatch.setattr(workers, "list_videos", lambda u, o, limit=None: ("x", [VideoItem(**i.__dict__)
                                                                                for i in items]))
    page = window.download
    page.url.setText("https://www.youtube.com/@x")
    page._fetch()
    qtbot.waitUntil(lambda: page.fetch_btn.isEnabled() and page.model.rowCount() == 3, timeout=5000)
    box = page.form_filter
    assert box.toolTip() and box.itemText(0) == "All formats"
    box.setCurrentIndex(box.findData(SHORT_FORM))
    assert page.pager.total == 2 and page.filter_btn.count == 1
    box.setCurrentIndex(box.findData(LONG_FORM))
    assert page.pager.total == 1
    box.setCurrentIndex(0)
    assert page.pager.total == 3 and page.filter_btn.count == 0
    window.close()
    store.close()


def test_board_format_filter(qtbot, tmp_path):
    from reelay.gui.app import create_window
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    store = Store(db_path())
    profile = store.create_profile("Be Unconventional", tmp_path / "lib")
    store.set_active_profile(profile.id)
    from pathlib import Path

    from tests.test_gui import FAKE_MP4
    folder = Path(profile.output_folder)
    folder.mkdir(parents=True, exist_ok=True)
    items = [_item(vid="s1", w=1080, h=1920, seconds=40), _item(vid="l1", w=1920, h=1080, seconds=800),
             _item(vid="l2", w=1920, h=1080, seconds=600)]
    for i in items:                                         # downloaded, so they're on the Board
        i.filename, i.folder = f"{i.video_id}.mp4", str(folder)
        (folder / i.filename).write_bytes(FAKE_MP4)
    store.save_videos(profile.id, items)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    window.show()
    window.go("board")
    board = window.board

    def visible():
        return sum(c.model.rowCount() for c in board.columns.values())
    total = visible()
    box = board.form_box
    box.setCurrentIndex(box.findData(SHORT_FORM))
    assert visible() == total - 2 and board.filter_btn.count == 1
    box.setCurrentIndex(box.findData(LONG_FORM))
    assert visible() == 2
    board.clear_filters()
    assert visible() == total and box.currentIndex() == 0
    window.close()
    store.close()
