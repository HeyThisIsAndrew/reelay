"""Small platform marks (YouTube, TikTok, Instagram, Bluesky, Threads,
Facebook, Snapchat), drawn with QPainter so they're crisp at any size and need no image
files. They're the platforms' own marks in their colours (YouTube Shorts has its own), used only to say
which platform something is about."""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer

RATIO = 2


def _canvas(size: int) -> tuple[QPixmap, QPainter]:
    pix = QPixmap(size * RATIO, size * RATIO)
    pix.setDevicePixelRatio(RATIO)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    return pix, p


def _text(p: QPainter, rect: QRectF, text: str, color: str, scale: float, bold: bool = True) -> None:
    font = QFont()
    font.setPixelSize(max(6, int(rect.height() * scale)))
    font.setBold(bold)
    p.setFont(font)
    p.setPen(QColor(color))
    p.drawText(rect, Qt.AlignCenter, text)


# Vector glyphs on a 24 x 24 grid (the same paths the website draws), filled in each platform's colours.
TIKTOK_NOTE = ("M14.2 3h3c.2 1.9 1.5 3.4 3.4 3.6v3.1a7.1 7.1 0 0 1-3.4-1v6.2a5.6 5.6 0 1 1-5.6-5.6c.3 0 .6 0 "
               ".9.1v3.2a2.5 2.5 0 1 0 1.7 2.3z")
# The corrected Threads mark (as on Be Unconventional HQ), on a 640 x 640 grid.
THREADS_AT = ("M427.5 299.7C429.7 300.6 431.7 301.6 433.8 302.5C463 316.6 484.4 337.7 495.6 363.9C511.3 400.4 512.8 "
              "459.7 465.3 507.1C429.1 543.3 385 559.6 322.7 560.1L322.4 560.1C252.2 559.6 198.3 536 162 489.9C129.7 "
              "448.9 113.1 391.8 112.5 320.3L112.5 319.8C113 248.3 129.6 191.2 161.9 150.2C198.2 104.1 252.2 80.5 "
              "322.4 80L322.7 80C393 80.5 447.6 104 485 149.9C503.4 172.6 517 199.9 525.6 231.6L485.2 242.4C478.1 "
              "216.6 467.4 194.6 453 177C423.8 141.2 380 122.8 322.5 122.4C265.5 122.9 222.4 141.2 194.3 176.8C168.1 "
              "210.1 154.5 258.3 154 320C154.5 381.7 168.1 429.9 194.3 463.3C222.3 498.9 265.5 517.2 322.5 517.7C373.9 "
              "517.3 407.9 505.1 436.2 476.8C468.5 444.6 467.9 405 457.6 380.9C451.5 366.7 440.5 354.9 425.7 346C422 "
              "372.9 413.9 394.3 401 410.8C383.9 432.6 359.6 444.4 328.3 446.1C304.7 447.4 282 441.7 264.4 430.1C243.6 "
              "416.3 231.4 395.3 230.1 370.8C227.6 322.5 265.8 287.8 325.3 284.4C346.4 283.2 366.2 284.1 384.5 "
              "287.2C382.1 272.4 377.2 260.6 369.9 252C359.9 240.3 344.3 234.3 323.7 234.2L323 234.2C306.4 234.2 284 "
              "238.8 269.7 260.5L235.3 236.9C254.5 207.8 285.6 191.8 323.1 191.8L323.9 191.8C386.5 192.2 423.8 231.3 "
              "427.6 299.5L427.4 299.7L427.5 299.7zM271.5 368.5C272.8 393.6 299.9 405.3 326.1 403.8C351.7 402.4 380.7 "
              "392.4 385.6 330.6C372.4 327.7 357.8 326.2 342.2 326.2C337.4 326.2 332.6 326.3 327.8 326.6C284.9 329 "
              "270.6 349.8 271.6 368.4L271.5 368.5z")
