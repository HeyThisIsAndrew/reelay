"""Bringing in a finished video from this computer (reelay/local_import.py)."""

from __future__ import annotations

from pathlib import Path

from reelay.local_import import (LOCAL_PLATFORM, NO_TRANSCRIPT_NOTE, import_files, sidecar_transcript,
                                 title_from_name)
from reelay.store import Store
from tests.fakes import FAKE_MP4

SRT = """1
00:00:00,000 --> 00:00:02,500
The 16-35 GM is my favourite lens.

2
00:00:02,500 --> 00:00:05,000
So much so that I bought it twice.
"""


def _setup(tmp_path):
    store = Store(tmp_path / "t.db")
    profile = store.create_profile("Hey This Is Andrew", tmp_path / "library")
    exports = tmp_path / "exports"
    exports.mkdir()
    return store, profile, exports


def test_a_finished_video_becomes_a_ticket_with_its_transcript(tmp_path):
    store, profile, exports = _setup(tmp_path)
    video = exports / "why_i_bought_the_16-35_twice FINAL v2.mp4"
    video.write_bytes(FAKE_MP4)
    (exports / "why_i_bought_the_16-35_twice FINAL v2.srt").write_text(SRT, encoding="utf-8")

    [done], problems = import_files(store, profile, [video])

    assert problems == []
    row = store.get_video(done.row_id)
    assert row.item.platform == LOCAL_PLATFORM
    assert row.item.title == "Why i bought the 16-35 twice"
    assert row.ticket.authorization == "Own content"
    assert row.ticket.transcript == ("The 16-35 GM is my favourite lens. So much so that I bought it twice.")
    assert done.transcript_source == "caption file"
    # copied into the library folder; the export itself is untouched
    assert Path(row.item.folder) == Path(profile.output_folder)
    assert (Path(profile.output_folder) / row.item.filename).read_bytes() == FAKE_MP4
    assert video.exists()


def test_the_same_file_twice_is_one_ticket(tmp_path):
    store, profile, exports = _setup(tmp_path)
    video = exports / "clip.mp4"
    video.write_bytes(FAKE_MP4)
    [first], _ = import_files(store, profile, [video])
    renamed = exports / "clip copy.mp4"
    renamed.write_bytes(FAKE_MP4)
    [second], _ = import_files(store, profile, [renamed])
    assert second.row_id == first.row_id and second.already_there
    assert len(store.load_videos(profile.id)) == 1


def test_a_different_file_with_the_same_name_is_not_overwritten(tmp_path):
    store, profile, exports = _setup(tmp_path)
    a = exports / "a" / "clip.mp4"
    b = exports / "b" / "clip.mp4"
    a.parent.mkdir()
    b.parent.mkdir()
    a.write_bytes(FAKE_MP4)
    b.write_bytes(FAKE_MP4 + b"different")
    [one], _ = import_files(store, profile, [a])
    [two], _ = import_files(store, profile, [b])
    assert one.row_id != two.row_id
    assert one.path.read_bytes() == FAKE_MP4
    assert two.path.read_bytes() == FAKE_MP4 + b"different"


def test_files_that_cannot_be_posted_are_reported_plainly(tmp_path):
    store, profile, exports = _setup(tmp_path)
    notes = exports / "notes.pdf"
    notes.write_bytes(b"%PDF")
    imported, problems = import_files(store, profile, [notes, exports / "missing.mp4"])
    assert imported == []
    assert problems[0].startswith("notes.pdf: not a video file")
    assert problems[1] == "missing.mp4: not found."


def test_no_caption_file_means_no_transcript_and_a_plain_note(tmp_path):
    store, profile, exports = _setup(tmp_path)
    video = exports / "silent.mov"
    video.write_bytes(FAKE_MP4)
    [done], _ = import_files(store, profile, [video])
    assert done.transcript_source == ""
    assert not store.get_video(done.row_id).ticket.transcript
    assert "caption file with the same name" in NO_TRANSCRIPT_NOTE and "—" not in NO_TRANSCRIPT_NOTE


