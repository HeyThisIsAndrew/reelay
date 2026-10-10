"""Per-profile branding: a logo, a tagline and brand colours, so it's always clear whose profile you're in.

Saved in the settings table next to the profile (no schema change):

- ``profile_brand.<id>``: JSON ``{"tagline", "background", "accent"}``
- ``profile_logo.<id>``: the logo as a base64 PNG, scaled to at most 384 px

Keeping the logo inside the database means it travels with a shared database to other computers.
The Custom theme uses the active profile's colours; the logo is the profile's icon everywhere.
"""

from __future__ import annotations

import base64
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPainterPath, QPixmap

from ..models import Profile
from . import theme

LOGO_MAX_PX = 384
BRAND_KEY = "profile_brand.{}"
LOGO_KEY = "profile_logo.{}"


class BrandError(ValueError):
    pass


@dataclass
class Brand:
    tagline: str = ""
    background: str = ""       # Custom theme colours; "" = not chosen yet
    accent: str = ""
    logo: bytes = b""          # PNG

    @property
    def has_colors(self) -> bool:
        return bool(theme.normalize(self.background) and theme.normalize(self.accent))


# ---------------------------------------------------------------- load / save
def load(store, profile_id: int | None) -> Brand:
    if profile_id is None:
        return Brand()
    try:
        data = json.loads(store.get_setting(BRAND_KEY.format(profile_id), "") or "{}")
    except ValueError:
        data = {}
    try:
        logo = base64.b64decode(store.get_setting(LOGO_KEY.format(profile_id), "") or "")
    except ValueError:
        logo = b""
    return Brand(tagline=str(data.get("tagline", ""))[:120],
                 background=theme.normalize(str(data.get("background", ""))) or "",
                 accent=theme.normalize(str(data.get("accent", ""))) or "", logo=logo)


def save(store, profile_id: int, brand: Brand) -> None:
    store.set_setting(BRAND_KEY.format(profile_id), json.dumps(
        {"tagline": brand.tagline.strip()[:120], "background": theme.normalize(brand.background) or "",
         "accent": theme.normalize(brand.accent) or ""}))
    store.set_setting(LOGO_KEY.format(profile_id), base64.b64encode(brand.logo).decode("ascii"))


def forget(store, profile_id: int) -> None:
    """A deleted profile's branding goes with it."""
    store.set_setting(BRAND_KEY.format(profile_id), "")
    store.set_setting(LOGO_KEY.format(profile_id), "")


def custom_colors(store, profile: Profile | None) -> tuple[str, str]:
    """(background, accent) for the Custom theme: the profile's brand colours, else the defaults."""
    brand = load(store, profile.id if profile else None)
    return (brand.background, brand.accent) if brand.has_colors else theme.DEFAULT_CUSTOM


def strip_color(store, profile: Profile) -> str:
    """The colour that marks this profile in the sidebar: its brand accent, else its avatar colour."""
    brand = load(store, profile.id)
    return brand.accent or theme.normalize(profile.color) or theme.P.text_muted


# ---------------------------------------------------------------- logos
def _png(image: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    image.save(buf, "PNG")
    return bytes(data)


def logo_from_file(path: str | Path) -> bytes:
    """Read an image a person picked (PNG, JPEG, WebP, SVG…) and return a small PNG for storing."""
    image = QImage(str(path))
    if image.isNull():
        raise BrandError("That file isn't an image Reelay can read. Try a PNG or JPEG.")
    if max(image.width(), image.height()) > LOGO_MAX_PX:
        image = image.scaled(LOGO_MAX_PX, LOGO_MAX_PX, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return _png(image.convertToFormat(QImage.Format_ARGB32))


def logo_image(brand: Brand) -> QImage | None:
    if not brand.logo:
        return None
    image = QImage.fromData(brand.logo)
    return None if image.isNull() else image


def _rounded(size: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), size * 0.24, size * 0.24)
    return path


def avatar_pixmap(profile: Profile, brand: Brand | None = None, size: int = 26, ratio: int = 2) -> QPixmap:
    """The profile's icon: its logo (fitted into a rounded square), or coloured initials."""
    pix = QPixmap(size * ratio, size * ratio)
    pix.setDevicePixelRatio(ratio)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    image = logo_image(brand) if brand else None
    if image is not None:
        p.setClipPath(_rounded(size))
        if image.hasAlphaChannel():           # transparent logos sit on the brand background
            p.fillPath(_rounded(size), QColor(brand.background or theme.P.bg_raised))
        scaled = image.scaled(size * ratio, size * ratio, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        scaled.setDevicePixelRatio(ratio)
        w, h = scaled.width() / ratio, scaled.height() / ratio
        p.drawImage(QRectF((size - w) / 2, (size - h) / 2, w, h), scaled)
    else:
        p.setBrush(QColor(profile.color))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(0, 0, size, size))
        font = QFont()
        font.setPixelSize(int(size * 0.42))
        font.setWeight(QFont.DemiBold)
        p.setFont(font)
        p.setPen(QColor(Qt.white))
        p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, profile.initials)
    p.end()
    return pix


def avatar_icon(profile: Profile, brand: Brand | None = None, size: int = 26) -> QIcon:
    return QIcon(avatar_pixmap(profile, brand, size))


# ---------------------------------------------------------------- colours from a logo
def suggest_colors(logo: bytes) -> tuple[str, str] | None:
    """(background, accent) that match a logo: the accent is its most common vivid colour; the background
    is its main dark or light colour when it has one, else a dark tint of the accent."""
    image = QImage.fromData(logo) if logo else QImage()
    if image.isNull():
        return None
    image = image.scaled(64, 64, Qt.KeepAspectRatio, Qt.FastTransformation).convertToFormat(QImage.Format_ARGB32)
    vivid, plain = Counter(), Counter()
    for y in range(image.height()):
        for x in range(image.width()):
            c = image.pixelColor(x, y)
            if c.alpha() < 128:
                continue
            key = (c.red() // 24, c.green() // 24, c.blue() // 24)
            if c.hsvSaturationF() > 0.35 and c.valueF() > 0.25:
                vivid[key] += 1
            else:
                plain[key] += 1

    def color(key) -> str:
        return theme._hex(v * 24 + 12 for v in key)

    accent = color(vivid.most_common(1)[0][0]) if vivid else None
    background = None
    if plain:
        candidate = color(plain.most_common(1)[0][0])
        lum = theme.luminance(candidate)
        if lum < 0.04 or lum > 0.8:          # clearly a dark or a light brand background
            background = candidate
    if accent is None and background is None:
        return None
    if accent is None:
        accent = theme.DEFAULT_CUSTOM[1]
    if background is None:
        background = theme.mix(theme.THEMES["dark"].bg_primary, accent, 0.08)
    return background, accent