# Bluesky's butterfly, on its own 64 x 56.53 grid.
BLUESKY_BUTTERFLY = ("M13.873 3.805C21.21 9.332 29.103 20.537 32 26.55v15.882c0-.338-.13.044-.41.867-1.512 4.456-7.418 "
                     "21.847-20.923 7.944-7.111-7.32-3.819-14.64 9.125-16.85-7.405 1.264-15.73-.825-18.014-9.015C1.12 "
                     "23.022 0 8.51 0 6.55 0-3.268 8.579-.182 13.873 3.805m36.254 0C42.79 9.332 34.897 20.537 32 "
                     "26.55v15.882c0-.338.13.044.41.867 1.512 4.456 7.418 21.847 20.923 7.944 7.111-7.32 3.819-14.64-9.125"
                     "-16.85 7.405 1.264 15.73-.825 18.014-9.015C62.88 23.022 64 8.51 64 6.55c0-9.818-8.578-6.732-13.873"
                     "-2.745")
# YouTube's play button, and the YouTube Shorts mark (24 x 24; the play triangle is drawn white on top).
YOUTUBE_MARK = ("M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 "
                "3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 "
                "9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502"
                "-5.814z")
YOUTUBE_PLAY = "M9.545 15.568V8.432L15.818 12z"
SHORTS_MARK = ("m18.931 9.99-1.441-.601 1.717-.913a4.48 4.48 0 0 0 1.874-6.078 4.506 4.506 0 0 0-6.09-1.874L4.792 "
               "5.929a4.504 4.504 0 0 0-2.402 4.193 4.521 4.521 0 0 0 2.666 3.904c.036.012 1.442.6 1.442.6l-1.706.901a4.51 "
               "4.51 0 0 0-2.369 3.967A4.528 4.528 0 0 0 6.93 24c.725 0 1.437-.174 2.08-.508l10.21-5.406a4.494 4.494 0 "
               "0 0 2.39-4.192 4.525 4.525 0 0 0-2.678-3.904Z")
SHORTS_PLAY = "M9.597 15.19V8.824l6.007 3.184z"
FACEBOOK_MARK = ("M12 2a10 10 0 0 0-1.6 19.9v-7H7.9V12h2.5V9.8c0-2.5 1.5-3.9 3.8-3.9 1.1 0 2.2.2 2.2.2v2.5h-1.3c-1.2 "
                 "0-1.6.8-1.6 1.6V12h2.8l-.4 2.9h-2.3v7A10 10 0 0 0 12 2z")


def _svg(p: QPainter, rect: QRectF, body: str, view: str = "0 0 24 24") -> None:
    """Draw 24 x 24 SVG markup (paths with their own fills) into `rect`, as crisp vectors."""
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}">{body}</svg>'
    QSvgRenderer(QByteArray(svg.encode())).render(p, rect)


def _youtube(p: QPainter, s: float) -> None:
    _svg(p, QRectF(0, 0, s, s), f'<path fill="#FF0000" d="{YOUTUBE_MARK}"/><path fill="#FFFFFF" d="{YOUTUBE_PLAY}"/>')


def _youtube_shorts(p: QPainter, s: float) -> None:
    _svg(p, QRectF(0, 0, s, s), f'<path fill="#FF0000" d="{SHORTS_MARK}"/><path fill="#FFFFFF" d="{SHORTS_PLAY}"/>')


def _tiktok(p: QPainter, s: float) -> None:
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#111111"))
    p.drawRoundedRect(QRectF(0, 0, s, s), s * 0.24, s * 0.24)
    inner = QRectF(s * 0.16, s * 0.16, s * 0.68, s * 0.68)
    shift = s * 0.035                                        # the cyan and red echoes behind the white note
    for dx, dy, color in ((-shift, -shift, "#25F4EE"), (shift, shift, "#FE2C55"), (0, 0, "#FFFFFF")):
        _svg(p, inner.translated(dx, dy), f'<path fill="{color}" d="{TIKTOK_NOTE}"/>')


def _instagram(p: QPainter, s: float) -> None:
    grad = QLinearGradient(0, s, s, 0)
    grad.setColorAt(0.0, QColor("#FEDA75"))
    grad.setColorAt(0.35, QColor("#FA7E1E"))
    grad.setColorAt(0.6, QColor("#D62976"))
    grad.setColorAt(1.0, QColor("#4F5BD5"))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(grad))
    p.drawRoundedRect(QRectF(0, 0, s, s), s * 0.28, s * 0.28)
    pen = QPen(QColor("#FFFFFF"), max(1.0, s * 0.08))
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(QRectF(s * 0.2, s * 0.2, s * 0.6, s * 0.6), s * 0.18, s * 0.18)
    p.drawEllipse(QPointF(s * 0.5, s * 0.5), s * 0.14, s * 0.14)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#FFFFFF"))
    p.drawEllipse(QPointF(s * 0.67, s * 0.33), s * 0.04, s * 0.04)


