"""Small platform marks (YouTube, TikTok, Instagram, Bluesky, Threads,
Facebook, Snapchat), drawn with QPainter so they're crisp at any size and need no image
files. They're simplified badges in each platform's colours, used only to say
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
THREADS_AT = ("M16.8 11.1a6.6 6.6 0 0 0-.3-.1c-.2-3-1.8-4.7-4.6-4.7-1.7 0-3.1.7-3.9 2l1.5 1c.6-.9 1.6-1.1 2.4-1.1 1 "
              "0 1.7.3 2.1.9.3.4.5 1 .6 1.7a10 10 0 0 0-2.3-.1c-2.3.1-3.8 1.5-3.7 3.4.1 1 .6 1.8 1.4 2.3.7.5 1.6.7 "
              "2.6.6 1.3-.1 2.3-.6 3-1.4.5-.7.9-1.5 1-2.6.6.4 1.1.9 1.3 1.5.4 1 .4 2.6-.9 3.9-1.2 1.2-2.6 1.7-4.8 "
              "1.7-2.4 0-4.2-.8-5.4-2.3-1.1-1.4-1.7-3.4-1.7-6s.6-4.6 1.7-6c1.2-1.5 3-2.3 5.4-2.3 2.4 0 4.3.8 5.5 "
              "2.3.6.7 1.1 1.7 1.4 2.8l1.7-.5c-.4-1.4-1-2.5-1.8-3.5C17.5 1.1 15.2.1 12.2.1h-.1C9.2.1 6.9 1.1 5.4 3c"
              "-1.4 1.7-2.1 4.1-2.1 7.1s.7 5.4 2.1 7.1c1.5 1.9 3.8 2.9 6.7 2.9h.1c2.6 0 4.4-.7 5.9-2.2 2-2 1.9-4.5 "
              "1.3-6-.5-1.1-1.4-2-2.6-2.6zm-4.5 4.4c-1 .1-2.1-.4-2.2-1.4 0-.7.5-1.5 2.2-1.6h.6c.6 0 1.2.1 1.8.2-.2 "
              "2.4-1.3 2.7-2.4 2.8z")
BLUESKY_BUTTERFLY = ("M12 11.2C10.7 8.6 7.2 4 4.6 3.6 3 3.4 2.6 4.6 2.6 6c0 1.1.6 5 1 5.6.9 1.6 3 2 4.7 1.6-3 .5"
                     "-4.1 2.3-2.4 4.1 3 3.1 4.6-.7 5.4-2.6l.7-1.7.7 1.7c.8 1.9 2.4 5.7 5.4 2.6 1.7-1.8.6-3.6-2.4"
                     "-4.1 1.7.4 3.8 0 4.7-1.6.4-.6 1-4.5 1-5.6 0-1.4-.4-2.6-2-2.4-2.6.4-6.1 5-7.4 7.6z")
FACEBOOK_MARK = ("M12 2a10 10 0 0 0-1.6 19.9v-7H7.9V12h2.5V9.8c0-2.5 1.5-3.9 3.8-3.9 1.1 0 2.2.2 2.2.2v2.5h-1.3c-1.2 "
                 "0-1.6.8-1.6 1.6V12h2.8l-.4 2.9h-2.3v7A10 10 0 0 0 12 2z")


def _svg(p: QPainter, rect: QRectF, body: str, view: str = "0 0 24 24") -> None:
    """Draw 24 x 24 SVG markup (paths with their own fills) into `rect`, as crisp vectors."""
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}">{body}</svg>'
    QSvgRenderer(QByteArray(svg.encode())).render(p, rect)


def _youtube(p: QPainter, s: float) -> None:
    body = QRectF(0, s * 0.18, s, s * 0.64)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#FF0000"))
    p.drawRoundedRect(body, s * 0.18, s * 0.18)
    tri = QPainterPath(QPointF(s * 0.40, s * 0.34))
    tri.lineTo(s * 0.68, s * 0.50)
    tri.lineTo(s * 0.40, s * 0.66)
    tri.closeSubpath()
    p.setBrush(QColor("#FFFFFF"))
    p.drawPath(tri)


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
    _svg(p, QRectF(0, s * 0.02, s, s), f'<path fill="#1185FE" d="{BLUESKY_BUTTERFLY}"/>', "1.6 2.6 21 21")


def _threads(p: QPainter, s: float) -> None:
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#111111"))
    p.drawRoundedRect(QRectF(0, 0, s, s), s * 0.24, s * 0.24)
    # The ribbon spans y 0.1–20.1 of its grid: a viewBox centred on it keeps it centred in the badge.
    _svg(p, QRectF(s * 0.2, s * 0.2, s * 0.6, s * 0.6), f'<path fill="#FFFFFF" d="{THREADS_AT}"/>',
         "0.9 -0.9 22 22")


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


DRAW = {"youtube": _youtube, "tiktok": _tiktok, "instagram": _instagram, "bluesky": _bluesky,
        "threads": _threads, "facebook": _facebook, "snapchat": _snapchat}


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
