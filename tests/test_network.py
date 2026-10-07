"""Live checks against real platforms. Run with:  pytest --run-network -m network"""

import re
import subprocess

import pytest

from reelay.downloader import QUALITY_BEST, download_item
from reelay.fetcher import list_videos
from reelay.naming import DEFAULT_TEMPLATE, render_stem
from reelay.ytdl import base_options, find_ffmpeg

pytestmark = pytest.mark.network
TIKTOK_PROFILE = "https://www.tiktok.com/@livingood.lifestyle"


def test_tiktok_listing_has_metadata():
    _, items = list_videos(TIKTOK_PROFILE, base_options(), limit=3)
    assert len(items) == 3
    assert all(i.platform == "TikTok" and i.upload_date and i.thumbnail_url for i in items)


def test_tiktok_download_is_clean_and_has_full_audio(tmp_path):
    _, items = list_videos(TIKTOK_PROFILE, base_options(), limit=1)
    done = download_item(items[0], tmp_path, QUALITY_BEST, base_options(),
                         name_for=lambda i: render_stem(DEFAULT_TEMPLATE, i))
    assert re.fullmatch(r"TikTok_\d{8}_\d+\.mp4", done.filename)
    assert done.watermark_free and "audio" not in done.format_id
    # ffmpeg prints stream info to stderr; both streams must be present
    out = subprocess.run([find_ffmpeg(), "-i", str(tmp_path / done.filename)], capture_output=True, text=True).stderr
    assert "Video:" in out and "Audio:" in out
