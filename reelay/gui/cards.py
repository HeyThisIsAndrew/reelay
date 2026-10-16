"""Media-first cards, shared by the Library grid and the Board.

Everything here is painted (no widget per card), so thousands of videos scroll smoothly.

- ``draw_thumbnail``: a 16:9 frame. Landscape covers fill it; vertical covers (Shorts, Reels, TikToks) sit
  as a 9:16 frame on a blurred, dimmed copy of themselves, so you can tell short-form from long-form at a
  glance without reading a label.
- ``draw_pill``: the small text badge laid over a thumbnail (its length).
- ``source_badge`` / ``draw_source_badge``: where a video came from, as the platform's logo on a light chip
  (YouTube Shorts has its own mark), with hover text such as "From YouTube · Short · 1080p".
- ``MediaGrid`` + ``LibraryCardDelegate``: the Library's responsive grid.
- ``HoverFader``: eases a card's hover highlight in and out (120 ms).
"""

from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate, QToolTip

from ..models import Status, VideoItem
from . import icons, theme
from .table_model import fmt_count, fmt_duration, short_date

CARD_MIN_WIDTH = 240             # the default tile size (Medium)
TILE_SIZES = {"Small": 180, "Medium": 240, "Large": 320, "XL": 400}
TILE_MIN, TILE_MAX = 180, 400
CARD_GAP = 16
CARD_TEXT_HEIGHT = 62
RADIUS = theme.RADIUS_LG

_blur_cache: dict[tuple[str, int, int], QPixmap] = {}
_pillar_cache: dict[tuple[int, int], QPixmap] = {}     # (pixmap cacheKey, height) → scaled vertical frame
CACHE_LIMIT = 800


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


def source_badge(item: VideoItem, cover: QPixmap | None = None) -> tuple[str, str]:
    """The logo for where a video came from (YouTube Shorts has its own), and its hover text:
    e.g. ("youtube_shorts", "From YouTube · Short · 1080p")."""
    from ..readiness import SHORT_FORM, video_form
    key = (item.platform or "").lower()
    short = video_form(item) == SHORT_FORM
    if key == "youtube" and short:
        key = "youtube_shorts"
    form = "Short" if short else "Long-form"
    parts = [f"From {item.platform or 'an unknown platform'}", form, resolution_label(item)]
    return key, " · ".join(p for p in parts if p)


BADGE = 24                                  # the source badge on a thumbnail: a light chip with the logo


def badge_rect(thumb: QRectF) -> QRectF:
    return QRectF(thumb.left() + 8, thumb.top() + 8, BADGE, BADGE)


def draw_source_badge(p: QPainter, thumb: QRectF, item: VideoItem, cover: QPixmap | None = None,
                      size: float = BADGE, inset: float = 8) -> QRectF:
    """Where the video came from, top left of its thumbnail: the platform's logo on a light chip (so dark
    logos read on a dark picture). Returns the chip's rect, for its hover text."""
    from .logos import platform_pixmap
    rect = QRectF(thumb.left() + inset, thumb.top() + inset, size, size)
    key, _tip = source_badge(item, cover)
    p.save()
    p.setPen(Qt.NoPen)
    chip = QColor(Qt.white)
    chip.setAlphaF(0.92)
    p.setBrush(chip)
    p.drawRoundedRect(rect, size * 0.3, size * 0.3)
    pad = max(2, round(size / 6))
    logo = rect.adjusted(pad, pad, -pad, -pad).toRect()
    p.drawPixmap(logo, platform_pixmap(key, logo.width()))
    p.restore()
    return rect


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
        if len(_blur_cache) > CACHE_LIMIT:
            _blur_cache.pop(next(iter(_blur_cache)))
    return _blur_cache[ck]


