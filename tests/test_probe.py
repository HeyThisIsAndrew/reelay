"""Ground truth from the file itself: size, duration, displayed size, codecs. With ffprobe when it's installed,
otherwise from the ffmpeg every copy of Reelay has."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reelay import probe as P
from reelay.ytdl import find_ffmpeg

FF = find_ffmpeg()
pytestmark = pytest.mark.skipif(not FF, reason="needs ffmpeg")


def clip(path: Path, size: str = "180x320", seconds: float = 2, audio: bool = True):
    cmd = [FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=15:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    subprocess.run(cmd + [str(path)], check=True)
    return path


@pytest.mark.parametrize("use_ffprobe", [True, False])
def test_probe_reads_the_real_file(tmp_path, monkeypatch, use_ffprobe):
    if not use_ffprobe:
        monkeypatch.setattr(P, "find_ffprobe", lambda: None)            # the bundled ffmpeg has no ffprobe
    elif not P.find_ffprobe():
        pytest.skip("no ffprobe on this computer")
    video = clip(tmp_path / "v.mp4", "180x320", 2)
    info = P.probe(video)
    assert info.size == video.stat().st_size
    assert info.width == 180 and info.height == 320 and info.vertical and not info.landscape
    assert abs(info.duration - 2.0) < 0.2
    assert info.video_codec == "h264" and info.audio_codec == "aac"
    assert info.fps and abs(info.fps - 15) < 0.5


def test_a_landscape_silent_file_and_a_non_video(tmp_path):
    info = P.probe(clip(tmp_path / "wide.mp4", "320x180", 1, audio=False))
    assert info.landscape and info.audio_codec == ""
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video at all")
    assert P.probe(junk) is None
    assert P.probe(tmp_path / "missing.mp4") is None


def test_the_store_remembers_a_probe_until_the_file_changes(tmp_path):
    from reelay.store import Store
    store = Store(tmp_path / "r.db")
    video = clip(tmp_path / "v.mp4", "180x320", 1)
    calls = []
    real = P.probe
    first = P.cached_probe(store, video, probe=lambda p: calls.append(p) or real(p))
    again = P.cached_probe(store, video, probe=lambda p: calls.append(p) or real(p))
    assert first == again and len(calls) == 1                           # the second came from the database
    clip(video, "320x180", 1)                                           # a new file at the same path
    changed = P.cached_probe(store, video, probe=lambda p: calls.append(p) or real(p))
    assert changed.landscape and len(calls) == 2
    assert P.cached_probe(store, video, probe=None) == changed           # lookup only: no probing
    assert P.cached_probe(store, tmp_path / "other.mp4", probe=None) is None
    store.close()
