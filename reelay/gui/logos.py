"""Small platform marks (YouTube, TikTok, Instagram, Bluesky, Threads,
Facebook), drawn with QPainter so they're crisp at any size and need no image
files. They're simplified badges in each platform's colours, used only to say
which platform something is about."""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap

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
    r = QRectF(0, 0, s, s)
    for dx, color in ((-s * 0.04, "#25F4EE"), (s * 0.04, "#FE2C55"), (0, "#FFFFFF")):
        _text(p, r.translated(dx, 0), "♪", color, 0.72)


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
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#1185FE"))
    for sign in (-1, 1):   # two wings, upper and lower, on each side
        cx = s * 0.5 + sign * s * 0.2
        p.drawEllipse(QPointF(cx, s * 0.38), s * 0.24, s * 0.2)
        p.drawEllipse(QPointF(s * 0.5 + sign * s * 0.15, s * 0.66), s * 0.15, s * 0.15)


def _threads(p: QPainter, s: float) -> None:
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#111111"))
    p.drawRoundedRect(QRectF(0, 0, s, s), s * 0.24, s * 0.24)
    _text(p, QRectF(0, -s * 0.04, s, s), "@", "#FFFFFF", 0.78)


def _facebook(p: QPainter, s: float) -> None:
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#0866FF"))
    p.drawEllipse(QRectF(0, 0, s, s))
    _text(p, QRectF(s * 0.06, s * 0.1, s, s), "f", "#FFFFFF", 0.85)


DRAW = {"youtube": _youtube, "tiktok": _tiktok, "instagram": _instagram, "bluesky": _bluesky,
        "threads": _threads, "facebook": _facebook}


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
