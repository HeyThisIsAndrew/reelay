"""Readiness checks the file that will be posted (probe.MediaInfo), with each platform's current limits."""

from __future__ import annotations

from pathlib import Path

import pytest

from reelay.models import Platform, Post
from reelay.probe import MediaInfo
from reelay.readiness import MB, GB, evaluate, is_facebook_reel

ALL = frozenset({Platform.YOUTUBE, Platform.BLUESKY, Platform.THREADS, Platform.FACEBOOK})


def _post(**kw):
    base = dict(video_row=1, title="A title", description="A description", bluesky_text="Hi",
                threads_text="Hi", facebook_text="Hi",
                target_platforms=[Platform.YOUTUBE, Platform.BLUESKY, Platform.THREADS, Platform.FACEBOOK])
    return Post(**{**base, **kw})


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "v.mp4"
    path.write_bytes(b"\0" * 1000)
    return path


def _messages(readiness, platform):
    return [c.message for c in readiness.platforms[platform].checks]


def test_bluesky_takes_ten_minutes_and_300_mb_now(video):
    five = MediaInfo(size=50 * MB, duration=300, width=1080, height=1920)
    assert evaluate(_post(), ALL, video, media=five).platforms[Platform.BLUESKY].ready       # was blocked at 3 min
    long = MediaInfo(size=50 * MB, duration=660, width=1080, height=1920)
    r = evaluate(_post(), ALL, video, media=long)
    assert not r.platforms[Platform.BLUESKY].ready and any("10 minutes" in m for m in _messages(r, Platform.BLUESKY))
    big = MediaInfo(size=350 * MB, duration=60, width=1080, height=1920)
    r = evaluate(_post(), ALL, video, media=big)
    assert any("300 MB" in m and "350 MB" in m for m in _messages(r, Platform.BLUESKY))


def test_facebook_says_reel_or_page_video_and_caps_at_1_gb(video):
    reel = evaluate(_post(), ALL, video, media=MediaInfo(size=10 * MB, duration=30, width=1080, height=1920))
    assert "Posts as a Reel (vertical, 3–90 seconds)." in _messages(reel, Platform.FACEBOOK)
    wide = evaluate(_post(), ALL, video, media=MediaInfo(size=10 * MB, duration=30, width=1920, height=1080))
    assert "Posts as a regular Page video." in _messages(wide, Platform.FACEBOOK)
    huge = evaluate(_post(), ALL, video, media=MediaInfo(size=int(1.2 * GB), duration=600, width=1920, height=1080))
    assert not huge.platforms[Platform.FACEBOOK].ready
    assert is_facebook_reel(1080, 1920, 90) and not is_facebook_reel(1080, 1920, 91)
    assert not is_facebook_reel(1080, 1920, 2) and not is_facebook_reel(1920, 1080, 30)


def test_youtube_and_threads_read_the_real_file(video):
    short = evaluate(_post(), ALL, video, media=MediaInfo(size=MB, duration=50, width=1080, height=1920))
    assert any(m.startswith("Posts as a Short") for m in _messages(short, Platform.YOUTUBE))
    # the platform said 30 seconds; the file (an edit, a different download) is really 6 minutes
    real = evaluate(_post(), ALL, video, duration=30, media=MediaInfo(size=MB, duration=360, width=1080, height=1920))
    assert any("5 minutes" in m for m in _messages(real, Platform.THREADS))
    assert any(m.startswith("Posts as a regular") for m in _messages(real, Platform.YOUTUBE))
    big = evaluate(_post(), ALL, video, media=MediaInfo(size=int(1.1 * GB), duration=60, width=1080, height=1920))
    assert any("1 GB" in m for m in _messages(big, Platform.THREADS))


def test_without_a_probe_the_size_on_disk_still_counts(tmp_path):
    path = tmp_path / "big.mp4"
    with open(path, "wb") as f:
        f.truncate(310 * MB)                                     # sparse: instant, takes no space
    r = evaluate(_post(), ALL, path, duration=60)
    assert any("300 MB" in m for m in _messages(r, Platform.BLUESKY))


def test_pipeline_uses_the_inspected_file_once_its_probe_is_in(tmp_path):
    from reelay.models import VideoItem
    from reelay.pipeline import Pipeline
    from reelay.probe import fingerprint
    from reelay.store import Store
    from tests.fakes import FAKE_MP4, FakeUploads
    store = Store(tmp_path / "r.db")
    profile = store.create_profile("Be Unconventional", tmp_path / "lib")
    folder = Path(profile.output_folder)
    folder.mkdir(parents=True)
    item = VideoItem(platform="TikTok", video_id="1", url="https://t/1", upload_date="20261001", duration=30,
                     filename="1.mp4", folder=str(folder))
    (folder / "1.mp4").write_bytes(FAKE_MP4)
    row_id = store.save_videos(profile.id, [item])[item.key]

    class Accounts:
        def account_for(self, *a, **k):
            return None
    pipeline = Pipeline(store, Accounts(), FakeUploads())
    pipeline.enabled_platforms = lambda: frozenset({Platform.THREADS})
    post = Post(video_row=row_id, target_platforms=[Platform.THREADS], threads_text="Hi")
    first = pipeline.readiness(row_id, post)
    assert first.platforms[Platform.THREADS].ready                  # the platform said 30 s; no probe yet
    path = folder / "1.mp4"
    store.save_probe(str(path), *fingerprint(path), MediaInfo(size=len(FAKE_MP4), duration=400, width=1080,
                                                              height=1920))
    again = pipeline.readiness(row_id, post)
    assert not again.platforms[Platform.THREADS].ready              # the file is really 6 min 40 s
    store.close()
