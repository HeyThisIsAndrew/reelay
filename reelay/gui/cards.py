"""Media-first cards, shared by the Library grid and the Board.

Everything here is painted (no widget per card), so thousands of videos scroll smoothly.

- ``draw_thumbnail``: a 16:9 frame. Landscape covers fill it; vertical covers (Shorts, Reels, TikToks) sit
  as a 9:16 frame on a blurred, dimmed copy of themselves, so you can tell short-form from long-form at a
  glance without reading a label.
- ``draw_pill``: the small badges laid over a thumbnail (duration, format).
- ``media_label``: "Short · 1080p", "Long-form · 4K"…
- ``MediaGrid`` + ``LibraryCardDelegate``: the Library's responsive grid.
- ``HoverFader``: eases a card's hover highlight in and out (120 ms).
"""

from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate

from ..models import Status, VideoItem
from . import icons, theme
from .table_model import fmt_count, fmt_duration, short_date

from ..readiness import SHORT_FORM_MAX_SECONDS as SHORT_MAX_SECONDS   # Shorts top out at 3 minutes
CARD_MIN_WIDTH = 236
CARD_GAP = 16
CARD_TEXT_HEIGHT = 62
RADIUS = theme.RADIUS_LG

_blur_cache: dict[tuple[str, int, int], QPixmap] = {}


# ---------------------------------------------------------------- labels
def resolution_label(item: VideoItem) -> str:
    if not item.width or not item.height:
        return ""
    short_side = min(item.width, item.height)
    for px, label in ((2160, "4K"), (1440, "1440p"), (1080, "1080p"), (720, "720p")):
        if short_side >= px * 0.95:
            return label
    return f"{short_side}p"


def is_vertical(item: VideoItem, cover: QPixmap | None = None) -> bool:
    if item.width and item.height:
        return item.height > item.width
    if cover is not None and not cover.isNull():
        return cover.height() > cover.width()
    return item.platform.lower() in ("tiktok", "instagram")


def media_label(item: VideoItem, cover: QPixmap | None = None) -> str:
    """What kind of video this is, for the badge on its thumbnail."""
    seconds = float(item.duration or 0)
    if is_vertical(item, cover):
        kind = "Short" if not seconds or seconds <= SHORT_MAX_SECONDS else "Vertical"
    else:
        kind = "Long-form" if seconds > SHORT_MAX_SECONDS else "Video"
    res = resolution_label(item)
    return f"{kind} · {res}" if res else kind