def _pillar(image: QPixmap, h: int) -> QPixmap:
    """A vertical cover scaled for a frame ``h`` tall (2x for sharpness), scaled once and reused."""
    ck = (image.cacheKey(), h)
    cached = _pillar_cache.get(ck)
    if cached is None:
        cached = image.scaledToHeight(h * 2, Qt.SmoothTransformation)
        _pillar_cache[ck] = cached
        if len(_pillar_cache) > CACHE_LIMIT:
            _pillar_cache.pop(next(iter(_pillar_cache)))
    return cached


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
        scaled = _pillar(image, h)
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
    """The card body: a card a step brighter than its well (white with a soft shadow line in light themes),
    a crisp hairline, a faint top highlight in dark themes, accent glow on hover, accent ring when selected."""
    pal = theme.P
    path = rounded(rect, RADIUS)
    dark = pal.scheme != "light"
    if not dark:
        p.fillPath(rounded(rect.translated(0, 1), RADIUS), QColor(pal.card_shadow))
    p.fillPath(path, QColor(pal.card_fill if hover < 0.5 else pal.bg_hover))
    if dark and not selected:
        glint = QColor(pal.card_highlight)
        fade = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.top() + min(28.0, rect.height() / 3)))
        fade.setColorAt(0.0, glint)
        glint.setAlphaF(0.0)
        fade.setColorAt(1.0, glint)
        p.setPen(QPen(QBrush(fade), 1))
        p.drawPath(rounded(rect.adjusted(1.5, 1.5, -1.5, -1.5), RADIUS - 1))
    if selected:
        p.setPen(QPen(QColor(pal.accent_color), 2))
        p.drawPath(rounded(rect.adjusted(1, 1, -1, -1), RADIUS - 1))
    else:
        base, glow = QColor(pal.card_edge), QColor(pal.accent_ring)
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
    """Eases each card's hover highlight in and out; call ``value(key)`` while painting.
    Each tick repaints only the cards that are animating (``rect_for(key)``), never the whole view."""

    DURATION = 0.12

    def __init__(self, viewport, rect_for=None):
        self.viewport = viewport
        self.rect_for = rect_for
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
            self._repaint(key)
        if not busy:
            self._timer.stop()

    def _repaint(self, key: str) -> None:
        rect = self.rect_for(key) if self.rect_for is not None else None
        if rect is not None and rect.isValid():
            self.viewport.update(rect.adjusted(-2, -2, 2, 2))
        elif self.rect_for is None:
            self.viewport.update()


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
        self.setViewportMargins(CARD_GAP // 2, CARD_GAP // 2, 0, 0)
        self.card_min_width = CARD_MIN_WIDTH          # the tile size control sets this
        self.card_size = QSize(CARD_MIN_WIDTH, self._card_height(CARD_MIN_WIDTH))
        self.fader = HoverFader(self.viewport(), self._card_rect)
        self.mouse = QPoint(-1, -1)
        # Resizing (dragging the panel edge, the window) relayouts once the drag pauses, not every pixel.
        self._relayout_timer = QTimer(self, singleShot=True, interval=60, timeout=self._relayout)

    @staticmethod
    def _card_height(width: int) -> int:
        return int(width * 9 / 16) + CARD_TEXT_HEIGHT

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if not self.isVisible() or self.gridSize().width() <= 0:
            self._relayout()                       # first layout: right away
        else:
            self._relayout_timer.start()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._relayout()

    def set_card_min_width(self, width: int) -> None:
        self.card_min_width = max(TILE_MIN, min(TILE_MAX, int(width)))
        self._relayout()

    def _relayout(self) -> None:
        """As many columns of at least ``card_min_width`` as fit, stretched to fill the row. A view
        narrower than one tile gets one card exactly as wide as the view (never wider)."""
        sb = 0 if self.verticalScrollBar().isVisible() else (self.verticalScrollBar().sizeHint().width() or 14) + 4
        margin = CARD_GAP // 2 + sb
        vw = max(self.viewport().width(), 1)
        min_w = self.card_min_width
        avail = max(1, vw - margin)
        columns = max(1, avail // (min_w + CARD_GAP))
        grid_w = avail // columns
        if columns == 1:
            card_w = max(1, min(vw - CARD_GAP, grid_w - CARD_GAP))
            if card_w < min_w and vw > CARD_GAP:
                card_w = max(1, vw - CARD_GAP)
        else:
            card_w = max(min_w, grid_w - CARD_GAP)
            grid_w = card_w + CARD_GAP
            if columns * grid_w > avail:
                grid_w = avail // columns
                card_w = max(1, grid_w - CARD_GAP)

        self.card_size = QSize(card_w, self._card_height(card_w))
        self.setGridSize(QSize(grid_w, self.card_size.height() + CARD_GAP))

    def _card_rect(self, key: str) -> QRect | None:
        model = self.model()
        if model is None or not key.isdigit() or int(key) >= model.rowCount():
            return None
        return QRect(self.visualRect(model.index(int(key), 0)).topLeft(), self.card_size)

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
        return self.page.model.item(self.page.pager.to_model_row(index))

    def helpEvent(self, event, view, option, index) -> bool:  # noqa: N802
        """Hovering the source badge says where the video came from and what it is."""
        if event.type() == QEvent.ToolTip and index.isValid():
            rect = QRectF(option.rect.topLeft(), QSize(self.grid.card_size)).adjusted(0.5, 0.5, -0.5, -0.5)
            thumb = QRectF(rect.left(), rect.top(), rect.width(), rect.width() * 9 / 16)
            if badge_rect(thumb).contains(QPointF(event.pos())):
                QToolTip.showText(event.globalPos(), source_badge(self.item_at(index))[1], view)
                return True
        return super().helpEvent(event, view, option, index)

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

        draw_source_badge(p, thumb, item, cover)
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
