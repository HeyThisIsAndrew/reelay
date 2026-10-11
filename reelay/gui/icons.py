"""Reelay's line icons: simple 24×24 strokes drawn for Reelay, tinted from the theme.

``icon(name)`` gives a QIcon in the secondary text colour that turns the accent colour when its
button is checked (the current page in the sidebar), so the brand shows exactly where you are.
Icons are re-tinted after a theme change by calling ``icon()`` again (results are cached per colour).
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from . import theme

# Each value is the inside of an <svg viewBox="0 0 24 24"> drawn with a stroke (no fills unless noted).
PATHS: dict[str, str] = {
    "library": '<rect x="3" y="4" width="18" height="14" rx="2.5"/><path d="M10 8.5v5l4-2.5z"/>'
               '<path d="M7 21h10"/>',
    "board": '<rect x="3" y="4" width="5" height="16" rx="1.5"/><rect x="9.5" y="4" width="5" height="11" rx="1.5"/>'
             '<rect x="16" y="4" width="5" height="7" rx="1.5"/>',
    "bolt": '<path d="M13 3L5 13.5h6l-1 7.5 8-10.5h-6z"/>',
    "accounts": '<circle cx="8" cy="8" r="3"/><circle cx="17" cy="9.5" r="2.5"/><path d="M3 19c.6-3 2.6-4.5 5-4.5'
                's4.4 1.5 5 4.5"/><path d="M14.5 15.2c.8-.5 1.6-.7 2.5-.7 2 0 3.5 1.3 4 3.5"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 2.8v2.4M12 18.8v2.4M2.8 12h2.4M18.8 12h2.4'
                'M5.5 5.5l1.7 1.7M16.8 16.8l1.7 1.7M5.5 18.5l1.7-1.7M16.8 7.2l1.7-1.7"/>',
    "help": '<circle cx="12" cy="12" r="9"/><path d="M9.6 9.3a2.5 2.5 0 0 1 4.8.9c0 1.7-2.4 2.1-2.4 3.6"/>'
            '<path d="M12 17.2v.1"/>',
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
    "filter": '<path d="M4 6h16M7 12h10M10 18h4"/>',
    "grid": '<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/>'
            '<rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>',
    "list": '<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4.5 6h.1M4.5 12h.1M4.5 18h.1"/>',
    "gear": '<circle cx="12" cy="12" r="3"/><path d="M19.4 13.5a7.5 7.5 0 0 0 0-3l2-1.5-2-3.4-2.4 1a7.5 7.5 0 0 0'
            '-2.6-1.5L14 2.5h-4l-.4 2.6A7.5 7.5 0 0 0 7 6.6l-2.4-1-2 3.4 2 1.5a7.5 7.5 0 0 0 0 3l-2 1.5 2 3.4'
            ' 2.4-1a7.5 7.5 0 0 0 2.6 1.5l.4 2.6h4l.4-2.6a7.5 7.5 0 0 0 2.6-1.5l2.4 1 2-3.4z"/>',
    "play": '<path d="M8 5.5v13l10.5-6.5z" fill="{c}"/>',
    "pause": '<path d="M8.5 5.5v13M15.5 5.5v13" stroke-width="2.6"/>',
    "volume": '<path d="M4 9.5h3.5L12 6v12l-4.5-3.5H4z"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/>',
    "mute": '<path d="M4 9.5h3.5L12 6v12l-4.5-3.5H4z"/><path d="M16 9.5l5 5M21 9.5l-5 5"/>',
    "loop": '<path d="M17 3.5l3 3-3 3"/><path d="M4 11.5v-1a4 4 0 0 1 4-4h12"/><path d="M7 20.5l-3-3 3-3"/>'
            '<path d="M20 12.5v1a4 4 0 0 1-4 4H4"/>',
    "popout": '<path d="M14 4h6v6"/><path d="M20 4l-8.5 8.5"/><path d="M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5'
              ' 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10"/>',
    "external": '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M19 14v5H5V5h5"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "clock": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    "sparkles": '<path d="M12 3.5l1.6 4.4 4.4 1.6-4.4 1.6L12 15.5l-1.6-4.4L6 9.5l4.4-1.6z"/>'
                '<path d="M18.5 15l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z"/>',
    "hourglass": '<path d="M7 3.5h10M7 20.5h10M8 3.5c0 4.5 8 4.5 8 8.5s-8 4-8 8.5M16 3.5c0 4.5-8 4.5-8 8.5'
                 's8 4 8 8.5"/>',
    "download": '<path d="M12 4v11"/><path d="M7.5 10.5L12 15l4.5-4.5"/><path d="M5 19.5h14"/>',
    "close": '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    "more": '<path d="M6 12h.1M12 12h.1M18 12h.1" stroke-width="2.6"/>',
    "refresh": '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4h-4"/>',
    "folder": '<path d="M3.5 7.5A1.5 1.5 0 0 1 5 6h4.5l2 2.5H19a1.5 1.5 0 0 1 1.5 1.5v7.5A1.5 1.5 0 0 1 19 19H5'
              'a1.5 1.5 0 0 1-1.5-1.5z"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "sidebar": '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M9.5 4.5v15"/>',
    "calendar": '<rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
    "link": '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3'
            'a4 4 0 0 0 5.7 5.7l1-1"/>',
    "warning": '<path d="M12 4l9 16H3z"/><path d="M12 10v4.5M12 17.3v.1"/>',
}

_cache: dict[tuple[str, str, int, float], QPixmap] = {}


def pixmap(name: str, color: str | None = None, size: int = 18, ratio: float = 2.0) -> QPixmap:
    color = color or theme.P.text_secondary
    key = (name, color, size, ratio)
    if key in _cache:
        return _cache[key]
    body = PATHS[name].replace("{c}", color)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
           f'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    pix = QPixmap(int(size * ratio), int(size * ratio))
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size * ratio, size * ratio))
    painter.end()
    pix.setDevicePixelRatio(ratio)
    _cache[key] = pix
    return pix


def icon(name: str, size: int = 18, color: str | None = None, active: str | None = None) -> QIcon:
    """Secondary-text tint normally; ``active`` (default: the accent) when its button is checked."""
    result = QIcon()
    normal = pixmap(name, color or theme.P.text_secondary, size)
    result.addPixmap(normal, QIcon.Normal, QIcon.Off)
    result.addPixmap(pixmap(name, theme.P.text_primary, size), QIcon.Active, QIcon.Off)
    on = pixmap(name, active or theme.P.accent_color, size)
    for mode in (QIcon.Normal, QIcon.Active, QIcon.Selected):
        result.addPixmap(on, mode, QIcon.On)
    result.addPixmap(pixmap(name, theme.P.text_muted, size), QIcon.Disabled, QIcon.Off)
    return result


def size(px: int = 18) -> QSize:
    return QSize(px, px)