# ---------------------------------------------------------------- painting primitives
def rounded(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


def _blurred(key: str, cover: QPixmap, w: int, h: int) -> QPixmap:
    """A soft, dimmed backdrop made from the cover itself (shrink, then smooth-scale back up)."""
    ck = (key, w, h)
    if ck not in _blur_cache:
        tiny = cover.scaled(14, 14, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        big = tiny.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        _blur_cache[ck] = big.copy((big.width() - w) // 2, (big.height() - h) // 2, w, h)
        if len(_blur_cache) > 800:
            _blur_cache.pop(next(iter(_blur_cache)))
    return _blur_cache[ck]


def draw_thumbnail(p: QPainter, rect: QRectF, item: VideoItem, cover: QPixmap | None, clip: QPainterPath,
                   frame: QPixmap | None = None) -> None:
    """Fill ``rect`` (clipped to ``clip``) with the video's cover, or a hover-scrub ``frame``."""
    pal = theme.P
    p.save()
    p.setClipPath(clip)
    image = frame if frame is not None and not frame.isNull() else cover
    if image is None or image.isNull():
        p.fillRect(rect, QColor(pal.bg_pressed))
        from .logos import platform_pixmap
        logo = platform_pixmap(item.platform.lower(), 26, faded=True)
        p.setOpacity(0.5)
        p.drawPixmap(QPointF(rect.center().x() - 13, rect.center().y() - 13), logo)
        p.restore()
        return
    w, h = int(rect.width()), int(rect.height())
    if image.height() > image.width() * 1.05:                 # vertical: pillar on a blurred backdrop
        p.drawPixmap(rect.toRect(), _blurred(item.key, image, w, h))
        dim = QColor(pal.scrim)
        dim.setAlphaF(0.38)
        p.fillRect(rect, dim)
        scaled = image.scaledToHeight(h * 2, Qt.SmoothTransformation)
        pw = scaled.width() / 2
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawPixmap(QRectF(rect.center().x() - pw / 2, rect.top(), pw, h), scaled, QRectF(scaled.rect()))
    else:                                                      # landscape: fill and crop
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        src = QRectF(image.rect())
        target_ratio = rect.width() / max(rect.height(), 1)
        if src.width() / max(src.height(), 1) > target_ratio:
            cw = src.height() * target_ratio
            src = QRectF(src.x() + (src.width() - cw) / 2, src.y(), cw, src.height())
        else:
            ch = src.width() / target_ratio
            src = QRectF(src.x(), src.y() + (src.height() - ch) / 2, src.width(), ch)
        p.drawPixmap(rect, image, src)
    p.restore()


def draw_pill(p: QPainter, text: str, anchor: QPointF, align: Qt.AlignmentFlag, font: QFont) -> QRectF:
    """A small translucent badge with white text; ``anchor`` is the corner it hugs."""
    fm = QFontMetrics(font)
    w, h = fm.horizontalAdvance(text) + 12, fm.height() + 4
    x = anchor.x() - w if align & Qt.AlignRight else anchor.x()
    y = anchor.y() - h if align & Qt.AlignBottom else anchor.y()
    r = QRectF(x, y, w, h)
    bg = QColor(theme.P.scrim)
    bg.setAlphaF(0.62)
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    p.fillPath(rounded(r, 5), bg)
    p.setFont(font)
    p.setPen(QColor(Qt.white))
    p.drawText(r, Qt.AlignCenter, text)
    p.restore()
    return r


def draw_frame(p: QPainter, rect: QRectF, hover: float, selected: bool) -> QPainterPath:
    """The card body: surface, hairline, accent glow on hover, accent ring when selected."""
    pal = theme.P
    path = rounded(rect, RADIUS)
    p.fillPath(path, QColor(pal.bg_raised if hover < 0.5 else pal.bg_hover))
    if selected:
        p.setPen(QPen(QColor(pal.accent_color), 2))
        p.drawPath(rounded(rect.adjusted(1, 1, -1, -1), RADIUS - 1))
    else:
        base, glow = QColor(pal.divider), QColor(pal.accent_ring)
        mixed = QColor.fromRgbF(base.redF() + (glow.redF() - base.redF()) * hover,
                                base.greenF() + (glow.greenF() - base.greenF()) * hover,
                                base.blueF() + (glow.blueF() - base.blueF()) * hover)
        p.setPen(QPen(mixed, 1))
        p.drawPath(rounded(rect.adjusted(0.5, 0.5, -0.5, -0.5), RADIUS))
    return path


def small_font(base: QFont, delta: int = -2, bold: bool = False) -> QFont:
    f = QFont(base)
    f.setPointSize(max(base.pointSize() + delta, 8))
    if bold:
        f.setWeight(QFont.DemiBold)
    return f


# ---------------------------------------------------------------- hover easing
class HoverFader:
    """Eases each card's hover highlight in and out; call ``value(key)`` while painting."""

    DURATION = 0.12

    def __init__(self, viewport):
        self.viewport = viewport
        self.target: str | None = None
        self._t: dict[str, float] = {}
        self._last = time.monotonic()
        self._timer = QTimer(viewport, interval=16, timeout=self._tick)

    def hover(self, key: str | None) -> None:
        if key != self.target:
            self.target = key
            if key is not None:
                self._t.setdefault(key, 0.0)
            self._last = time.monotonic()
            self._timer.start()

    def value(self, key: str) -> float:
        return self._t.get(key, 0.0)

    def _tick(self) -> None:
        from .inspector import ANIMATION_MS
        now = time.monotonic()
        step = 1.0 if ANIMATION_MS <= 0 else (now - self._last) / self.DURATION
        self._last = now
        busy = False
        for key in list(self._t):
            goal = 1.0 if key == self.target else 0.0
            cur = self._t[key]
            cur = min(goal, cur + step) if goal > cur else max(goal, cur - step)
            if cur == 0.0 and key != self.target:
                del self._t[key]
            else:
                self._t[key] = cur
                busy = busy or cur != goal
        self.viewport.update()
        if not busy:
            self._timer.stop()


# ---------------------------------------------------------------- the Library grid
class MediaGrid(QListView):
    """A responsive grid of cards: as many columns as fit, cards stretching to fill the row."""

    checkToggled = Signal(object)        # proxy index whose download tick was clicked
    hoverMoved = Signal(object, float)   # proxy index (or None), fraction across the thumbnail

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("MediaGrid")
        self.setViewMode(QListView.IconMode)
        self.setMovement(QListView.Static)
        self.setResizeMode(QListView.Adjust)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(24)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WA_Hover)
        self.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.setSpacing(0)
        self.card_size = QSize(CARD_MIN_WIDTH, self._card_height(CARD_MIN_WIDTH))
        self.fader = HoverFader(self.viewport())
        self.mouse = QPoint(-1, -1)

    @staticmethod
    def _card_height(width: int) -> int:
        return int(width * 9 / 16) + CARD_TEXT_HEIGHT

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._relayout()
        super().resizeEvent(event)

    def _relayout(self) -> None:
        width = self.viewport().width() - CARD_GAP
        columns = max(1, (width + CARD_GAP) // (CARD_MIN_WIDTH + CARD_GAP))
        card_w = max(CARD_MIN_WIDTH, (width - CARD_GAP * (columns - 1)) // columns - 1)
        self.card_size = QSize(card_w, self._card_height(card_w))
        self.setGridSize(QSize(card_w + CARD_GAP, self.card_size.height() + CARD_GAP))
        self.setViewportMargins(CARD_GAP // 2, CARD_GAP // 2, 0, 0)

    def thumb_rect(self, index) -> QRect:
        r = self.visualRect(index)
        cell = QRect(r.topLeft(), self.card_size)
        return QRect(cell.left(), cell.top(), cell.width(), int(cell.width() * 9 / 16))

    def check_rect(self, index) -> QRect:
        t = self.thumb_rect(index)
        return QRect(t.right() - 30, t.top() + 8, 22, 22)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self.mouse = event.position().toPoint()
        index = self.indexAt(self.mouse)
        self.fader.hover(f"{index.row()}" if index.isValid() else None)
        if index.isValid():
            self.viewport().update(self.visualRect(index))      # hover-scrub follows the mouse
            t = self.thumb_rect(index)
            frac = (self.mouse.x() - t.left()) / max(t.width(), 1) if t.contains(self.mouse) else -1.0
            self.hoverMoved.emit(index, frac)
        else:
            self.hoverMoved.emit(None, -1.0)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.mouse = QPoint(-1, -1)
        self.fader.hover(None)
        self.hoverMoved.emit(None, -1.0)
        super().leaveEvent(event)

    def viewportEvent(self, event) -> bool:  # noqa: N802
        if event.type() == QEvent.HoverLeave:
            self.fader.hover(None)
        return super().viewportEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and event.button() == Qt.LeftButton \
                and self.check_rect(index).contains(event.position().toPoint()):
            self.checkToggled.emit(index)      # ticking for download never changes the selection
            event.accept()
            return
        super().mousePressEvent(event)


class LibraryCardDelegate(QStyledItemDelegate):
    """Paints one Library video: thumbnail with badges and a download tick, then title, details and status."""

    def __init__(self, page, grid: MediaGrid):
        super().__init__(grid)
        self.page, self.grid = page, grid
        self.scrub = None              # set to a ScrubCache for hover previews
        self._sizes: dict[str, int] = {}

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return self.grid.card_size

    def item_at(self, index) -> VideoItem:
        return self.page.model.item(self.page.proxy.mapToSource(index).row())

    def _file_size(self, item: VideoItem) -> int:
        if not item.is_downloaded:
            return 0
        if item.key not in self._sizes:
            path = item.path(self.page.folder)
            try:
                self._sizes[item.key] = path.stat().st_size if path else 0
            except OSError:
                self._sizes[item.key] = 0
        return self._sizes[item.key]

    def forget_size(self, key: str) -> None:
        self._sizes.pop(key, None)

    def paint(self, p: QPainter, option, index) -> None:
        pal = theme.P
        item = self.item_at(index)
        cell = QRectF(option.rect.topLeft(), QSize(self.grid.card_size))
        rect = cell.adjusted(0.5, 0.5, -0.5, -0.5)
        selected = bool(option.state & QStyle.State_Selected)
        hover = self.grid.fader.value(f"{index.row()}")
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        clip = draw_frame(p, rect, hover, selected)

        thumb = QRectF(rect.left(), rect.top(), rect.width(), rect.width() * 9 / 16)
        cover = self.page.thumbs.cover(item)
        frame = None
        if self.scrub is not None and hover > 0.5 and item.is_downloaded:
            frac = self.grid.mouse.x() - thumb.left()
            if thumb.contains(QPointF(self.grid.mouse)):
                frame = self.scrub.frame(item, self.page.folder, frac / max(thumb.width(), 1))
        top_clip = QPainterPath()
        top_clip.addRect(thumb)
        draw_thumbnail(p, thumb, item, cover, clip.intersected(top_clip), frame)
        if hover > 0 and not selected:
            wash = QColor(Qt.white)
            wash.setAlphaF(0.05 * hover)
            p.fillRect(thumb, wash)

        badge_font = small_font(option.font, -3, bold=True)
        draw_pill(p, media_label(item, cover), QPointF(thumb.left() + 8, thumb.top() + 8),
                  Qt.AlignLeft | Qt.AlignTop, badge_font)
        if item.duration:
            draw_pill(p, fmt_duration(item.duration), QPointF(thumb.right() - 8, thumb.bottom() - 8),
                      Qt.AlignRight | Qt.AlignBottom, small_font(option.font, -2, bold=True))

        # download tick (top right): shown on hover, when ticked, or done
        check = QRectF(self.grid.check_rect(index))
        if item.is_downloaded:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal.success))
            p.drawEllipse(check.adjusted(3, 3, -3, -3))
            p.drawPixmap(check.adjusted(6, 6, -6, -6).toRect(), icons.pixmap("check", pal.on_badge, 10))
        elif item.checked or hover > 0.05 or selected:
            p.setOpacity(1.0 if item.checked else max(hover, 0.85 if selected else 0))
            if item.checked:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(pal.accent_color))
                p.drawEllipse(check)
                p.drawPixmap(check.adjusted(5, 5, -5, -5).toRect(), icons.pixmap("check", pal.on_accent, 12))
            else:
                ring = QColor(pal.scrim)
                ring.setAlphaF(0.45)
                p.setBrush(ring)
                p.setPen(QPen(QColor(Qt.white), 1.6))
                p.drawEllipse(check.adjusted(1, 1, -1, -1))
            p.setOpacity(1.0)

        # download progress along the thumbnail's bottom edge
        if item.status in (Status.DOWNLOADING, Status.QUEUED):
            bar = QRectF(thumb.left(), thumb.bottom() - 3, thumb.width(), 3)
            back = QColor(pal.scrim)
            back.setAlphaF(0.5)
            p.fillRect(bar, back)
            p.fillRect(QRectF(bar.left(), bar.top(), bar.width() * (item.progress or 0) / 100, 3),
                       QColor(pal.accent_color))

        # text: title, then details and status
        text = QRectF(rect.left() + 12, thumb.bottom() + 9, rect.width() - 24, CARD_TEXT_HEIGHT - 14)
        title_font = small_font(option.font, 0, bold=True)
        p.setFont(title_font)
        p.setPen(QColor(pal.text_primary))
        name = item.custom_name or item.hook
        p.drawText(QRectF(text.left(), text.top(), text.width(), 20), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(title_font).elidedText(name, Qt.ElideRight, int(text.width())))
        status, color = self._status(item)
        meta_font = small_font(option.font, -2)
        fm = QFontMetrics(meta_font)
        p.setFont(meta_font)
        status_w = fm.horizontalAdvance(status) + (14 if status else 0)
        bits = [short_date(item.upload_date), f"{fmt_count(item.view_count)} views" if item.view_count else ""]
        size = self._file_size(item)
        if size:
            bits.append(f"{size / 1e9:.1f} GB" if size >= 1e9 else f"{size / 1e6:.0f} MB")
        meta = " · ".join(b for b in bits if b)
        line = QRectF(text.left(), text.top() + 22, text.width(), 18)
        p.setPen(QColor(pal.text_muted))
        p.drawText(QRectF(line.left(), line.top(), line.width() - status_w - 6, line.height()),
                   Qt.AlignLeft | Qt.AlignVCenter, fm.elidedText(meta, Qt.ElideRight, int(line.width() - status_w)))
        if status:
            dot = QPointF(line.right() - status_w + 4, line.center().y())
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(color))
            p.drawEllipse(dot, 3, 3)
            p.setPen(QColor(color))
            p.drawText(QRectF(dot.x() + 7, line.top(), status_w, line.height()), Qt.AlignLeft | Qt.AlignVCenter,
                       status)
        p.restore()

    @staticmethod
    def _status(item: VideoItem) -> tuple[str, str]:
        pal = theme.P
        if item.status == Status.DOWNLOADING:
            return f"{(item.progress or 0):.0f}%", pal.accent_color
        if item.status == Status.QUEUED:
            return "Waiting", pal.text_secondary
        if item.status == Status.FAILED:
            return "Failed", pal.danger
        if item.is_downloaded:
            return "Downloaded", pal.success
        if item.checked:
            return "Selected", pal.accent_color
        return "", pal.text_muted
