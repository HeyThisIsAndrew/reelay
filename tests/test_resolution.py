"""A video's resolution: known from the full metadata, the download, or the file itself; never a blank dash."""

from __future__ import annotations

import struct

import pytest

from reelay.downloader import video_dimensions
from reelay.models import Status, VideoItem, info_dimensions

# What yt-dlp returns for a TikTok post with process=False (the Library's "load details"): no width or height at the
# top level, only on each format. The watermarked "download" format has no size at all.
TIKTOK_UNPROCESSED = {
    "id": "7309", "title": "Golden hour", "duration": 22,
    "formats": [
        {"format_id": "download", "vcodec": "h264", "url": "https://x/1"},
        {"format_id": "h264_540p", "vcodec": "h264", "width": 576, "height": 1024, "url": "https://x/2"},
        {"format_id": "h264_720p", "vcodec": "h264", "width": 720, "height": 1280, "url": "https://x/3"},
        {"format_id": "audio", "vcodec": "none", "acodec": "aac", "url": "https://x/4"},
    ],
}


def test_details_without_format_selection_still_give_the_resolution():
    """The bug: Resolution showed "—" because the top level of the details had no size."""
    item = VideoItem(platform="TikTok", video_id="7309", url="https://www.tiktok.com/@x/video/7309")
    item.update_from_info(TIKTOK_UNPROCESSED)
    assert (item.width, item.height) == (720, 1280)          # the largest video format offered


def test_which_size_wins():
    assert info_dimensions({"width": 1920, "height": 1080, "formats": [{"width": 3840, "height": 2160}]}) == \
        (1920, 1080)                                          # what yt-dlp selected
    assert info_dimensions({"requested_downloads": [{"width": 1280, "height": 720}],
                            "formats": [{"width": 1920, "height": 1080}]}) == (1280, 720)   # what was downloaded
    assert info_dimensions({"formats": [{"vcodec": "none", "width": 1, "height": 1}]}) is None
    assert info_dimensions({}) is None
    item = VideoItem(platform="TikTok", video_id="1", url="u", width=720, height=1280)
    item.update_from_info({"title": "t"})
    assert (item.width, item.height) == (720, 1280)           # never blanks a known size


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _tkhd(width: int, height: int, rotated: bool = False, version: int = 0) -> bytes:
    times = struct.pack(">IIII I", 0, 0, 1, 0, 1000) if version == 0 else struct.pack(">QQII Q", 0, 0, 1, 0, 1000)
    a, b, c, d = (0, 0x10000, -0x10000, 0) if rotated else (0x10000, 0, 0, 0x10000)
    matrix = struct.pack(">iiiiiiiii", a, b, 0, c, d, 0, 0, 0, 0x40000000)
    body = bytes([version, 0, 0, 3]) + times + b"\0" * 8 + struct.pack(">hhhH", 0, 0, 0, 0) + matrix + \
        struct.pack(">II", width << 16, height << 16)
    return _box(b"tkhd", body)


def _mp4(*tracks: bytes) -> bytes:
    moov = _box(b"moov", _box(b"mvhd", b"\0" * 100) + b"".join(_box(b"trak", t) for t in tracks))
    return _box(b"ftyp", b"isom\0\0\0\0isom") + _box(b"mdat", b"\0" * 64) + moov     # moov at the end, as often


def test_size_is_read_from_the_downloaded_file(tmp_path):
    plain = tmp_path / "a.mp4"
    plain.write_bytes(_mp4(_tkhd(0, 0), _tkhd(1080, 1920)))                     # audio track first: skipped
    assert video_dimensions(plain) == (1080, 1920)
    turned = tmp_path / "b.mp4"
    turned.write_bytes(_mp4(_tkhd(1920, 1080, rotated=True, version=1)))         # stored landscape, shown upright
    assert video_dimensions(turned) == (1080, 1920)
    junk = tmp_path / "c.mp4"
    junk.write_bytes(b"not a video at all")
    assert video_dimensions(junk) is None
    assert video_dimensions(tmp_path / "missing.mp4") is None


def test_download_records_the_downloaded_size(tmp_path, monkeypatch):
    import reelay.downloader as dl
    folder = tmp_path / "lib"
    folder.mkdir()
    target = folder / "ok.mp4"

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {"id": "1", "title": "t", "formats": [{"vcodec": "h264", "width": 1080, "height": 1920}]}

        def process_ie_result(self, info, download=True):
            target.write_bytes(_mp4(_tkhd(540, 960)))
            return {"requested_downloads": [{"filepath": str(target), **FakeYDL.got}]}
    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", FakeYDL)
    item = VideoItem(platform="TikTok", video_id="1", url="https://www.tiktok.com/@x/video/1")
    FakeYDL.got = {"width": 720, "height": 1280}             # yt-dlp says what it fetched
    done = dl.download_item(item, folder, "720p", {}, lambda i: "ok")
    assert (done.width, done.height) == (720, 1280)
    target.unlink()
    FakeYDL.got = {}                                          # it doesn't say: the file does
    done = dl.download_item(item, folder, "best", {}, lambda i: "ok")
    assert (done.width, done.height) == (540, 960)


@pytest.fixture
def detail(qapp, tmp_path):
    from reelay.gui.detail_panel import DetailPanel
    from reelay.gui.thumbnails import ThumbnailCache
    return DetailPanel(ThumbnailCache(tmp_path / "thumbs"))


def test_library_details_list_known_facts_and_say_when_size_comes(detail):
    listed = VideoItem(platform="TikTok", video_id="7399", url="https://www.tiktok.com/@x/video/7399", title="Eats",
                       upload_date="20261001", duration=45, view_count=6100, uploader="juniperlane")
    facts = dict(detail.facts_for(listed))
    assert facts["Views"] == "6.1K" and "Likes" not in facts and "Comments" not in facts   # unknown: no row
    assert facts["Resolution"] == "Known after download" and "—" not in facts.values()
    detail.show_item(listed, "TikTok_7399.mp4")
    assert list(detail.fact_values)[0] == "Platform" and detail.fact_values["Platform"].text() == "TikTok"
    logo = detail.fact_values["Platform"].parentWidget().findChildren(type(detail.fact_values["Platform"]))
    assert any(lab.pixmap() is not None and not lab.pixmap().isNull() for lab in logo)   # the platform's logo
    done = VideoItem(platform="TikTok", video_id="7309", url="u", width=720, height=1280, like_count=1203,
                     status=Status.DOWNLOADED, filename="a.mp4")
    facts = dict(detail.facts_for(done))
    assert facts["Resolution"] == "720×1280 · 720p" and facts["Likes"] == "1.2K"
    done.width = done.height = None
    assert "Resolution" not in dict(detail.facts_for(done))      # downloaded but unreadable: left out, not a dash
