"""Local video edits (ffmpeg): remove/replace the sound, burn in captions, and
posting the edited copy instead of the original. Real ffmpeg on tiny generated
clips; Whisper is faked. Nothing leaves this machine."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from reelay import video_tools as vt
from tests.fakes import FAKE_MP4, disable_publishing, make_world

FF = vt._bundled_ffmpeg() or vt.find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(not FF, reason="no ffmpeg on this machine")


def streams(path) -> str:
    return subprocess.run([FF, "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr


@pytest.fixture
def clip(tmp_path):
    src = tmp_path / "7001.mp4"
    subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=180x320:rate=15:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(src)], check=True)
    track = tmp_path / "royalty free.m4a"
    subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=880:duration=0.5",
                    str(track)], check=True)
    return src, track


@needs_ffmpeg
def test_strip_and_replace_never_touch_the_original(clip):
    src, track = clip
    before = src.read_bytes()
    out = vt.strip_audio(src, ffmpeg=FF)
    assert out.name == "7001.edited.mp4" and "Audio" not in streams(out)
    out = vt.replace_audio(src, track, ffmpeg=FF)          # short track loops to the video's length
    info = streams(out)
    assert "Audio: aac" in info and "Duration: 00:00:02" in info
    assert src.read_bytes() == before
    assert vt.output_path(out) == out                       # editing an edit keeps one edited file


@needs_ffmpeg
def test_mixing_into_a_silent_video_explains_itself_and_cleans_up(clip, tmp_path):
    src, track = clip
    silent = vt.strip_audio(src, tmp_path / "silent.mp4", ffmpeg=FF)
    with pytest.raises(vt.VideoToolError, match="set its volume to 0"):
        vt.replace_audio(silent, track, tmp_path / "mixed.mp4", original_volume=0.3, ffmpeg=FF)
    assert not list(tmp_path.glob("*.part.mp4")) and not (tmp_path / "mixed.mp4").exists()


@pytest.mark.skipif(not vt.ffmpeg_for(captions=True), reason="no ffmpeg with libass")
def test_burn_captions_from_a_file_with_an_awkward_name(clip, tmp_path):
    src, _ = clip
    subs = tmp_path / "Lydia's caps: [final], v2.srt"
    subs.write_text(vt.to_srt([vt.Segment(0, 1.5, "These sunnies keep up with you")]), encoding="utf-8")
    out = vt.burn_captions(src, subs)
    assert out.is_file() and "Video: h264" in streams(out)
    with pytest.raises(vt.VideoToolError, match="caption file"):
        vt.burn_captions(src, tmp_path / "notes.txt")


def test_missing_files_and_ffmpeg_errors_are_plain(tmp_path):
    with pytest.raises(vt.VideoToolError, match="Download it first"):
        vt.strip_audio(tmp_path / "nope.mp4", ffmpeg="ffmpeg")
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x")

    def failing(cmd, **kw):
        return SimpleNamespace(returncode=1, stderr="lots of noise\nInvalid data found when processing input\n")
    with pytest.raises(vt.VideoToolError, match="Invalid data found"):
        vt.strip_audio(src, ffmpeg="ffmpeg", runner=failing)


def test_caption_lines_are_short_and_timed():
    segs = vt.chunk([vt.Segment(0.0, 4.0, "these polarized sunglasses can keep up with every single adventure")])
    assert all(len(s.text) <= 32 for s in segs) and segs[0].start == 0 and abs(segs[-1].end - 4.0) < 1e-6
    srt = vt.to_srt(segs)
    assert srt.startswith("1\n00:00:00,000 --> ") and "\n\n2\n" in srt


def test_auto_captions_with_a_fake_whisper(tmp_path):
    src = tmp_path / "v.mp4"
    src.write_bytes(b"x")

    class FakeModel:
        def transcribe(self, path, language=None, vad_filter=True):
            return iter([SimpleNamespace(start=0.0, end=2.0, text=" Grab yours today "),
                         SimpleNamespace(start=2.0, end=2.5, text="  ")]), None
    srt = vt.auto_captions(src, model=FakeModel())
    assert srt.name == "v.captions.srt" and "Grab yours today" in srt.read_text()
    ok, why = vt.whisper_available()
    assert ok or "faster-whisper" in why


def test_pipeline_posts_the_edited_copy(tmp_path, monkeypatch):
    disable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch)
    row = w.store.get_video(w.rows[0])
    original = w.pipeline.video_path(row)
    edited = vt.output_path(original)
    edited.write_bytes(FAKE_MP4)
    vt.set_edit(w.store, row.id, edited, ["strip", "replace"])
    assert w.pipeline.video_path(row) == edited and w.pipeline.original_path(row) == original
    assert vt.describe(vt.get_edit(w.store, row.id)) == "sound removed, new sound"
    edited.unlink()                                         # deleted in Finder: fall back to the original
    assert w.pipeline.video_path(row) == original
    vt.clear_edit(w.store, row.id)
    assert vt.get_edit(w.store, row.id) is None
    w.store.close()


# ------------------------------------------------------------------ in the ticket
@pytest.fixture
def ticket(qtbot, tmp_path):
    from PySide6.QtWidgets import QApplication

    from reelay.gui.app import create_window
    from reelay.models import Stage
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
    window.go("board")
    b = window.board
    col = b.columns[Stage.BACKLOG]
    row = ids[items[0].key]
    b._on_select(col, next(col.model.index(r) for r, c in enumerate(col.model.cards) if c.id == row))
    yield window, store, b.panel, row
    window.close()
    store.close()


def test_video_section_makes_and_forgets_an_edit(ticket, qtbot, monkeypatch):
    _window, store, panel, row = ticket
    tools = panel.video_tools
    assert panel.video_section._summary_text == "Original"
    assert tools.strip_btn.isEnabled() and not tools.original_btn.isVisible()

    def fake_strip(src, dst=None, **kw):
        dst.write_bytes(FAKE_MP4)
        return dst
    monkeypatch.setattr(vt, "strip_audio", fake_strip)
    tools._strip()
    qtbot.waitUntil(lambda: vt.edited_path(store, row) is not None, timeout=5000)
    qtbot.waitUntil(lambda: not tools.worker.isRunning(), timeout=5000)
    assert "sound removed" in tools.status.text() and tools.summary() == "Edited: sound removed"
    assert panel.video_section._summary_text == "Edited: sound removed"
    assert tools.source() == vt.output_path(tools.original)  # the next edit stacks on this one

    tools._use_original()
    assert vt.edited_path(store, row) is None and tools.summary() == "Original"
    assert tools.original.read_bytes() == FAKE_MP4                 # the original is never changed


def test_clean_up_removes_only_copies_nothing_still_needs(tmp_path):
    """Edited copies a ticket will still post are kept; unused ones and those of posted tickets go."""
    from pathlib import Path

    from reelay.models import Stage, VideoItem
    from reelay.store import Store
    from reelay.video_tools import clean_up_edited, edited_copies, edited_path, set_edit
    store = Store(tmp_path / "r.db")
    profile = store.create_profile("Be Unconventional", tmp_path / "lib")
    folder = Path(profile.output_folder)
    folder.mkdir(parents=True)
    items = [VideoItem(platform="TikTok", video_id=str(n), url=f"https://t/{n}", upload_date="20261001",
                       filename=f"{n}.mp4", folder=str(folder)) for n in range(2)]
    ids = store.save_videos(profile.id, items)
    waiting, posted = ids[items[0].key], ids[items[1].key]
    for name, size in (("0.edited.mp4", 3000), ("1.edited.mp4", 2000), ("old.edited.mp4", 1000)):
        (folder / name).write_bytes(b"\0" * size)
    set_edit(store, waiting, folder / "0.edited.mp4", ["strip"])
    set_edit(store, posted, folder / "1.edited.mp4", ["replace"])
    store.set_stage([posted], Stage.PUBLISHED)
    copies = {c.path.name: c for c in edited_copies(store)}
    assert copies["0.edited.mp4"].reason == "in use" and not copies["0.edited.mp4"].removable
    assert copies["1.edited.mp4"].reason == "posted" and copies["old.edited.mp4"].reason == "unused"
    assert sum(c.size for c in copies.values() if c.removable) == 3000
    removed, freed = clean_up_edited(store)
    assert (removed, freed) == (2, 3000)
    assert (folder / "0.edited.mp4").exists() and not (folder / "1.edited.mp4").exists()
    assert edited_path(store, waiting) is not None and edited_path(store, posted) is None
    store.close()


def test_settings_shows_edited_copies_and_cleans_up(qtbot, tmp_path, monkeypatch):
    from pathlib import Path

    from PySide6.QtWidgets import QApplication, QMessageBox

    from reelay.gui.app import create_window
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    store = Store(db_path())
    profile = store.create_profile("Be Unconventional", tmp_path / "lib")
    store.set_active_profile(profile.id)
    folder = Path(profile.output_folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "gone.edited.mp4").write_bytes(b"\0" * 5_000_000)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    window.go("settings")
    page = window.settings_page
    page.refresh()
    assert "5 MB can be cleaned up" in page.storage_label.text() and page.clean_btn.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    page.clean_btn.click()
    assert not (folder / "gone.edited.mp4").exists() and not page.clean_btn.isEnabled()
    assert "Removed 1 edited copy" in page.storage_label.text()
    window.close()
    store.close()
