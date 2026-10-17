"""A different sound for one platform: a copy made only for that platform, never touching the master."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from reelay import platform_media as PM
from reelay.models import Platform, Post, VideoItem
from reelay.store import Store
from reelay.video_tools import VideoToolError, ffmpeg_for

FF = ffmpeg_for()
pytestmark = pytest.mark.skipif(not FF, reason="needs ffmpeg")


def _clip(path: Path) -> Path:
    subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=180x320:rate=15:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def _track(path: Path) -> Path:
    subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=800:duration=1",
                    str(path)], check=True)
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def world(tmp_path):
    store = Store(tmp_path / "r.db")
    profile = store.create_profile("Be Unconventional", tmp_path / "lib")
    folder = Path(profile.output_folder)
    folder.mkdir(parents=True)
    master = _clip(folder / "1.mp4")
    item = VideoItem(platform="TikTok", video_id="1", url="https://t/1", upload_date="20261001",
                     filename="1.mp4", folder=str(folder), width=180, height=320, duration=2)
    row_id = store.save_videos(profile.id, [item])[item.key]
    yield store, row_id, master, _track(tmp_path / "music.m4a")
    store.close()


def test_the_copy_is_made_once_and_the_master_never_changes(world):
    store, row, master, music = world
    before = _digest(master)
    store.save_platform_sound(PM.PlatformSound(row, Platform.YOUTUBE, str(music)))
    copy = PM.path_for(store, row, Platform.YOUTUBE, master)
    assert copy.name == "1.edited.youtube.mp4" and copy.is_file() and _digest(master) == before
    made = copy.stat().st_mtime
    assert PM.path_for(store, row, Platform.YOUTUBE, master) == copy and copy.stat().st_mtime == made   # reused
    assert PM.path_for(store, row, Platform.TIKTOK, master) == master          # no sound of its own: the master
    sound = store.get_platform_sound(row, Platform.YOUTUBE)
    store.save_platform_sound(replace(sound, mode=PM.MIX, volume_master=0.4))   # a different setting → remade
    assert PM.fresh_copy(store.get_platform_sound(row, Platform.YOUTUBE), master) is None
    PM.path_for(store, row, Platform.YOUTUBE, master)
    assert PM.fresh_copy(store.get_platform_sound(row, Platform.YOUTUBE), master) == copy


def test_a_missing_sound_file_says_so(world):
    store, row, master, music = world
    store.save_platform_sound(PM.PlatformSound(row, Platform.BLUESKY, str(music)))
    music.unlink()
    with pytest.raises(VideoToolError, match="isn't there any more"):
        PM.path_for(store, row, Platform.BLUESKY, master)


def test_readiness_waits_for_the_copy_then_checks_it(world):
    from reelay.pipeline import Pipeline
    from reelay.probe import wait_for_probes
    from tests.fakes import FakeUploads
    store, row, master, music = world

    class Accounts:
        def account_for(self, *a, **k):
            return None
    pipeline = Pipeline(store, Accounts(), FakeUploads())
    pipeline.enabled_platforms = lambda: frozenset({Platform.BLUESKY})
    post = Post(video_row=row, target_platforms=[Platform.BLUESKY], bluesky_text="Hi")
    store.save_platform_sound(PM.PlatformSound(row, Platform.BLUESKY, str(music)))
    first = pipeline.readiness(row, post).platforms[Platform.BLUESKY]
    assert not first.ready and any("Making the copy" in c.message for c in first.checks)
    PM.wait_for_builds()
    wait_for_probes()
    ready = pipeline.readiness(row, post).platforms[Platform.BLUESKY]
    assert ready.ready and any("own sound (music.m4a)" in c.message for c in ready.checks)
    assert pipeline.video_path_for(store.get_video(row), Platform.BLUESKY).name == "1.edited.bluesky.mp4"
    music.unlink()
    gone = pipeline.readiness(row, post).platforms[Platform.BLUESKY]
    assert not gone.ready and any("isn't there any more" in c.message for c in gone.checks)


def test_clean_up_removes_a_posted_tickets_platform_copy_but_keeps_its_sound(world):
    from reelay.models import Stage
    from reelay.video_tools import clean_up_edited, edited_copies
    store, row, master, music = world
    store.save_platform_sound(PM.PlatformSound(row, Platform.YOUTUBE, str(music)))
    copy = PM.path_for(store, row, Platform.YOUTUBE, master)
    assert [c.reason for c in edited_copies(store)] == ["in use"]
    store.set_stage([row], Stage.PUBLISHED)
    assert clean_up_edited(store)[0] == 1 and not copy.exists()
    assert store.get_platform_sound(row, Platform.YOUTUBE) is not None          # still chosen; remade if needed


def test_the_ticket_menu_sets_and_removes_a_platforms_sound(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QDialog

    from reelay.gui import video_widgets
    from reelay.gui.app import create_window
    from reelay.models import Stage
    from reelay.paths import db_path
    from reelay.probe import wait_for_probes
    from reelay.tokens import MemoryTokenStore
    store = Store(db_path())
    profile = store.create_profile("Be Unconventional", tmp_path / "lib")
    store.set_active_profile(profile.id)
    folder = Path(profile.output_folder)
    folder.mkdir(parents=True, exist_ok=True)
    master = _clip(folder / "1.mp4")
    item = VideoItem(platform="TikTok", video_id="1", url="https://t/1", upload_date="20261001",
                     filename="1.mp4", folder=str(folder), width=180, height=320, duration=2)
    row = store.save_videos(profile.id, [item])[item.key]
    store.save_post(Post(video_row=row, title="T", description="D", target_platforms=["youtube", "tiktok"]))
    music = _track(tmp_path / "music.m4a")
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    window.show()
    window.go("board")
    board = window.board
    col = board.columns[Stage.BACKLOG]
    board._on_select(col, next(col.model.index(r) for r, c in enumerate(col.model.cards) if c.id == row))
    section = board.panel.sections[Platform.YOUTUBE]
    assert section.act_sound.text() == "Use Different Sound…" and not section.act_no_sound.isVisible()
    assert board.panel.sections[Platform.TIKTOK].note.toolTip()            # the in-app sounds note
    assert not hasattr(section, "note")                                     # only TikTok and Instagram

    def pick(dialog):
        dialog.set_track(music)
        return QDialog.Accepted
    monkeypatch.setattr(video_widgets.SoundDialog, "exec", pick)
    section.act_sound.trigger()
    PM.wait_for_builds()
    wait_for_probes()
    qtbot.waitUntil(lambda: "♪" in board.panel.sections[Platform.YOUTUBE].toggle.text(), timeout=5000)
    copy = folder / "1.edited.youtube.mp4"
    assert copy.is_file() and master.is_file()
    assert board.panel.sections[Platform.YOUTUBE].act_no_sound.isVisible()
    board.panel.sections[Platform.YOUTUBE].act_no_sound.trigger()
    assert not copy.exists() and master.is_file() and store.get_platform_sound(row, Platform.YOUTUBE) is None
    qtbot.waitUntil(lambda: "♪" not in board.panel.sections[Platform.YOUTUBE].toggle.text(), timeout=5000)
    window.close()
    store.close()
