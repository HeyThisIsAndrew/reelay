"""The ticket's cover becomes the YouTube thumbnail: prepared within YouTube's limits, set right after the
upload, and a refusal never fails the post."""

from __future__ import annotations

from pathlib import Path

import pytest

from reelay.covers import MAX_BYTES, CoverError, prepare_cover


def _image(path: Path, w: int, h: int, fmt: str, noise: bool = False) -> Path:
    from PySide6.QtGui import QColor, QImage
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor("#336699"))
    if noise:                                   # incompressible pixels, so the file is big
        import random
        rnd = random.Random(1)
        for y in range(0, h):
            for x in range(0, w, 2):
                img.setPixel(x, y, rnd.getrandbits(24))
    assert img.save(str(path), fmt)
    return path


def test_a_small_jpeg_or_png_is_sent_as_it_is(qtbot, tmp_path):
    png = _image(tmp_path / "c.png", 1280, 720, "PNG")
    data, mime = prepare_cover(png)
    assert mime == "image/png" and data == png.read_bytes()
    jpg = _image(tmp_path / "c.jpg", 1280, 720, "JPEG")
    assert prepare_cover(jpg) == (jpg.read_bytes(), "image/jpeg")


def test_other_formats_and_big_files_become_a_jpeg_under_two_megabytes(qtbot, tmp_path):
    webp = _image(tmp_path / "c.webp", 1280, 720, "WEBP")
    data, mime = prepare_cover(webp)
    assert mime == "image/jpeg" and data[:3] == b"\xff\xd8\xff"
    big = _image(tmp_path / "big.png", 3840, 2160, "PNG", noise=True)
    assert big.stat().st_size > MAX_BYTES
    data, mime = prepare_cover(big)
    assert mime == "image/jpeg" and len(data) <= MAX_BYTES


def test_a_missing_or_broken_cover_says_so(qtbot, tmp_path):
    with pytest.raises(CoverError, match="isn't there"):
        prepare_cover(tmp_path / "gone.jpg")
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")
    with pytest.raises(CoverError, match="can't read"):
        prepare_cover(broken)


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.headers = status, body or {}, {}
        self.text = ""

    def json(self):
        return self._body


class _Session:
    def __init__(self, status=200, body=None):
        self.calls, self.status, self.body = [], status, body

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return _Resp(self.status, self.body)


def _client(session):
    from reelay.youtube import OAuthClient, Tokens, YouTubeClient
    return YouTubeClient(OAuthClient("id.apps.googleusercontent.com", "s"), Tokens("a", "r", 9e12), session=session)


def test_set_thumbnail_posts_the_image_to_youtube():
    session = _Session(200, {"items": [{"default": {"url": "u"}}]})
    _client(session).set_thumbnail("VID", b"\xff\xd8\xffdata", "image/jpeg")
    method, url, kw = session.calls[0]
    assert method == "POST" and url.endswith("/upload/youtube/v3/thumbnails/set")
    assert kw["params"] == {"videoId": "VID", "uploadType": "media"}
    assert kw["data"].startswith(b"\xff\xd8") and kw["headers"]["Content-Type"] == "image/jpeg"


def test_a_channel_that_cant_have_custom_thumbnails_gets_a_plain_reason():
    from reelay.youtube import YouTubeError
    with pytest.raises(YouTubeError, match="verified"):
        _client(_Session(403, {"error": {"code": 403, "message": "forbidden"}})).set_thumbnail("V", b"x", "image/jpeg")


def test_the_upload_sets_the_cover_and_a_refusal_is_only_a_note(qtbot, tmp_path):
    from reelay.uploads import UploadWorker
    from reelay.youtube import YouTubeError
    cover = _image(tmp_path / "cover.png", 1280, 720, "PNG")

    class Client:
        def __init__(self, refuse=False):
            self.refuse, self.covers = refuse, []

        def upload(self, path, resource, on_progress=None, cancel=None):
            return {"id": "VID"}

        def set_thumbnail(self, video_id, data, mime):
            if self.refuse:
                raise YouTubeError("This channel can't use custom thumbnails until it's verified.", 403)
            self.covers.append((video_id, mime))

    ok = Client()
    worker = UploadWorker(1, ok, tmp_path / "v.mp4", {})
    worker.thumbnail = cover
    worker.run()
    assert ok.covers == [("VID", "image/png")] and worker.result["id"] == "VID"
    assert worker.result.get("cover_note") == ""
    refused = Client(refuse=True)
    worker = UploadWorker(2, refused, tmp_path / "v.mp4", {})
    worker.thumbnail = cover
    worker.run()
    assert worker.result["id"] == "VID" and "verified" in worker.result["cover_note"]   # the post still counts
