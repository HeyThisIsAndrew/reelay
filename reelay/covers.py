"""A ticket's cover image, made ready for YouTube's thumbnail.

YouTube takes a JPEG or PNG of at most 2 MB (thumbnails.set). A cover that already fits is sent exactly as it
is; anything else (WEBP, HEIC, a huge PNG) is converted to a JPEG, scaled to fit 1920 x 1080 and compressed
until it fits. Nothing is written to disk and the cover file is never changed.

QImage is safe to use off the GUI thread, so the upload worker calls this directly.
"""

from __future__ import annotations

from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024            # YouTube's limit for a custom thumbnail
MAX_SIZE = (1920, 1080)                # bigger than YouTube shows; keeps the JPEG well under the limit
JPEG_MAGIC, PNG_MAGIC = b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n"


class CoverError(Exception):
    """Why a cover can't be used, in words a person can act on."""


def prepare_cover(path: Path | str) -> tuple[bytes, str]:
    """(image bytes, MIME type) ready for YouTube, or CoverError."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise CoverError(f"The cover image isn't there any more ({path.name}). Choose it again on the ticket.") \
            from None
    except OSError as exc:
        raise CoverError(f"Reelay can't open the cover image ({path.name}): {exc.strerror or exc}") from None
    if len(data) <= MAX_BYTES:
        if data.startswith(JPEG_MAGIC):
            return data, "image/jpeg"
        if data.startswith(PNG_MAGIC):
            return data, "image/png"
    return _as_jpeg(data, path.name), "image/jpeg"


def _as_jpeg(data: bytes, name: str) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PySide6.QtGui import QImage

    image = QImage.fromData(QByteArray(data))
    if image.isNull():
        raise CoverError(f"Reelay can't read the cover image ({name}). Use a JPEG or PNG.")
    if image.width() > MAX_SIZE[0] or image.height() > MAX_SIZE[1]:
        image = image.scaled(*MAX_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    for quality in (92, 85, 75, 65, 55):
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        image.save(buf, "JPEG", quality)
        out = bytes(buf.data())
        if len(out) <= MAX_BYTES:
            return out
    raise CoverError(f"The cover image ({name}) is too detailed to fit YouTube's 2 MB limit. Try a smaller one.")