def test_sidecar_formats(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(FAKE_MP4)
    assert sidecar_transcript(video) == ""
    (tmp_path / "v.txt").write_text("Plain words.\n", encoding="utf-8")
    assert sidecar_transcript(video) == "Plain words."
    (tmp_path / "v.vtt").write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nHello there\n", encoding="utf-8")
    assert sidecar_transcript(video) == "Hello there"    # .srt, then .vtt, then .txt: .vtt wins over .txt


def test_title_from_file_name():
    assert title_from_name(Path("my_new_video FINAL v2.mp4")) == "My new video"
    assert title_from_name(Path("16-35 GM twice FINAL.mp4")) == "16-35 GM twice"
    assert title_from_name(Path("Vlog - day 3_export.mov")) == "Vlog day 3"
    assert title_from_name(Path("Morning Routine 4K export.mov")) == "Morning Routine"
    assert title_from_name(Path("v3.mp4")) == "v3"


# ------------------------------------------------------------------ the window: drop a file anywhere
def test_dropping_a_video_on_the_window_opens_its_ticket_on_the_board(qtbot, tmp_path):
    from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
    from PySide6.QtGui import QDragEnterEvent, QDropEvent
    from PySide6.QtWidgets import QApplication

    from reelay.gui.app import create_window
    from reelay.gui.local_drop import summary, video_paths
    from reelay.paths import db_path
    from reelay.tokens import MemoryTokenStore

    store = Store(db_path())
    profile = store.create_profile("Hey This Is Andrew", tmp_path / "lib")
    store.set_active_profile(profile.id)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    exports = tmp_path / "exports"
    exports.mkdir()
    video = exports / "Morning edit.mp4"
    video.write_bytes(FAKE_MP4)
    (exports / "Morning edit.srt").write_text(SRT, encoding="utf-8")
    other = exports / "notes.txt"
    other.write_text("x")

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(video)), QUrl.fromLocalFile(str(other)), QUrl("https://example.com/v.mp4")])
    assert video_paths(mime) == [video]            # only local video files count

    target = window.download
    enter = QDragEnterEvent(QPoint(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(target, enter)          # a drop is delivered where the drag entered
    assert enter.isAccepted()
    drop = QDropEvent(QPoint(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    with qtbot.waitSignal(window.local_import.imported, timeout=5000) as blocker:
        QApplication.sendEvent(target, drop)
    row_ids, message = blocker.args
    assert message == "Added “Morning edit” to the Board's first column."
    assert window.stack.currentWidget() is window.board
    assert window.board.selected_id == row_ids[0]
    assert store.get_video(row_ids[0]).ticket.transcript.startswith("The 16-35 GM")
    assert "—" not in summary([], ["x.mp4: not found."])
    window.close()
    store.close()


def test_files_without_captions_are_transcribed_on_this_computer_when_whisper_is_installed(tmp_path):
    from reelay.local_import import transcribe_missing
    from reelay.video_tools import Segment
    store, profile, exports = _setup(tmp_path)
    a = exports / "a.mp4"
    a.write_bytes(FAKE_MP4)
    b = exports / "b.mp4"
    b.write_bytes(FAKE_MP4 + b"b")
    (exports / "b.srt").write_text(SRT, encoding="utf-8")
    imported, _ = import_files(store, profile, [a, b])
    heard = []

    def fake(path):
        heard.append(path.name)
        return [Segment(0.0, 1.0, "First line."), Segment(1.0, 2.0, "Second line.")]
    assert transcribe_missing(store, imported, transcribe=fake) == 1
    assert heard == ["a.mp4"]                                  # b already had its caption file
    assert store.get_video(imported[0].row_id).ticket.transcript == "First line. Second line."
    assert imported[0].transcript_source == "speech on this computer"