def _bluesky(p: QPainter, s: float) -> None:
    h = s * 56.53 / 64                       # the butterfly is wider than tall: centred in the square
    _svg(p, QRectF(0, (s - h) / 2, s, h), f'<path fill="#1185FE" d="{BLUESKY_BUTTERFLY}"/>', "0 0 64 56.53")


def _threads(p: QPainter, s: float) -> None:
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#111111"))
    p.drawRoundedRect(QRectF(0, 0, s, s), s * 0.24, s * 0.24)
    # The @ spans x 112–526 and y 80–560 of its grid: a square view centred on it keeps it centred in the badge.
    _svg(p, QRectF(s * 0.17, s * 0.17, s * 0.66, s * 0.66), f'<path fill="#FFFFFF" d="{THREADS_AT}"/>',
         "79 80 480 480")


def _facebook(p: QPainter, s: float) -> None:
    # The blue mark has the "f" cut out of it; a white disc behind shows the "f" in white.
    _svg(p, QRectF(0, 0, s, s), f'<circle cx="12" cy="12" r="9.4" fill="#FFFFFF"/>'
                                f'<path fill="#0866FF" d="{FACEBOOK_MARK}"/>')


def _snapchat(p: QPainter, s: float) -> None:
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#FFFC00"))
    p.drawRoundedRect(QRectF(0, 0, s, s), s * 0.24, s * 0.24)
    ghost = QPainterPath()                      # the ghost: round head, little arms, scalloped hem
    ghost.moveTo(s * 0.30, s * 0.58)
    ghost.lineTo(s * 0.31, s * 0.42)
    ghost.cubicTo(s * 0.31, s * 0.15, s * 0.69, s * 0.15, s * 0.69, s * 0.42)
    ghost.lineTo(s * 0.70, s * 0.58)
    ghost.quadTo(s * 0.84, s * 0.60, s * 0.76, s * 0.68)
    ghost.quadTo(s * 0.68, s * 0.70, s * 0.63, s * 0.79)
    ghost.quadTo(s * 0.57, s * 0.73, s * 0.50, s * 0.79)
    ghost.quadTo(s * 0.43, s * 0.73, s * 0.37, s * 0.79)
    ghost.quadTo(s * 0.32, s * 0.70, s * 0.24, s * 0.68)
    ghost.quadTo(s * 0.16, s * 0.60, s * 0.30, s * 0.58)
    p.setBrush(QColor("#FFFFFF"))
    p.setPen(QPen(QColor("#111111"), max(1.0, s * 0.05)))
    p.drawPath(ghost)


def _local(p: QPainter, s: float) -> None:
    """A video made on this computer (not downloaded from a platform): a small laptop."""
    p.setRenderHint(QPainter.Antialiasing)
    ink = QColor("#3a3a3a")
    p.setPen(QPen(ink, max(1.0, s * 0.09)))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(QRectF(s * 0.17, s * 0.2, s * 0.66, s * 0.46), s * 0.06, s * 0.06)
    p.setPen(Qt.NoPen)
    p.setBrush(ink)
    p.drawRoundedRect(QRectF(s * 0.06, s * 0.72, s * 0.88, s * 0.1), s * 0.04, s * 0.04)


DRAW = {"youtube": _youtube, "youtube_shorts": _youtube_shorts, "tiktok": _tiktok, "instagram": _instagram,
        "bluesky": _bluesky, "threads": _threads, "facebook": _facebook, "snapchat": _snapchat,
        "local": _local}


@lru_cache(maxsize=64)
def platform_pixmap(platform: str, size: int = 16, faded: bool = False) -> QPixmap:
    pix, p = _canvas(size)
    if faded:
        p.setOpacity(0.72)   # "to do": still clearly recognizable; the card adds a dashed ring
    draw = DRAW.get(platform)
    if draw:
        draw(p, float(size))
    else:
        _text(p, QRectF(0, 0, size, size), platform[:1].upper(), "#888888", 0.8)
    p.end()
    return pix


def platform_icon(platform: str, size: int = 16) -> QIcon:
    return QIcon(platform_pixmap(platform, size))
