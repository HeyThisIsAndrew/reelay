"""Board page: a Kanban pipeline (Backlog → Staging → Scheduled → Published)
with drag-and-drop, a post editor, YouTube uploads and status sync."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import (QAbstractListModel, QItemSelectionModel, QMimeData, QModelIndex, QPointF, QRect, QRectF,
                            QEvent, QSettings, QSize, Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QLabel, QListView, QMenu,
                               QMessageBox, QPushButton, QScrollArea, QSplitter, QStackedWidget, QStyle,
                               QStyledItemDelegate, QToolButton, QVBoxLayout, QWidget, QLineEdit)

from ..models import Platform, Post, PostStatus, Profile, Stage
from ..coverage import needs_work as coverage_needs_work, short_summary
from ..pipeline import Card, Pipeline, PipelineError
from ..store import Change
from ..text import clock, local_text
from ..uploads import UploadManager
from . import theme
from .theme import set_prop
from ..columns import NAME_MAX, SUBTITLE_MAX, ColumnError, column_for, default_columns, is_custom
from ..columns import add as add_column
from ..columns import load as load_columns
from ..columns import remove as remove_column
from ..columns import save as save_columns
from .board_tools import ArchivedDialog, BulkPanel, ImportDialog
from .bridge import change_bridge
from . import icons
from .cards import (BADGE, HoverFader, draw_frame, draw_pill, draw_source_badge, draw_thumbnail, small_font,
                    source_badge)
from .chrome import FilterButton, Popover, icon_button
from .inspector import ContextualSplitter, Inspector, MediaPane
from .player import PlayerWidget
from .post_panel import PostPanel
from .table_model import fmt_count, fmt_duration, short_date
from .thumbnails import ThumbnailCache
from .tips import tip
from .workers import CallWorker
from .widgets import search_field

log = logging.getLogger("reelay")

MIME = "application/x-reelay-cards"
CARD_HEIGHT = 132
COVER_W, COVER_H = 52, 92
SYNC_EVERY_SECONDS = 600
COLUMN_MIN_WIDTH = 280      # columns never squeeze below this; drag the gaps between them to resize
CARD_LOGO_SIZE = 18


# ====================================================================== model / delegate
class BoardModel(QAbstractListModel):
    moveRequested = Signal(list, str, int)   # card ids, target stage, insert row

    def __init__(self, stage: str, parent=None):
        super().__init__(parent)
        self.stage = stage
        self.cards: list[Card] = []

    def set_cards(self, cards: list[Card]) -> None:
        self.beginResetModel()
        self.cards = cards
        self.endResetModel()

    def refresh_card(self, card_id: int) -> None:
        for row, card in enumerate(self.cards):
            if card.id == card_id:
                idx = self.index(row)
                self.dataChanged.emit(idx, idx)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.cards)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        card = self.cards[index.row()]
        if role == Qt.UserRole:
            return card
        if role == Qt.DisplayRole:
            return card.title
        if role == Qt.ToolTipRole:
            from ..coverage import NOT_APPLICABLE
            marks = {"published": "✓", "scheduled": "🕒", "ready": "●", "needs_work": "!"}
            lines = [f"{marks[c.state]}  {c.text}" for c in card.coverage.values() if c.state != NOT_APPLICABLE]
            return card.row.item.hook + ("\n\n" + "\n".join(lines) if lines else
                                         "\n\nNot planned for any platform yet")
        return None

    def flags(self, index):
        base = Qt.ItemIsDropEnabled
        if not index.isValid():
            return base
        return base | Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled

    def supportedDropActions(self):
        return Qt.MoveAction

    def supportedDragActions(self):
        return Qt.MoveAction

    def mimeTypes(self):
        return [MIME]

    def mimeData(self, indexes):
        data = QMimeData()
        data.setData(MIME, json.dumps([self.cards[i.row()].id for i in indexes if i.isValid()]).encode())
        return data

    def canDropMimeData(self, data, action, row, column, parent):
        return data.hasFormat(MIME)

    def dropMimeData(self, data, action, row, column, parent):
        if not data.hasFormat(MIME):
            return False
        ids = json.loads(bytes(data.data(MIME)).decode())
        insert = row if row >= 0 else (parent.row() if parent.isValid() else len(self.cards))
        # Defer so the drag finishes before the board reloads its models.
        QTimer.singleShot(0, lambda: self.moveRequested.emit(ids, self.stage, insert))
        return False  # the page reloads everything; Qt must not remove source rows


def card_notes(card: Card) -> list[str]:
    """Plain-language notes for a card's marks (its tooltip)."""
    flags = getattr(card, "flags", {})
    notes = []
    if flags.get("automation"):
        notes.append("Automations ran on this video: " + ", ".join(flags["automation"]))
    if flags.get("approval"):
        notes.append("An AI assistant asked to change this video; it's waiting for your approval")
    if flags.get("ai"):
        notes.append("The caption was last edited by an AI assistant")
    return notes


def _two_lines(fm: QFontMetrics, text: str, width: int) -> list[str]:
    words, line1 = text.split(), ""
    while words and fm.horizontalAdvance((line1 + " " + words[0]).strip()) <= width:
        line1 = (line1 + " " + words.pop(0)).strip()
    if not line1 and words:  # one very long word
        return [fm.elidedText(text, Qt.ElideRight, width)]
    rest = " ".join(words)
    return [line1] + ([fm.elidedText(rest, Qt.ElideRight, width)] if rest else [])


class CardDelegate(QStyledItemDelegate):
    """A Board card. "Large" (the default) leads with a 16:9 thumbnail; "Compact" is the dense strip."""

    density = "large"                 # set by the Board from Filter → Cards

    def __init__(self, thumbs: ThumbnailCache, parent=None):
        super().__init__(parent)
        self.thumbs = thumbs
        self.fader = HoverFader(parent.viewport(), self._card_rect) if parent is not None else None
        if parent is not None:
            parent.viewport().installEventFilter(self)
            self._view = parent

    def _card_rect(self, key: str):
        """Where card ``key`` (its id) is on screen, so hover easing repaints just that card."""
        model = self._view.model()
        for row, card in enumerate(getattr(model, "cards", [])):
            if str(card.id) == key:
                return self._view.visualRect(model.index(row, 0))
        return None

    # hover easing: follow the mouse over the list
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if self.fader is not None:
            if event.type() == QEvent.MouseMove:
                index = self._view.indexAt(event.position().toPoint())
                self.fader.hover(str(index.data(Qt.UserRole).id) if index.isValid() else None)
            elif event.type() in (QEvent.Leave, QEvent.HoverLeave):
                self.fader.hover(None)
        return False

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        width = option.widget.viewport().width() if option.widget is not None else option.rect.width()
        if self.density == "compact":
            return QSize(width, CARD_HEIGHT)
        tf = QFontMetrics(small_font(option.font, 0, bold=True)).height()
        sf = QFontMetrics(small_font(option.font, -2)).height()
        text = 10 + 2 * tf + 4 + sf + 8 + CARD_LOGO_SIZE + 8 + sf + 12
        return QSize(width, int((width - 4) * 9 / 16) + text + 10)

    def _badge_rect(self, option) -> QRectF:
        """Where paint() puts the source badge."""
        if self.density == "compact":
            r = QRectF(option.rect).adjusted(2, 4, -2, -4)
            return QRectF(r.x() + 14, r.y() + (r.height() - COVER_H) / 2 + 4, 18, 18)
        r = QRectF(option.rect).adjusted(2, 5, -2, -5)
        return QRectF(r.left() + 8, r.top() + 8, BADGE, BADGE)

    def helpEvent(self, event, view, option, index) -> bool:  # noqa: N802
        card: Card = index.data(Qt.UserRole)
        if event.type() == QEvent.ToolTip:
            from PySide6.QtWidgets import QToolTip
            if self._badge_rect(option).contains(QPointF(event.pos())):      # where the video came from
                QToolTip.showText(event.globalPos(), source_badge(card.row.item)[1], view)
                return True
            notes = card_notes(card)
            if notes:
                QToolTip.showText(event.globalPos(), "\n".join(notes), view)
                return True
        return super().helpEvent(event, view, option, index)

    def paint(self, painter: QPainter, option, index) -> None:
        if self.density == "compact":
            return self._paint_compact(painter, option, index)
        card: Card = index.data(Qt.UserRole)
        p = theme.P
        item = card.row.item
        r = QRectF(option.rect).adjusted(2, 5, -2, -5)
        selected = bool(option.state & QStyle.State_Selected)
        hover = self.fader.value(str(card.id)) if self.fader is not None else 0.0
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        frame = draw_frame(painter, r, hover, selected)
        thumb = QRectF(r.left(), r.top(), r.width(), r.width() * 9 / 16)
        top = QPainterPath()
        top.addRect(thumb)
        cover = self.thumbs.cover(item)
        draw_thumbnail(painter, thumb, item, cover, frame.intersected(top))
        draw_source_badge(painter, thumb, item, cover)
        if item.duration:
            draw_pill(painter, fmt_duration(item.duration), QPointF(thumb.right() - 8, thumb.bottom() - 8),
                      Qt.AlignRight | Qt.AlignBottom, small_font(option.font, -2, bold=True))
        self._draw_flags(painter, card, QPointF(thumb.right() - 8, thumb.top() + 8))
        if card.uploading and card.progress is not None and not card.queued:
            bar = QRectF(thumb.left(), thumb.bottom() - 3, thumb.width(), 3)
            back = QColor(p.scrim)
            back.setAlphaF(0.5)
            painter.fillRect(bar, back)
            painter.fillRect(QRectF(bar.x(), bar.y(), bar.width() * card.progress / 100, 3), QColor(p.accent_color))

        x, w = int(r.left() + 12), int(r.width() - 24)
        y = int(thumb.bottom() + 10)
        title_font = small_font(option.font, 0, bold=True)
        painter.setFont(title_font)
        painter.setPen(QColor(p.text_primary))
        fm = QFontMetrics(title_font)
        for line in _two_lines(fm, card.title, w):
            painter.drawText(QRect(x, y, w, fm.height()), Qt.AlignLeft | Qt.AlignVCenter, line)
            y += fm.height()
        y = int(thumb.bottom() + 10 + fm.height() * 2 + 4)
        small = small_font(option.font, -2)
        painter.setFont(small)
        sfm = QFontMetrics(small)
        meta = " · ".join(b for b in (short_date(item.upload_date),
                                      f"{fmt_count(item.view_count)} views" if item.view_count else "") if b)
        painter.setPen(QColor(p.text_muted))
        painter.drawText(QRect(x, y, w, sfm.height()), Qt.AlignLeft | Qt.AlignVCenter,
                         sfm.elidedText(meta, Qt.ElideRight, w))
        y += sfm.height() + 8
        self._draw_platforms(painter, card, x + 1, y, width=w - 4)
        y += CARD_LOGO_SIZE + 8
        status, color = self._status(card)
        painter.setFont(small)
        painter.setPen(QColor(color))
        painter.drawText(QRect(x, y, w, sfm.height()), Qt.AlignLeft | Qt.AlignVCenter,
                         sfm.elidedText(status, Qt.ElideRight, w))
        painter.restore()

    @staticmethod
    def _draw_flags(painter: QPainter, card: Card, anchor: QPointF) -> None:
        """Small marks over the thumbnail: an Automation acted, a request awaits approval, AI wrote the copy."""
        flags = getattr(card, "flags", {})
        marks = [name for name, on in (("bolt", flags.get("automation")), ("hourglass", flags.get("approval")),
                                       ("sparkles", flags.get("ai"))) if on]
        x = anchor.x()
        for name in reversed(marks):
            d = 22.0
            rect = QRectF(x - d, anchor.y(), d, d)
            bg = QColor(theme.P.scrim)
            bg.setAlphaF(0.62)
            painter.setPen(Qt.NoPen)
            painter.setBrush(bg)
            painter.drawEllipse(rect)
            color = {"hourglass": theme.P.warning, "bolt": QColor(Qt.white).name()}.get(name, theme.P.accent_ring)
            painter.drawPixmap(rect.adjusted(4, 4, -4, -4).toRect(), icons.pixmap(name, color, 14))
            x -= d + 4

    def _paint_compact(self, painter: QPainter, option, index) -> None:
        card: Card = index.data(Qt.UserRole)
        p = theme.P
        r = QRectF(option.rect).adjusted(2, 4, -2, -4)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(r, 9, 9)
        painter.fillPath(path, QColor(p.bg_hover if (selected or hovered) else p.bg_raised))
        painter.setPen(QPen(QColor(p.text_muted if selected else p.border), 1))
        painter.drawPath(path)

        # cover
        cover = QRectF(r.x() + 10, r.y() + (r.height() - COVER_H) / 2, COVER_W, COVER_H)
        cpath = QPainterPath()
        cpath.addRoundedRect(cover, 6, 6)
        painter.save()
        painter.setClipPath(cpath)
        widget = option.widget
        ratio = widget.devicePixelRatioF() if widget else 1.0
        pix = self.thumbs.thumb(card.row.item, COVER_W, COVER_H, ratio)
        if pix is not None:
            painter.drawPixmap(cover.topLeft(), pix)
        else:
            painter.fillPath(cpath, QColor(p.bg_pressed))
        painter.restore()
        draw_source_badge(painter, cover, card.row.item, None, size=18, inset=4)

        # text
        x = int(cover.right() + 12)
        w = int(r.right() - 10 - x)
        y = int(r.y() + 12)
        title_font = QFont(option.font)
        title_font.setWeight(QFont.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(p.text_primary))
        fm = QFontMetrics(title_font)
        for line in _two_lines(fm, card.title, w):
            painter.drawText(QRect(x, y, w, fm.height()), Qt.AlignLeft | Qt.AlignVCenter, line)
            y += fm.height()

        small = QFont(option.font)
        small.setPointSize(max(option.font.pointSize() - 2, 8))
        painter.setFont(small)
        sfm = QFontMetrics(small)
        item = card.row.item
        meta = " · ".join(b for b in (short_date(item.upload_date), fmt_duration(item.duration))
                          if b and b != "—")
        painter.setPen(QColor(p.text_secondary))
        painter.drawText(QRect(x, y + 4, w, sfm.height()), Qt.AlignLeft | Qt.AlignVCenter,
                         sfm.elidedText(meta, Qt.ElideRight, w))

        self._draw_platforms(painter, card, x + 2, int(r.bottom()) - sfm.height() - 34, width=w - 6)

        status, color = self._status(card)
        painter.setPen(QColor(color))
        painter.drawText(QRect(x, int(r.bottom()) - sfm.height() - 10, w, sfm.height()),
                         Qt.AlignLeft | Qt.AlignVCenter, sfm.elidedText(status, Qt.ElideRight, w))
        if card.uploading and card.progress is not None and not card.queued:
            bar = QRectF(x, r.bottom() - 7, w, 3)
            painter.fillRect(bar, QColor(p.bg_pressed))
            painter.fillRect(QRectF(bar.x(), bar.y(), bar.width() * card.progress / 100, 3), QColor(p.accent_color))
        painter.restore()

    @staticmethod
    def _draw_platforms(painter: QPainter, card: Card, x: int, y: int, size: int = CARD_LOGO_SIZE,
                        width: int | None = None) -> None:
        """The coverage map: every platform in a fixed order, so the eye learns the
        positions. Published = green ✓, scheduled = blue clock, ready = a plain neutral dot (the normal
        state), needs work = red !, not going there = a faint logo with no badge. Glyphs use an ink that
        reads at 3:1 or more on their badge."""
        from ..coverage import NEEDS_WORK, NOT_APPLICABLE, PUBLISHED, READY, SCHEDULED
        from .logos import platform_pixmap
        p = theme.P
        cov = card.coverage
        gap = 8
        if width:
            size = max(12, min(size, (width - gap * (len(cov) - 1)) // max(len(cov), 1)))
        for name, c in cov.items():
            painter.save()
            if c.state == NOT_APPLICABLE:
                painter.setOpacity(0.22)
            painter.drawPixmap(x, y, platform_pixmap(name, size))
            painter.restore()
            color = {PUBLISHED: p.success, SCHEDULED: p.info, READY: p.text_secondary,
                     NEEDS_WORK: p.danger}.get(c.state)
            if color:
                d = 11.0
                badge = QRectF(x + size - d + 3, y + size - d + 3, d, d)
                painter.setPen(QPen(QColor(p.bg_raised), 1.5))
                painter.setBrush(QColor(color))
                painter.drawEllipse(badge)
                ink = QPen(QColor(p.ink_on(color)), 1.4)   # glyphs on the coloured badges
                ink.setCapStyle(Qt.RoundCap)
                painter.setPen(ink)
                cx, cy = badge.center().x(), badge.center().y()
                if c.state == PUBLISHED:
                    painter.drawPolyline([QPointF(cx - 2.6, cy + 0.1), QPointF(cx - 0.7, cy + 2.0),
                                          QPointF(cx + 2.7, cy - 2.0)])
                elif c.state == SCHEDULED:
                    painter.drawLine(QPointF(cx, cy), QPointF(cx, cy - 2.8))
                    painter.drawLine(QPointF(cx, cy), QPointF(cx + 2.2, cy))
                elif c.state == NEEDS_WORK:
                    painter.drawLine(QPointF(cx, cy - 2.8), QPointF(cx, cy + 0.6))
                    painter.drawPoint(QPointF(cx, cy + 2.6))
            x += size + gap

    @staticmethod
    def _status(card: Card) -> tuple[str, str]:
        p, post, item = theme.P, card.post, card.row.item
        held = [d for d in card.dispatches if d.status == "interrupted"]
        if held:                                    # waiting for your decision: amber
            names = ", ".join(Platform.LABELS.get(d.platform, d.platform) for d in held)
            return f"⚠ Interrupted on {names}: open to decide", p.warning_text
        asked = getattr(card, "pending_approvals", [])
        if asked:                                   # an agent's request: also waiting for you
            return f"⏳ Waiting for your approval: {', '.join(asked)}", p.warning_text
        if card.search_hit is not None and card.search_hit.spoken:   # found by what was said in the video
            said = card.search_hit.snippet.replace("«", "").replace("»", "")
            return f"🗣 “{said}”", p.text_primary
        if len(card.dispatches) > 1 and not (post and post.status == PostStatus.UPLOADING):
            marks = {"pending": "⏱", "dispatching": "…", "scheduled": "⏱", "published": "✓", "failed": "⚠"}
            short = {"youtube": "YT", "bluesky": "Bsky", "threads": "Thr", "facebook": "FB"}
            text = "  ".join(f"{short.get(d.platform, d.platform)} {marks.get(d.status, '?')}" for d in card.dispatches)
            failed = any(d.status == "failed" for d in card.dispatches)
            live = post and post.status == PostStatus.PUBLISHED
            waiting = any(d.status in ("pending", "scheduled") for d in card.dispatches)
            return text, p.danger if failed else (p.success if live else (p.info_text if waiting else p.text_primary))
        if post and post.status == PostStatus.UPLOADING:
            return ("Waiting to upload…" if card.queued else f"Uploading {(card.progress or 0):.0f}%"), p.text_primary
        if post and post.status == PostStatus.SCHEDULED:
            if post.error:
                return "⚠ Still private on YouTube", p.danger
            return f"Goes live {local_text(post.publish_at)}", p.info_text
        if post and post.status == PostStatus.PUBLISHED:
            return f"Live on {card.account.display_name}" if card.account else "Live on YouTube", p.success
        going = [s for s in card.platform_states.values() if s.target]
        marked = [s for s in going if s.state != "none"]
        if marked and not card.dispatches:
            live = sum(1 for s in going if s.state == "published")
            if live == len(going):
                return f"✓ Live on {'all ' + str(live) if live > 1 else Platform.LABELS[going[0].platform]}", p.success
            scheduled = any(s.state in ("scheduled", "sending") for s in going)
            return short_summary(card.coverage), (p.danger if coverage_needs_work(card.coverage) else
                                                  p.info_text if scheduled else p.text_secondary)
        # One post on its way (a single platform): when it goes live. Two or more are listed above.
        on_the_way = [d for d in card.dispatches if d.status in ("pending", "scheduled", "dispatching")]
        if on_the_way:
            when = min((d.publish_at or d.due_at) for d in on_the_way)
            return f"Goes live {local_text(when)}", p.info_text
        if card.row.stage == Stage.PUBLISHED:
            return "Posted outside Reelay", p.text_secondary
        if post and post.status == PostStatus.FAILED:
            return f"⚠ {post.error}", p.danger
        if not item.is_downloaded:
            return "⚠ Video file missing", p.danger
        readiness = card.readiness
        if readiness is not None and readiness.blocking:
            names = ", ".join(Platform.LABELS.get(n, n) for n in readiness.blocking)
            # Ready somewhere else, or nowhere yet: the same words either way, in sentence case
            return (f"⚠ Not ready for {names}" if readiness.ready else f"⚠ Not ready · {names}"), p.danger
        to_fix = coverage_needs_work(card.coverage)
        if to_fix:
            return f"⚠ Needs work: {', '.join(Platform.LABELS.get(n, n) for n in to_fix)}", p.danger
        if card.row.stage == Stage.STAGING:
            if post and post.account_id and card.account:
                when = f" · {local_text(post.publish_at)}" if post.publish_at else ""
                return f"✓ Ready · {card.account.display_name}{when}", p.text_secondary
            return "✓ Ready · pick a channel", p.text_secondary
        if card.row.stage == Stage.SCHEDULED:      # moved there by hand, nothing actually scheduled: say so
            return "Nothing scheduled yet: schedule it, or move it back", p.warning_text
        return "✓ Ready · not started", p.text_muted


STEP_TIPS = {
    Stage.BACKLOG: "videos you've downloaded and haven't started on",
    Stage.STAGING: "videos you're writing posts for",
    Stage.SCHEDULED: "videos waiting to go live (cards move here once every platform is scheduled)",
    Stage.PUBLISHED: "videos that are live (cards move here once every platform is posted)",
}


def column_purpose(column) -> str:
    """The line under a column's title: its own subtitle, else what its built-in steps hold."""
    return column.purpose


class InlineEdit(QStackedWidget):
    """A label you double-click to edit in place: Return or clicking away keeps the change, Esc drops it."""

    edited = Signal(str)

    def __init__(self, text: str, object_name: str, placeholder: str, limit: int, parent=None):
        super().__init__(parent)
        self.label = QLabel(text, objectName=object_name)
        self.label.setCursor(Qt.IBeamCursor)
        self.field = tip(QLineEdit(text, objectName="InlineEdit"), placeholder)
        self.field.setPlaceholderText(placeholder)
        self.field.setMaxLength(limit)
        self.field.setProperty("role", object_name)          # styled like the label it replaces
        self.field.installEventFilter(self)
        self.field.returnPressed.connect(self._commit)
        self.field.editingFinished.connect(self._commit)
        self.addWidget(self.label)
        self.addWidget(self.field)
        self.setSizePolicy(self.label.sizePolicy())

    def sizeHint(self):
        return self.currentWidget().sizeHint()

    def minimumSizeHint(self):
        return self.currentWidget().minimumSizeHint()

    def text(self) -> str:
        return self.label.text()

    def setToolTip(self, text: str) -> None:
        self.label.setToolTip(text)

    def mouseDoubleClickEvent(self, event) -> None:
        self.start()

    def start(self, text: str | None = None) -> None:
        self.field.setText(self.label.text() if text is None else text)
        self.setCurrentWidget(self.field)
        self.field.setFocus()
        self.field.selectAll()
        self.updateGeometry()

    def editing(self) -> bool:
        return self.currentWidget() is self.field

    def _commit(self) -> None:
        if not self.editing():
            return
        self._close()
        text = self.field.text().strip()
        if text != self.label.text():
            self.edited.emit(text)

    def _close(self) -> None:
        self.setCurrentWidget(self.label)
        self.updateGeometry()

    def eventFilter(self, obj, event) -> bool:
        # Esc drops the edit (claimed before the Board's own Esc shortcut sees it)
        if obj is self.field and event.type() in (QEvent.ShortcutOverride, QEvent.KeyPress) and \
                event.key() == Qt.Key_Escape:
            event.accept()
            self._close()
            return True
        return super().eventFilter(obj, event)


EMPTY_TEXT = {
    Stage.BACKLOG: "Downloaded videos land here.",
    Stage.STAGING: "Drag videos here to write their post.",
    Stage.SCHEDULED: "Drop a ready post here to upload and schedule it.",
    Stage.PUBLISHED: "Live videos show up here.",
}


class BoardColumn(QFrame):
    renameRequested = Signal(str)        # column id
    titleEdited = Signal(str, str)       # column id, new title (edited in place)
    subtitleEdited = Signal(str, str)    # column id, new subtitle ("" puts the default back)
    addRequested = Signal(str)           # add a column after this one
    removeRequested = Signal(str)
    editRequested = Signal()

    def __init__(self, column, thumbs: ThumbnailCache, parent=None):
        super().__init__(parent)
        self.setObjectName("BoardColumn")
        self.column = column
        stage = self.stage = column.primary
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 12, 10, 6)
        lay.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(4)
        title = InlineEdit(column.name, "ColumnTitle", "Column title", NAME_MAX)
        holds = [STEP_TIPS[s] for s in column.builtin]
        title.setToolTip((f"{column.name}: " + "; ".join(holds) if holds else
                          f"{column.name}: your own column. Drag cards in and out.") +
                         "\nDouble-click to rename it. Right-click to add or remove columns.")
        title.edited.connect(lambda text: self.titleEdited.emit(self.column.id, text))
        title.label.setContextMenuPolicy(Qt.CustomContextMenu)
        title.label.customContextMenuRequested.connect(lambda pos: self._menu(title.label.mapToGlobal(pos)))
        self.title = title
        self.count = QLabel("0", objectName="ColumnCount")     # a small pill, like the website draws it
        self.menu_btn = icon_button("more", f"Change “{column.name}”: rename it, edit its subtitle, add a column "
                                            "after it or remove it", 14)
        self.menu_btn.setObjectName("IconButton")
        self.menu_btn.setProperty("subtle", True)
        self.menu_btn.clicked.connect(lambda: self._menu(self.menu_btn.mapToGlobal(self.menu_btn.rect().bottomLeft())))
        head.addWidget(title)
        head.addStretch()
        head.addWidget(self.count)
        head.addWidget(self.menu_btn)
        lay.addLayout(head)
        self.purpose = InlineEdit(column.purpose, "ColumnPurpose", "What this column holds (empty for the "
                                  "default)", SUBTITLE_MAX)
        self.purpose.setToolTip("What this column holds. Double-click to change it; clear it to get the default "
                                "back.")
        self.purpose.edited.connect(self._subtitle_edited)
        self.purpose.label.setContextMenuPolicy(Qt.CustomContextMenu)
        self.purpose.label.customContextMenuRequested.connect(
            lambda pos: self._menu(self.purpose.label.mapToGlobal(pos)))
        lay.addWidget(self.purpose)
        self.model = BoardModel(stage, self)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda pos: self._menu(self.mapToGlobal(pos)))
        self.view = QListView(objectName="BoardList")
        self.view.setModel(self.model)
        self.view.setItemDelegate(CardDelegate(thumbs, self.view))
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.setDragDropMode(QAbstractItemView.DragDrop)
        self.view.setDefaultDropAction(Qt.MoveAction)
        self.view.setDragEnabled(True)
        self.view.setAcceptDrops(True)
        self.view.setDropIndicatorShown(True)
        self.view.setMouseTracking(True)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.setUniformItemSizes(False)
        self.view.setResizeMode(QListView.Adjust)        # cards are as tall as the column is wide (16:9)
        self.view.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.empty = QLabel(EMPTY_TEXT.get(stage, "Drag cards here."))
        self.empty.setProperty("faint", True)
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.empty)

    def _subtitle_edited(self, text: str) -> None:
        default = replace(self.column, subtitle="").purpose
        self.subtitleEdited.emit(self.column.id, "" if text == default else text)

    def _menu(self, global_pos) -> None:
        menu = QMenu(self)
        menu.addAction(f"Rename “{self.column.name}”", self.title.start)
        menu.addAction("Edit Subtitle", lambda: self.purpose.start(self.column.subtitle or self.column.purpose))
        if self.column.subtitle:
            menu.addAction("Use the Default Subtitle", lambda: self.subtitleEdited.emit(self.column.id, ""))
        menu.addSeparator()
        menu.addAction("Add a Column After This…", lambda: self.addRequested.emit(self.column.id))
        menu.addAction("Remove This Column…", lambda: self.removeRequested.emit(self.column.id))
        menu.addSeparator()
        menu.addAction("Edit Columns…", self.editRequested.emit)
        menu.exec(global_pos)

    def set_cards(self, cards: list[Card], total: int | None = None) -> None:
        self.model.set_cards(cards)
        filtered = total is not None and total != len(cards)
        self.count.setText(f"{len(cards)} of {total}" if filtered else str(len(cards)))
        self.count.setToolTip(f"{len(cards)} shown by the filter or search, {total} in this column" if filtered
                              else f"{len(cards)} in this column")
        set_prop(self.count, "filtered", filtered)          # outlined, so a narrowed column can't pass as whole
        self.empty.setVisible(not cards)

    def selected_cards(self) -> list[Card]:
        return [self.model.cards[i.row()] for i in sorted(self.view.selectionModel().selectedIndexes(),
                                                           key=lambda i: i.row())]


# ====================================================================== page
class BoardPage(QWidget):
    """Thin UI over the Pipeline: shows cards, collects intent, confirms with the
    user, then calls the pipeline. Refreshes from the store's change feed, so
    edits made anywhere (another page, an upload finishing, the companion app)
    show up here."""

    openAccounts = Signal()
    statusMessage = Signal(str)

    def __init__(self, pipeline: Pipeline, uploads: UploadManager, thumbs: ThumbnailCache, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.pipeline, self.uploads, self.thumbs = pipeline, uploads, thumbs
        self.store = pipeline.store
        self.profile: Profile | None = None
        self.cards: dict[int, Card] = {}
        self.selected_id: int | None = None
        self.bulk_ids: list[int] = []
        self.filter = "all"
        self.form = ""                   # "" both · readiness.SHORT_FORM · readiness.LONG_FORM
        self.tag = ""
        self.product_ids: set | None = None
        self.query = ""
        self.sync_worker: CallWorker | None = None
        self.task_worker: CallWorker | None = None
        self._last_sync = 0.0
        self._closed = False
        self._reload_timer = QTimer(self, singleShot=True, interval=30, timeout=self.reload)
        self._advance_timer = QTimer(self, interval=60_000, timeout=self._advance_due)
        self._advance_timer.start()
        self._build_ui()
        self._bridge = change_bridge(self.store)
        self._bridge.changed.connect(self._on_store_change)
        uploads.progress.connect(self._on_upload_progress)
        uploads.finished.connect(lambda _row, _ok, message: self._status(message))
        uploads.changed.connect(self._schedule_reload)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 12)
        outer.setSpacing(12)
        head = QHBoxLayout()
        head.setSpacing(10)
        title = QLabel("Board", objectName="PageTitle")
        self.subtitle = QLabel(objectName="PageCount")
        head.addWidget(title, 0, Qt.AlignBottom)
        head.addWidget(self.subtitle, 0, Qt.AlignBottom)
        self.clear_filters_btn = tip(QPushButton("Show All"), "Clear the search and every filter")
        set_prop(self.clear_filters_btn, "flat")
        self.clear_filters_btn.clicked.connect(self.clear_filters)
        self.clear_filters_btn.hide()
        head.addWidget(self.clear_filters_btn, 0, Qt.AlignBottom)
        head.addStretch()
        self.sync_label = QLabel()
        self.sync_label.setProperty("faint", True)
        head.addWidget(self.sync_label)
        # What AI assistants asked to do, waiting for a person: amber, and only there while something waits
        self.approvals_btn = tip(QPushButton(objectName="ApprovalsButton"),
                                 "Requests from AI assistants waiting for your decision: review, approve or reject")
        self.approvals_btn.setCursor(Qt.PointingHandCursor)
        self.approvals_btn.clicked.connect(lambda: self.open_approvals())
        self.approvals_btn.hide()
        head.addWidget(self.approvals_btn)
        self.search = search_field(
            "Search titles, captions and what was said",
            "Find videos by words in the title, caption, any platform's copy, hashtags, topic, keywords, notes, or "
            "anything said in the video (its transcript). Put \"quotes\" around an exact phrase.", width=340)
        self.search.textChanged.connect(self._on_search)
        head.addWidget(self.search)

        # Filter & Sort popover: readiness, hashtag, brand / product
        self.filter_box = tip(QComboBox(), "Show only tickets that are ready, not ready, or blocked on a platform")
        for key, label in (("all", "All tickets"), ("ready", "Ready"), ("not_ready", "Not ready"),
                           *((f"blocking:{p}", f"Blocking {Platform.LABELS[p]}") for p in Platform.ALL)):
            self.filter_box.addItem(label, key)
        self.filter_box.currentIndexChanged.connect(self._on_filter)
        self.tag_box = tip(QComboBox(), "Show only tickets with one hashtag, like a category. The number is how many "
                                        "tickets have it.")
        self.tag_box.setMinimumWidth(260)
        self.tag_box.addItem("All hashtags", "")
        self.tag_box.currentIndexChanged.connect(self._on_filter)
        from .widgets import ProductFilter
        self.products_filter = ProductFilter(self.store)
        self.product_box = self.products_filter.product       # brand menu: products_filter.brand
        self.products_filter.changed.connect(lambda _ids: self._on_filter())
        self.filter_popover = Popover(self)
        self.filter_popover.add_section("Show", "Which tickets the Board shows", self.filter_box)
        self.filter_popover.add_section("Hashtag", "Only tickets with this hashtag", self.tag_box)
        from ..readiness import LONG_FORM, SHORT_FORM
        self.form_box = tip(QComboBox(), "Show only Shorts and other short vertical videos (3 minutes or less), "
                                         "or only long-form videos")
        for key, label in (("", "All formats"), (SHORT_FORM, "Short-form"), (LONG_FORM, "Long-form")):
            self.form_box.addItem(label, key)
        self.form_box.currentIndexChanged.connect(self._on_filter)
        self.filter_popover.add_section("Format", "Short-form (vertical, 3 minutes or less) or long-form",
                                        self.form_box)
        self.filter_popover.add_section("Brand and product", "Only tickets tagged with a brand's products, or one "
                                        "product", self.products_filter)
        self.density_box = tip(QComboBox(), "Large cards lead with the video's thumbnail; compact cards fit more "
                                            "on screen")
        self.density_box.addItem("Large, with thumbnails", "large")
        self.density_box.addItem("Compact", "compact")
        self.density_box.setCurrentIndex(max(self.density_box.findData(
            self.store.get_setting("ui.board_density", "large")), 0))
        CardDelegate.density = self.density_box.currentData()
        self.density_box.currentIndexChanged.connect(self._set_density)
        self.filter_popover.add_section("Cards", "How big the Board's cards are", self.density_box)
        self.filter_btn = FilterButton(self.filter_popover)
        head.addWidget(self.filter_btn)
        self.columns_btn = tip(QToolButton(text="Columns", objectName="FilterButton"),
                               "Customize the Board: rename columns, change their subtitles, add, remove or "
                               "reorder them")
        self.columns_btn.setCursor(Qt.PointingHandCursor)
        self.columns_btn.clicked.connect(self.edit_columns)
        head.addWidget(self.columns_btn)

        # Less frequent board actions live in ⋯; checking YouTube is one click
        self.more_btn = icon_button("more", "More: import tickets from a spreadsheet, edit the columns, or see "
                                            "archived videos")
        self.more_btn.setPopupMode(QToolButton.InstantPopup)
        more = QMenu(self.more_btn)
        more.addAction("Import Sheet…", self.open_import)
        more.addAction("Edit Columns…", self.edit_columns)
        more.addAction("Archived Videos…", self.open_archived)
        self.more_btn.setMenu(more)
        self.sync_btn = icon_button("refresh", "Check YouTube for updates: whether scheduled videos have gone live "
                                               "(moves them to Posted)")
        self.sync_btn.clicked.connect(lambda: self.sync(force=True))
        head.addWidget(self.sync_btn)
        head.addWidget(self.more_btn)
        outer.addLayout(head)

        cols_body = QWidget(objectName="Page")
        self.columns_layout = QHBoxLayout(cols_body)
        self.columns_layout.setContentsMargins(0, 0, 0, 0)
        self.columns_layout.setSpacing(0)
        self.columns_splitter: QSplitter | None = None
        self.columns: dict[str, BoardColumn] = {}
        self.column_config = default_columns()
        self._build_columns()
        cols = QScrollArea()                 # many columns scroll sideways instead of squeezing
        cols.setWidgetResizable(True)
        cols.setFrameShape(QFrame.NoFrame)
        cols.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        cols.setWidget(cols_body)

        self.panel = PostPanel(self.thumbs)
        self.panel.use_player(True)
        self.panel.set_enabled_platforms(self.pipeline.enabled_platforms())
        self.panel.saveRequested.connect(self._save_post)
        self.panel.uploadRequested.connect(self._request_upload)
        self.panel.cancelUploadRequested.connect(self.pipeline.cancel_upload)
        self.panel.connectRequested.connect(self.openAccounts.emit)
        self.panel.readiness_for = lambda post: self.pipeline.readiness(post.video_row, post)
        self.panel.stage_name = self.stage_name
        self.panel.store = self.store
        self.panel.original_path_for = self.pipeline.original_path
        self.panel.video_tools.message.connect(self._status)
        self.panel.ticketFieldCommitted.connect(self._commit_ticket_field)
        self.panel.markPostedRequested.connect(self._mark_posted)
        self.panel.unmarkRequested.connect(self._unmark_posted)
        self.panel.revisionsRequested.connect(self._show_revisions)
        self.panel.fillRequested.connect(lambda row_id: self._fill([row_id]))
        self.panel.targetsChanged.connect(self._set_targets)
        self.panel.platformMarkRequested.connect(self._mark_platform)
        self.panel.handoffRequested.connect(self.prepare_to_publish)
        self.panel.soundRequested.connect(self.choose_sound)
        self.panel.soundRemoved.connect(self.remove_sound)
        self.panel.recoverRequested.connect(self.open_recovery)
        self.panel.transcriptRequested.connect(lambda row_id: self._get_transcripts([row_id], ask_replace=True))
        self.panel.batchRepurposeRequested.connect(lambda row_id: self.open_batch_repurpose([row_id] if row_id else None))
        self.panel.multiSelectRequested.connect(self.select_all_in_column)
        self.bulk = BulkPanel()
        self.bulk.applyRequested.connect(self._bulk_apply)
        self.bulk.fillRequested.connect(lambda: self._fill(self.bulk_ids))
        self.bulk.transcriptsRequested.connect(lambda: self._get_transcripts(self.bulk_ids))
        self.bulk.archiveRequested.connect(lambda: self._archive(self.bulk_ids))
        self.bulk_repurpose_btn = tip(
            QPushButton("Batch Repurpose with Muse Spark"),
            "Draft copy across 6 platforms for all selected videos with Meta Muse Spark. "
            "You review and approve each video before anything is saved.",
        )
        self.bulk_repurpose_btn.setIcon(icons.icon("sparkles", 16, theme.P.text_primary))
        self.bulk_repurpose_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.P.accent_soft}; border: 1px solid {theme.P.accent_ring}; "
            f"border-radius: 10px; padding: 8px 12px; font-weight: 700; color: {theme.P.text_primary}; }}"
            f"QPushButton:hover {{ border-color: {theme.P.accent_color}; }}"
        )
        self.bulk_repurpose_btn.clicked.connect(lambda: self.open_batch_repurpose(self.bulk_ids))
        if hasattr(self.bulk, "fill_btn") and self.bulk.fill_btn.parentWidget():
            _bulk_lay = self.bulk.fill_btn.parentWidget().layout()
            if _bulk_lay is not None:
                _bulk_lay.insertWidget(_bulk_lay.indexOf(self.bulk.fill_btn), self.bulk_repurpose_btn)
        self.side = QStackedWidget()
        self.side.addWidget(self.panel)
        self.side.addWidget(self.bulk)
        # A selected card that an AI assistant's request is waiting on says so above its ticket
        self.approval_bar = QFrame(objectName="ApprovalBar")
        bar = QHBoxLayout(self.approval_bar)
        bar.setContentsMargins(12, 8, 8, 8)
        self.approval_text = QLabel(objectName="ApprovalBarText")
        self.approval_text.setWordWrap(True)
        bar.addWidget(self.approval_text, 1)
        review = tip(QPushButton("Review…"), "Open Approvals on this video's request: see what it would do, "
                                            "then approve or reject it")
        review.clicked.connect(lambda: self.open_approvals(self.selected_id))
        bar.addWidget(review)
        self.approval_bar.hide()
        side_box = QWidget()
        side_lay = QVBoxLayout(side_box)
        side_lay.setContentsMargins(0, 0, 0, 0)
        side_lay.setSpacing(0)
        side_lay.addWidget(self.approval_bar)
        side_lay.addWidget(self.side, 1)
        # The ticket only takes room while a card is selected; otherwise the columns get the whole width.
        # The side panel: the video on top, the ticket under it.
        self.player = PlayerWidget()
        self.player.message.connect(self._status)
        self._player_row: int | None = None
        self.media_pane = MediaPane(self.player, side_box)
        self.inspector = Inspector(self.media_pane, "")
        self.inspector.closeRequested.connect(self.close_inspector)
        self.split = ContextualSplitter(cols, self.inspector, self.pipeline.store, "board", default_width=420)
        outer.addWidget(self.split, 1)
        esc = QShortcut(QKeySequence(Qt.Key_Escape), self, context=Qt.WidgetWithChildrenShortcut)
        esc.activated.connect(self.close_inspector)
        self.thumbs.loaded.connect(lambda _k: [c.view.viewport().update() for c in self.columns.values()])

    # ---------------------------------------------------------------- columns
    def _build_columns(self) -> None:
        """Columns sit in a splitter: drag the gap between two columns to resize them
        (remembered per profile); below the minimum width the board scrolls sideways."""
        while self.columns_layout.count():
            w = self.columns_layout.takeAt(0).widget()
            if w:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        self.columns_splitter = QSplitter(Qt.Horizontal)
        self.columns_splitter.setChildrenCollapsible(False)
        self.columns_splitter.setHandleWidth(10)
        self.columns_splitter.setToolTip("")
        self.columns_layout.addWidget(self.columns_splitter)
        self.columns = {}
        for column in self.column_config:
            col = BoardColumn(column, self.thumbs)
            col.model.moveRequested.connect(self._on_move)
            col.view.selectionModel().currentChanged.connect(
                lambda cur, _prev, c=col: self._on_select(c, cur))
            col.view.clicked.connect(lambda idx, c=col: self._on_select(c, idx))
            col.view.selectionModel().selectionChanged.connect(lambda *_a, c=col: self._on_selection(c))
            col.view.customContextMenuRequested.connect(lambda pos, c=col: self._card_menu(c, pos))
            col.renameRequested.connect(self._rename_column)
            col.addRequested.connect(self._add_column)
            col.removeRequested.connect(self._remove_column)
            col.editRequested.connect(self.edit_columns)
            # the column is rebuilt by the change, so apply it after the edit field has finished with itself
            col.titleEdited.connect(lambda cid, text: QTimer.singleShot(
                0, lambda: self._edit_column_text(cid, title=text)))
            col.subtitleEdited.connect(lambda cid, text: QTimer.singleShot(
                0, lambda: self._edit_column_text(cid, subtitle=text)))
            col.setMinimumWidth(COLUMN_MIN_WIDTH)
            self.columns[column.id] = col
            self.columns_splitter.addWidget(col)
        for i in range(1, self.columns_splitter.count()):
            self.columns_splitter.handle(i).setToolTip("Drag to make the columns wider or narrower")
        self._restore_column_sizes()
        self.columns_splitter.splitterMoved.connect(lambda *_a: self._save_column_sizes())
        if hasattr(self, "panel"):
            self.panel.stage_name = self.stage_name

    def _sizes_key(self) -> str:
        return f"board/column_widths/{self.profile.id if self.profile else 0}"

    def _save_column_sizes(self) -> None:
        widths = dict(zip(self.columns, self.columns_splitter.sizes()))
        QSettings().setValue(self._sizes_key(), json.dumps(widths))

    def _restore_column_sizes(self) -> None:
        try:
            saved = json.loads(QSettings().value(self._sizes_key(), "") or "{}")
        except (TypeError, ValueError):
            saved = {}
        if saved:
            self.columns_splitter.setSizes([max(int(saved.get(cid, COLUMN_MIN_WIDTH)), COLUMN_MIN_WIDTH)
                                            for cid in self.columns])

    def _column_for(self, stage: str) -> BoardColumn:
        return self.columns[column_for(self.column_config, stage).id]

    def stage_name(self, stage: str) -> str:
        return column_for(self.column_config, stage).name

    def _apply_columns(self, columns, moved: tuple[str, str] | None = None) -> bool:
        """Save a new column setup (moving cards out of a removed column) and redraw."""
        try:
            self.column_config = save_columns(self.store, self.profile.id, columns)
        except ColumnError as exc:
            QMessageBox.warning(self, "Columns", str(exc))
            return False
        if moved:
            from_stage, to_stage = moved
            rows = [r.id for r in self.store.load_videos(self.profile.id, archived=None) if r.stage == from_stage]
            if rows:
                self.store.set_stage(rows, to_stage)
        self.bulk_ids, self.selected_id = [], None
        self._hide_inspector()
        self._build_columns()
        self.reload()
        return True

    def _edit_column_text(self, column_id: str, title: str | None = None, subtitle: str | None = None) -> None:
        """A title or subtitle edited in place on the Board."""
        columns = [replace(c, stages=list(c.stages)) for c in self.column_config]
        col = next((c for c in columns if c.id == column_id), None)
        if col is None:
            return
        if title is not None:
            if not title:
                return                       # an empty title keeps the old one
            col.name = title
        if subtitle is not None:
            col.subtitle = subtitle
        self._apply_columns(columns)

    def _rename_column(self, column_id: str) -> None:
        from PySide6.QtWidgets import QInputDialog
        columns = [replace(c, stages=list(c.stages)) for c in self.column_config]
        col = next(c for c in columns if c.id == column_id)
        name, ok = QInputDialog.getText(self, "Rename Column", "Column name:", text=col.name)
        if ok and name.strip():
            col.name = name.strip()
            self._apply_columns(columns)

    def _add_column(self, after_id: str) -> None:
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Add a Column", "Name for the new column:", text="New column")
        if not ok or not name.strip():
            return
        columns = [replace(c, stages=list(c.stages)) for c in self.column_config]
        index = next(i for i, c in enumerate(columns) if c.id == after_id) + 1
        try:
            add_column(columns, name, index)
        except ColumnError as exc:
            QMessageBox.warning(self, "Columns", str(exc))
            return
        self._apply_columns(columns)

    def _remove_column(self, column_id: str) -> None:
        columns = [replace(c, stages=list(c.stages)) for c in self.column_config]
        try:
            removed, neighbour = remove_column(columns, column_id)
        except ColumnError as exc:
            QMessageBox.warning(self, "Columns", str(exc))
            return
        n = sum(1 for c in self.cards.values() if c.row.stage in removed.stages)
        cards = f" Its {n} card{'s' if n != 1 else ''} will show in “{neighbour.name}”." if n else ""
        if QMessageBox.question(self, "Remove Column", f"Remove the column “{removed.name}”?{cards} Nothing is "
                                "deleted, and you can add columns back any time.") != QMessageBox.Yes:
            return
        own = [s for s in removed.stages if is_custom(s)]
        self._apply_columns(columns, moved=(own[0], neighbour.primary) if own else None)

    def edit_columns(self) -> None:
        if not self.profile:
            return
        from .board_tools import ColumnsDialog
        dialog = ColumnsDialog(self.column_config, {s: sum(1 for c in self.cards.values() if c.row.stage == s)
                                                    for s in {c.row.stage for c in self.cards.values()}}, self)
        if dialog.exec() == ColumnsDialog.Accepted:
            columns, moves = dialog.result_columns()
            if self._apply_columns(columns):
                for from_stage, to_stage in moves:
                    rows = [r.id for r in self.store.load_videos(self.profile.id, archived=None)
                            if r.stage == from_stage]
                    if rows:
                        self.store.set_stage(rows, to_stage)
                self.reload()

    # ---------------------------------------------------------------- data
    def set_profile(self, profile: Profile) -> None:
        self.profile = profile
        self.column_config = load_columns(self.store, profile.id)
        self._build_columns()
        self.selected_id = None
        self.bulk_ids = []
        self._hide_inspector()
        self.subtitle.setText("Drag cards to move them along")
        self._last_sync = 0.0
        self._advance_due()
        self.reload()

    def _on_store_change(self, change: Change) -> None:
        if change.entity in ("video", "post", "account", "dispatch", "approval") and self.profile and \
                change.profile_id in (None, self.profile.id):
            self._schedule_reload()

    def _schedule_reload(self) -> None:
        self._reload_timer.start()

    def reload(self) -> None:
        self._reload_timer.stop()
        if not self.profile or self._closed:
            return
        try:
            columns = self.pipeline.board(self.profile.id)
        except PipelineError:  # profile was just deleted
            return
        if self._run_rules():
            columns = self.pipeline.board(self.profile.id)     # rules changed tickets: show the result
        self.cards = {card.id: card for cards in columns.values() for card in cards}
        self._mark_cards()
        hits = {h.row_id: h for h in self.store.search(self.profile.id, self.query)} if self.query else {}
        for card in self.cards.values():
            card.search_hit = hits.get(card.id)
        self._refresh_tag_box(self.cards.values())
        self._refresh_product_box(self.cards.values())
        by_column: dict[str, list[Card]] = {c.id: [] for c in self.column_config}
        for stage, cards in columns.items():
            by_column[column_for(self.column_config, stage).id].extend(cards)
        for column_id, col in self.columns.items():
            cards = by_column.get(column_id, [])
            col.set_cards([c for c in cards if self._matches(c)], total=len(cards))
        self._show_filter_state()
        if self.bulk_ids:
            self._reselect_bulk()
        else:
            self._reselect()

    def _mark_cards(self) -> None:
        """What each card's small marks say: an Automation acted on it, an agent's request awaits
        approval, or an AI assistant made the latest copy edit."""
        from ..rules import AGENT_ORIGINS, COPY_REVISION_FIELDS, activity
        from ..scoped_pipeline import OPERATION_LABELS
        from .approvals_dialog import pending_for
        try:
            origins = self.store.latest_copy_origins(self.profile.id, COPY_REVISION_FIELDS)
            requests = pending_for(self.store, self.profile.id)        # expired ones don't count
            asked: dict[int, list[str]] = {}
            for req in requests:
                for vid in req.target_ids:
                    label = OPERATION_LABELS.get(req.operation, req.operation)
                    if label not in asked.setdefault(vid, []):
                        asked[vid].append(label)
            pending = set(asked)
            ran: dict[int, list[str]] = {}
            for entry in activity(self.store, self.profile.id):
                if entry.get("done") and entry["done"] != ["nothing to do"]:
                    names = ran.setdefault(entry.get("video"), [])
                    if entry.get("rule") and entry["rule"] not in names:
                        names.append(entry["rule"])
        except Exception:  # noqa: BLE001 — marks are a nicety; never break the board
            log.exception("Couldn't work out card marks")
            return
        self._set_approvals_count(len(requests))
        for card in self.cards.values():
            card.pending_approvals = asked.get(card.id, [])
            card.flags = {"automation": ran.get(card.id, [])[:3], "approval": card.id in pending,
                          "ai": origins.get(card.id, "").split(":")[0] in AGENT_ORIGINS}

    def _set_density(self) -> None:
        CardDelegate.density = self.density_box.currentData()
        self.store.set_setting("ui.board_density", CardDelegate.density)
        for col in self.columns.values():
            col.model.layoutChanged.emit()

    def _run_rules(self) -> bool:
        """Automations: run enabled rules on videos they haven't handled yet. True if any acted."""
        if getattr(self, "_rules_running", False) or self.store.read_only or not self.profile:
            return False
        from .. import rules
        self._rules_running = True
        try:
            entries = rules.run(self.pipeline, self.profile.id)
        except Exception:  # noqa: BLE001 — a broken rule must never break the board
            log.exception("Automations failed")
            return False
        finally:
            self._rules_running = False
        if entries:
            names = sorted({e["rule"] for e in entries})
            self._status(f"Automations: {', '.join(names)} acted on {len(entries)} video"
                         f"{'s' if len(entries) != 1 else ''}. See Automations → Activity.")
        return bool(entries)

    # ---------------------------------------------------------------- filter
    def _matches(self, card: Card) -> bool:
        from ..tags import video_tags
        if self.tag and self.tag not in video_tags(card.row.item, card.post):
            return False
        if self.product_ids and not any(p.get("id") in self.product_ids for p in card.row.item.products or []):
            return False
        if self.form:
            from ..readiness import video_form
            if video_form(card.row.item) != self.form:
                return False
        if self.query and card.search_hit is None and not all(word in self._haystack(card)
                                                              for word in self.query.split()):
            return False
        r = card.readiness
        if self.filter == "all" or r is None:
            return True
        if self.filter == "ready":
            return r.ready and not r.blocking
        if self.filter == "not_ready":
            return bool(r.blocking)
        return self.filter.split(":", 1)[1] in r.blocking

    @staticmethod
    def _haystack(card: Card) -> str:
        item, post, t = card.row.item, card.post, card.row.ticket
        parts = [card.title, item.hook, item.description, item.custom_name, t.topic or "", t.keywords or "",
                 t.creator or "", *(f"{p.get('name', '')} {p.get('title', '')}" for p in item.products or [])]
        if post is not None:
            parts += [post.title, post.description, " ".join(post.tags), post.hashtags or "",
                      *(getattr(post, f) or "" for f in ("tiktok_text", "instagram_text", "bluesky_text",
                                                         "threads_text", "facebook_text"))]
        return " ".join(parts).lower()

    def _filtering(self) -> bool:
        return bool(self.query or self.filter != "all" or self.form or self.tag or self.product_ids)

    def _show_filter_state(self) -> None:
        """Say plainly when the Board shows only some tickets, with a way back to all of them."""
        total = sum(c.model.rowCount() for c in self.columns.values())
        if self._filtering():
            self.subtitle.setText(f"Showing {total} of {len(self.cards)} tickets")
        else:
            self.subtitle.setText("Drag cards to move them along")
        self.clear_filters_btn.setVisible(self._filtering())

    def clear_filters(self) -> None:
        for box in (self.filter_box, self.form_box, self.tag_box, self.products_filter.brand,
                    self.products_filter.product):
            box.setCurrentIndex(0)
        self.search.clear()
        self._on_filter()

    def _on_search(self, text: str) -> None:
        self.query = text.strip().lower()
        self._reload_timer.start()

    def _refresh_tag_box(self, cards) -> None:
        from ..tags import counts, video_tags
        found = counts(video_tags(c.row.item, c.post) for c in cards)
        wanted = [("", "All hashtags")] + [(t, f"#{t}  ({n})") for t, n in found]
        if [(self.tag_box.itemData(i), self.tag_box.itemText(i)) for i in range(self.tag_box.count())] == wanted:
            return
        self.tag_box.blockSignals(True)
        self.tag_box.clear()
        for tag, label in wanted:
            self.tag_box.addItem(label, tag)
        index = self.tag_box.findData(self.tag)
        self.tag_box.setCurrentIndex(max(index, 0))
        if index < 0:
            self.tag = ""
        self.tag_box.blockSignals(False)

    def _refresh_product_box(self, cards) -> None:
        from ..products import video_products
        self.products_filter.blockSignals(True)
        self.products_filter.set_videos(video_products(c.row.item) for c in cards)
        self.products_filter.blockSignals(False)
        self.product_ids = self.products_filter.selection()

    def filter_by_tag(self, tag: str) -> None:
        index = self.tag_box.findData(tag.lstrip("#").lower())
        if index >= 0:
            self.tag_box.setCurrentIndex(index)

    def _on_filter(self) -> None:
        self.filter = self.filter_box.currentData()
        self.form = self.form_box.currentData() or ""
        self.tag = self.tag_box.currentData() or ""
        self.product_ids = self.products_filter.selection()
        self.filter_btn.set_count(sum(bool(x) for x in (self.filter != "all", self.form, self.tag,
                                                         self.product_ids)))
        self.reload()

    # ---------------------------------------------------------------- multi-select & bulk
    def _on_selection(self, column: BoardColumn) -> None:
        chosen = column.selected_cards()
        if len(chosen) > 1:
            for other in self.columns.values():
                if other is not column:
                    other.view.blockSignals(True)
                    other.view.clearSelection()
                    other.view.blockSignals(False)
            self.bulk_ids = [c.id for c in chosen]
            self.selected_id = None
            self.bulk.show_cards(chosen)
            self.approval_bar.hide()
            self.side.setCurrentWidget(self.bulk)
            self.player.clear()
            self._player_row = None
            self.media_pane.set_player_visible(False)
            self.inspector.set_title(f"{len(chosen)} selected")
            self.split.reveal()
        elif self.bulk_ids:
            self.bulk_ids = []
            self.side.setCurrentWidget(self.panel)
            if chosen:
                self.selected_id = None
                self._on_select(column, column.model.index(column.model.cards.index(chosen[0])))
            else:
                self._hide_inspector()
        elif not chosen and not any(c.selected_cards() for c in self.columns.values()):
            self.selected_id = None          # clicked an empty spot: nothing selected, so no ticket
            self._hide_inspector()

    def _reselect_bulk(self) -> None:
        cards = [self.cards[i] for i in self.bulk_ids if i in self.cards]
        stages = {c.row.stage for c in cards}
        if len(cards) < 2 or len(stages) != 1:
            self.bulk_ids = []
            self._hide_inspector()
            return
        col = self._column_for(stages.pop())
        sel = col.view.selectionModel()
        sel.blockSignals(True)
        for row, c in enumerate(col.model.cards):
            if c.id in self.bulk_ids:
                sel.select(col.model.index(row), QItemSelectionModel.Select)
        sel.blockSignals(False)
        col.view.viewport().update()
        self.bulk.show_cards([c for c in col.model.cards if c.id in self.bulk_ids])

    def _bulk_apply(self, changes: dict) -> None:
        try:
            n = self.pipeline.bulk_update(self.bulk_ids, changes)
        except PipelineError as exc:
            self.bulk.show_errors(exc.problems)
            return
        self.bulk.clear_inputs()
        self.bulk.show_errors([])
        self._status(f"Updated {n} tickets.")

    def _card_menu(self, column: BoardColumn, pos) -> None:
        index = column.view.indexAt(pos)
        if not index.isValid():
            return
        chosen = column.selected_cards()
        clicked = column.model.cards[index.row()]
        if clicked not in chosen:
            column.view.setCurrentIndex(index)
            chosen = [clicked]
        ids = [c.id for c in chosen]
        n = len(ids)
        menu = QMenu(self)
        menu.addAction("Batch Repurpose with Muse Spark…", lambda: self.open_batch_repurpose(ids))
        menu.addAction("Fill Empty Copy from Original Post" + ("s" if n > 1 else ""), lambda: self._fill(ids))
        menu.addAction("Get Transcript" + ("s" if n > 1 else "") + " from Original Post" + ("s" if n > 1 else ""),
                       lambda: self._get_transcripts(ids, ask_replace=n == 1))
        if n == 1:
            menu.addSeparator()
            menu.addAction("Mark as Posted…", lambda: self._mark_posted(ids[0]))
            menu.addAction("Change History…", lambda: self._show_revisions(ids[0]))
        menu.addSeparator()
        menu.addAction(f"Archive {n} Video{'s' if n > 1 else ''}" if n > 1 else "Archive", lambda: self._archive(ids))
        menu.exec(column.view.viewport().mapToGlobal(pos))

    # ---------------------------------------------------------------- fill, transcripts, archive, import
    def _fill(self, row_ids: list[int]) -> None:
        if not row_ids:
            return
        try:
            filled = self.pipeline.fill_from_original(row_ids)
        except PipelineError as exc:
            self._status(str(exc))
            return
        fields = sum(len(f) for f in filled.values())
        self._status(f"Filled {fields} empty field{'s' if fields != 1 else ''} on {len(filled)} ticket"
                     f"{'s' if len(filled) != 1 else ''} from the original posts." if filled
                     else "Nothing to fill: those tickets' copy is already written (or doesn't fit).")

    def _get_transcripts(self, row_ids: list[int], ask_replace: bool = False) -> None:
        if not row_ids or (self.task_worker and self.task_worker.isRunning()):
            return
        from ..ytdl import base_options
        replace_existing = False
        if ask_replace and len(row_ids) == 1:
            row = self.store.get_video(row_ids[0])
            if row and row.ticket.transcript:
                if QMessageBox.question(self, "Replace Transcript", "This ticket already has a transcript. Replace "
                                        "it with the original post's captions?") != QMessageBox.Yes:
                    return
                replace_existing = True
        opts = base_options(self.profile.cookies_browser or None) if self.profile else {}

        def work():
            got, failed = 0, []
            for row_id in row_ids:
                try:
                    self.pipeline.fetch_transcript(row_id, opts, replace_existing=replace_existing)
                    got += 1
                except PipelineError as exc:
                    if exc.code != "invalid_post" or len(row_ids) == 1:   # "already has one" is fine in bulk
                        failed.append(str(exc))
            return got, failed

        self._status(f"Getting transcript{'s' if len(row_ids) > 1 else ''} from the original post"
                     f"{'s' if len(row_ids) > 1 else ''}…")
        self.task_worker = CallWorker(work, parent=self)
        self.task_worker.succeeded.connect(self._transcripts_done)
        self.task_worker.failed.connect(self._status)
        self.task_worker.start()

    def _transcripts_done(self, result) -> None:
        got, failed = result
        message = f"Got {got} transcript{'s' if got != 1 else ''}."
        if failed:
            message += f" {len(failed)} couldn't: {failed[0]}"
        self._status(message)

    def _archive(self, row_ids: list[int]) -> None:
        if not row_ids:
            return
        n = len(row_ids)
        if QMessageBox.question(self, "Archive", f"Archive {n} video{'s' if n > 1 else ''}? "
                                f"{'They' if n > 1 else 'It'} disappear{'' if n > 1 else 's'} from the Board and the "
                                "Download page. Files, tickets and history are kept, and you can bring "
                                f"{'them' if n > 1 else 'it'} back from Archived….") != QMessageBox.Yes:
            return
        try:
            self.pipeline.archive(row_ids, True)
        except PipelineError as exc:
            QMessageBox.warning(self, "Archive", str(exc) + ("\n\n" + "\n".join(exc.problems)
                                                              if len(exc.problems) > 1 else ""))
            return
        self.bulk_ids = []
        self.selected_id = None
        self._hide_inspector()
        self._status(f"Archived {n} video{'s' if n > 1 else ''}.")
        self.reload()

    def _set_approvals_count(self, n: int) -> None:
        self.approvals_btn.setText(f"Approvals · {n}")
        self.approvals_btn.setVisible(n > 0)

    def open_approvals(self, focus_video: int | None = None) -> None:
        """Review what AI assistants asked to do (approvals_dialog.py). Deciding never carries anything
        out here: the dispatcher does, once, after checking the video hasn't changed."""
        if not self.profile:
            return
        from .approvals_dialog import ApprovalsDialog
        dialog = ApprovalsDialog(self.pipeline, self.profile, self, focus_video=focus_video)
        dialog.exec()
        if dialog.decided:
            self.reload()

    def open_archived(self) -> None:
        if not self.profile:
            return
        dialog = ArchivedDialog(self.pipeline, self.profile, self)
        dialog.exec()
        if dialog.restored:
            self._status(f"Brought back {dialog.restored} video{'s' if dialog.restored != 1 else ''}.")
            self.reload()

    def open_import(self) -> None:
        if not self.profile:
            return
        dialog = ImportDialog(self.pipeline, self.profile, self)
        if dialog.exec() == ImportDialog.Accepted and dialog.applied:
            self._status(f"Imported {dialog.applied} ticket{'s' if dialog.applied != 1 else ''} from the sheet.")
            self.reload()

    def open_batch_repurpose(self, card_ids: list[int] | int | None = None) -> None:
        """Opens the Batch Review GUI for the selected tickets (or current stage)."""
        if isinstance(card_ids, int):
            ids = [card_ids]
        elif card_ids is not None:
            ids = list(card_ids)
        elif self.bulk_ids:
            ids = list(self.bulk_ids)
        elif self.selected_id:
            ids = [self.selected_id]
        else:
            ids = list(self.cards.keys())

        # If a single card id was requested, include other cards in the same column/stage if available
        if len(ids) == 1 and ids[0] in self.cards:
            card = self.cards[ids[0]]
            col = self._column_for(card.row.stage)
            if col and len(col.model.cards) > 1:
                ids = [c.id for c in col.model.cards]

        cards = [self.cards[i] for i in ids if i in self.cards]
        if not cards:
            return

        from .batch_dialog import BatchReviewDialog
        dialog = BatchReviewDialog(
            cards=cards,
            store=self.store,
            pipeline=self.pipeline,
            thumbs=self.thumbs,
            parent=self,
        )
        dialog.exec()
        self.reload()

    def select_all_in_column(self, card_id: int | None = None) -> None:
        """Selects all cards in the column containing the specified card."""
        cid = card_id or self.selected_id
        if not cid or cid not in self.cards:
            return
        stage = self.cards[cid].row.stage
        col = self._column_for(stage)
        if not col or not col.model.cards:
            return
        sel = col.view.selectionModel()
        sel.blockSignals(True)
        for row in range(len(col.model.cards)):
            sel.select(col.model.index(row), QItemSelectionModel.Select)
        sel.blockSignals(False)
        self._on_selection(col)

    def _reselect(self) -> None:
        card = self.cards.get(self.selected_id) if self.selected_id else None
        if card is None:
            self._hide_inspector()
            return
        col = self._column_for(card.row.stage)
        for row, c in enumerate(col.model.cards):
            if c.id == card.id:
                col.view.setCurrentIndex(col.model.index(row))
        self._show_card(card)

    def next_up(self) -> tuple[str, datetime | None]:
        """The next thing going out for this profile: ("TikTok", when) or ("", None)."""
        from ..models import DispatchStatus
        from ..publishing import parse_utc
        now = datetime.now(timezone.utc)
        best: tuple[datetime, str] | None = None
        for card in self.cards.values():
            moments = [(d.publish_at or d.due_at, d.platform) for d in card.dispatches
                       if d.status in (DispatchStatus.PENDING, DispatchStatus.SCHEDULED)]
            if card.post and card.post.status == PostStatus.SCHEDULED and card.post.publish_at:
                moments.append((card.post.publish_at, "youtube"))
            for iso, platform in moments:
                when = parse_utc(iso) if iso else None
                if when and when > now and (best is None or when < best[0]):
                    best = (when, platform)
        return (Platform.LABELS.get(best[1], best[1]), best[0]) if best else ("", None)

    # ---------------------------------------------------------------- the inspector
    def close_inspector(self) -> None:
        """The person closed the ticket (✕ or Esc): offer to save typed copy, then clear the selection."""
        if not self.split.is_open:
            return
        if self.side.currentWidget() is self.panel and self.panel.has_unsaved_changes():
            answer = QMessageBox.question(self, "Unsaved changes", "Save the changes to this ticket before closing?",
                                          QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                                          QMessageBox.Save)
            if answer == QMessageBox.Cancel:
                return
            if answer == QMessageBox.Save:
                try:
                    self.pipeline.save_draft(self.panel.collect())
                except PipelineError as exc:
                    self.panel.show_errors(exc.problems)   # stay open so nothing typed is lost
                    self._status(str(exc))
                    return
                self._status("Draft saved.")
                QTimer.singleShot(0, self.reload)
        for col in self.columns.values():
            sel = col.view.selectionModel()
            sel.blockSignals(True)
            sel.clear()
            sel.blockSignals(False)
            col.view.viewport().update()
        self.selected_id, self.bulk_ids = None, []
        self._hide_inspector()

    def _hide_inspector(self) -> None:
        self.approval_bar.hide()
        self.side.setCurrentWidget(self.panel)
        self.panel.show_empty()
        self.player.clear()
        self._player_row = None
        self.split.dismiss()

    def _load_player(self, card: Card) -> None:
        """Put the card's video in the player (only when the card changes, so playback isn't interrupted)."""
        self.media_pane.set_player_visible(True)
        if self._player_row == card.id:
            return
        from .. import previews
        self._player_row = card.id
        path = self.pipeline.video_path(card.row) if card.row.item.is_downloaded else None
        preview = previews.existing_preview(path) if path else None
        self.player.set_video(path, self.thumbs.cover(card.row.item), preview)

    def _show_card(self, card: Card) -> None:
        self.inspector.set_title("")
        self.split.reveal()
        asked = getattr(card, "pending_approvals", [])
        self.approval_bar.setVisible(bool(asked))
        if asked:
            self.approval_text.setText(f"⏳ Waiting for your approval: <b>{', '.join(asked)}</b>, asked by an AI "
                                       "assistant. Nothing happens until you decide.")
        self._load_player(card)
        self.panel.show_card(card, self.pipeline.youtube_accounts(self.profile.id),
                             self.pipeline.add_shorts_default_for(card.row))
        others = sorted(((c.id, c.title) for c in self.cards.values()), key=lambda t: t[1].lower())
        self.panel.set_ticket(others, self.pipeline.publishing_history(card.id))
        self.panel.set_platform_states(card.platform_states, card.post.publish_at if card.post else None)

    # ---------------------------------------------------------------- platform tags
    def _set_targets(self, row_id: int, platforms: list[str]) -> None:
        try:
            self.pipeline.set_targets(row_id, platforms)
        except PipelineError as exc:
            self.panel.show_errors(exc.problems)
            self.reload()
            return
        self.panel.show_errors([])

    def _mark_platform(self, row_id: int, platform: str, state: str, when, url: str = "", note: str = "") -> None:
        from ..handoff import check_post_url
        problem = check_post_url(platform, url)          # the same check as Prepare to Publish's pasted link
        if problem:
            QMessageBox.warning(self, "Mark as Posted", problem)
            return
        before = self.store.get_video(row_id).stage
        try:
            self.pipeline.set_platform_state(row_id, platform, state, when, url=(url or "").strip(), note=note)
        except PipelineError as exc:
            self.panel.show_errors(exc.problems)
            self.reload()
            return
        self.panel.show_errors([])
        after = self.store.get_video(row_id).stage
        if after != before:
            self._status(f"Moved to {self.stage_name(after)}: "
                         + ("every platform it's going to is live." if after == Stage.PUBLISHED else
                            "every platform it's going to is scheduled." if after == Stage.SCHEDULED else
                            "not every platform is scheduled any more."))

    def open_recovery(self, row_id: int) -> None:
        from .recovery_dialog import RecoveryDialog
        held = [d for d in self.store.dispatches_for(row_id) if d.status == "interrupted"]
        if not held:
            return
        dialog = RecoveryDialog(self.pipeline, held, self)
        dialog.exec()
        if dialog.resolved:
            self._status("Done. " + ", ".join({"retry": "sending again", "posted": "recorded as posted",
                                                "cancel": "cancelled"}[a] for _i, a in dialog.resolved) + ".")
        self.reload()

    # ---------------------------------------------------------------- a different sound for one platform
    def choose_sound(self, row_id: int, platform: str) -> None:
        """Pick a sound for one platform; its copy is made in the background (the ticket updates when ready)."""
        from ..models import Platform as P
        from ..platform_media import MIX, REPLACE, PlatformSound, build_soon
        from .ticket_widgets import PlatformSection
        from .video_widgets import SoundDialog
        row = self.store.get_video(row_id)
        master = self.pipeline.video_path(row) if row else None
        if master is None or not master.is_file():
            QMessageBox.information(self, "Different Sound", "Download the video first.")
            return
        label = P.LABELS.get(platform, platform)
        app = PlatformSection.IN_APP_SOUNDS.get(platform)
        note = (f"{label} posts with this sound; every other platform keeps its own. The original file isn't "
                "changed: Reelay makes a copy for " + label + " only.")
        if app:
            note += (f" Trending sounds from {app}'s library can't be added by any app: add those in {app} when you "
                     "post it (⋯ → Publish…).")
        dialog = SoundDialog(self, str(master.parent), title=f"Different Sound for {label}", note=note,
                             ok_text="Use This Sound")
        current = self.store.get_platform_sound(row_id, platform)
        if current is not None:
            dialog.preset(Path(current.audio_path) if Path(current.audio_path).is_file() else None,
                          current.volume_master if current.mode == MIX else 0.0, current.volume_audio)
        from PySide6.QtWidgets import QDialog
        if dialog.exec() != QDialog.Accepted or dialog.track is None:
            return
        opts = dialog.options()
        sound = PlatformSound(row_id, platform, str(dialog.track), MIX if opts["original_volume"] > 0 else REPLACE,
                              opts["original_volume"] or 0.25, opts["track_volume"],
                              current.output_path if current else None, None)
        self.store.save_platform_sound(sound)
        build_soon(self.store, sound, master)
        self._status(f"Making the {label} copy with {dialog.track.name}…")

    def remove_sound(self, row_id: int, platform: str) -> None:
        from ..models import Platform as P
        copy = self.store.clear_platform_sound(row_id, platform)
        if copy:
            Path(copy).unlink(missing_ok=True)              # only the platform's copy; never the original
        self._status(f"{P.LABELS.get(platform, platform)} posts with the ticket's own sound again.")

    def prepare_to_publish(self, row_id: int, platform: str) -> None:
        """The guided hand-off for a platform Reelay doesn't post to itself."""
        from .. import handoff
        from .handoff_dialog import HandoffDialog
        row = self.store.get_video(row_id)
        if row is None:
            return
        if self.panel.card is not None and self.panel.card.id == row_id and not self.panel._locked():
            post = self.panel.collect()           # what's on screen, saved first so nothing typed is lost
            try:
                self.pipeline.save_draft(post)
            except PipelineError as exc:
                self.panel.show_errors(exc.problems)
                self._status(str(exc))
                return
        else:
            post, _saved = self.pipeline.post_or_default(row)
        media = self.pipeline.media_info(row)              # the file's own length, when it's been inspected
        from PySide6.QtGui import QGuiApplication

        from ..video_tools import VideoToolError
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)   # its own sound: the copy may need making first
        try:
            video = self.pipeline.video_path_for(row, platform)
        except VideoToolError as exc:
            QMessageBox.warning(self, "Prepare to Publish", str(exc))
            return
        finally:
            QGuiApplication.restoreOverrideCursor()
        plan = handoff.prepare(post, platform, video,
                               (media.duration if media and media.duration else None) or row.item.duration)
        dialog = HandoffDialog(plan, self)
        if dialog.exec() != HandoffDialog.Accepted:
            if dialog.started:
                self._status(f"Not recorded yet. Open Publish… again when it's up on {dialog.label}.")
            return
        values = dialog.values()
        try:
            self.pipeline.confirm_handoff(row_id, platform, **values)
        except PipelineError as exc:
            QMessageBox.warning(self, "Prepare to Publish", str(exc))
            return
        self._status(f"Recorded: {'scheduled' if values['scheduled'] else 'live'} on {dialog.label}"
                     + (" with its link." if values["url"] else "."))
        self.reload()

    def _advance_due(self) -> None:
        """Every minute: hand-scheduled posts whose time has come count as live."""
        if self.profile and not self._closed:
            try:
                moved = self.pipeline.advance_stages(self.profile.id)
            except PipelineError:
                return
            if moved:
                self._status(f"{len(moved)} card{'s' if len(moved) != 1 else ''} moved on: their scheduled time "
                             "passed.")

    # ---------------------------------------------------------------- ticket fields & history
    def _commit_ticket_field(self, row_id: int, key: str, value) -> None:
        """A field was left (blur) or chosen: one logical commit, logged as a revision."""
        try:
            self.pipeline.update_ticket(row_id, {key: value})
        except PipelineError as exc:
            self.panel.show_errors(exc.problems)
            self._status(str(exc))
            return
        self.panel.show_errors([])

    def _mark_posted(self, row_id: int) -> None:
        from .ticket_widgets import MarkPostedDialog
        post = self.store.get_post(row_id)
        manual = [p for p in ("tiktok", "instagram") if not post or p not in post.target_platforms]
        dialog = MarkPostedDialog(self, default_platform=manual[0] if manual else "tiktok")
        if dialog.exec() != MarkPostedDialog.Accepted:
            return
        values = dialog.values()
        try:
            self.pipeline.mark_posted(row_id, **values)
        except PipelineError as exc:
            QMessageBox.warning(self, "Mark as Posted", str(exc))
            return
        self._status(f"Marked as posted to {values['platform'].capitalize().replace('Tiktok', 'TikTok')}.")
        self.reload()

    def _unmark_posted(self, manual_id: int) -> None:
        if QMessageBox.question(self, "Remove Record", "Remove this “posted by hand” record? The post itself "
                                                       "isn't touched.") == QMessageBox.Yes:
            self.pipeline.unmark_posted(manual_id)
            self.reload()

    def _show_revisions(self, row_id: int) -> None:
        from .ticket_widgets import RevisionDialog
        titles = {c.id: c.title for c in self.cards.values()}

        def restore(revision_id: int) -> str | None:
            try:
                self.pipeline.restore_revision(row_id, revision_id)
            except PipelineError as exc:
                return "\n".join(exc.problems)
            return None
        RevisionDialog(self.store.revisions(row_id), self, titles, on_restore=restore,
                       reload=lambda: self.store.revisions(row_id)).exec()

    def _on_select(self, column: BoardColumn, index) -> None:
        if not index.isValid() or len(column.selected_cards()) > 1:
            return
        card = column.model.cards[index.row()]
        if card.id == self.selected_id and self.panel.card is not None:
            return
        for other in self.columns.values():
            if other is not column:
                other.view.clearSelection()
        self.selected_id = card.id
        self._show_card(card)

    def _status(self, text: str) -> None:
        if text:
            self.statusMessage.emit(text)
            self.sync_label.setText(text)

    # ---------------------------------------------------------------- moves
    def _on_move(self, ids: list[int], target: str, insert_row: int) -> None:
        column = next((c for c in self.column_config if c.primary == target), None)
        for card_id in ids:
            card = self.cards.get(card_id)
            if card is None:
                continue
            try:
                if column is not None and card.row.stage in column.stages and card.row.stage != target:
                    self.pipeline.move(card_id, card.row.stage, insert_row)   # reordering within the column
                    continue
                if target == Stage.SCHEDULED and card.row.stage != Stage.SCHEDULED:
                    self._schedule_card(card)
                    return
                if target == Stage.PUBLISHED and card.row.stage != Stage.PUBLISHED:
                    if card.row.stage in (Stage.BACKLOG, Stage.STAGING) and not card.uploading:
                        answer = QMessageBox.question(
                            self, "Mark as Published",
                            f"Mark “{card.title}” as published?\n\nUse this for videos you've posted yourself "
                            "outside Reelay. Nothing is uploaded.")
                        if answer != QMessageBox.Yes:
                            continue
                self.pipeline.move(card_id, target, insert_row if card.row.stage == target else None)
                if card.row.stage != target:
                    self.selected_id = card_id
            except PipelineError as exc:
                self._status(str(exc))
        self.reload()

    def _schedule_card(self, card: Card) -> None:
        """Dropped on Scheduled: get it ready, then upload what's in the editor."""
        try:
            self.pipeline.prepare_upload(card.id)
        except PipelineError as exc:
            self._status(str(exc))
            return
        self.selected_id = card.id
        self.reload()
        post = self.panel.collect() if self.panel.card and self.panel.card.id == card.id \
            else self.store.get_post(card.id)
        self._request_upload(post, self.panel.shorts.isChecked() if self.panel.card
                             else self.pipeline.add_shorts_default_for(card.row))

    # ---------------------------------------------------------------- posts
    def _save_post(self, post: Post) -> None:
        try:
            self.pipeline.save_draft(post)
        except PipelineError as exc:
            self.panel.show_errors(exc.problems)   # nothing was saved; the text stays as typed
            self._status(str(exc))
            return
        self.panel.show_errors([])
        self._status("Draft saved.")
        self.reload()

    def _request_upload(self, post: Post, add_shorts: bool) -> None:
        if post is None or not self.profile:
            return
        self.selected_id = post.video_row
        problems = self.pipeline.upload_problems(post)
        if problems:
            self.pipeline.save_post(post)  # keep what was typed
            self.reload()
            self.panel.show_errors(problems)
            self._status("Fix the highlighted details, then upload again.")
            return
        force = False
        targets, _manual = self.pipeline.auto_targets(post)
        row = self.store.get_video(post.video_row)
        not_ready = [] if row is None or row.ready_gate_exempt else \
            self.pipeline.readiness(post.video_row, post).problems(targets)
        if not_ready:
            if not self._confirm_not_ready(not_ready):
                self.pipeline.save_post(post)  # keep what was typed
                self.reload()
                self.panel.show_errors(not_ready)
                self._status("Not scheduled: the ticket isn't ready yet.")
                return
            force = True
        if QMessageBox.question(self, "Schedule", self.pipeline.describe_upload(post)) != QMessageBox.Yes:
            self.pipeline.save_post(post)
            self.reload()
            return
        try:
            self.pipeline.start_upload(post, add_shorts, force=force)
        except PipelineError as exc:
            if exc.code == "ticket_not_ready" and self._confirm_not_ready(exc.problems):
                try:
                    self.pipeline.start_upload(post, add_shorts, force=True)
                except PipelineError as again:
                    self.panel.show_errors(again.problems)
                    return
            else:
                self.panel.show_errors(exc.problems)
                return
        self.panel.show_errors([])
        self._status("Scheduled." if Platform.YOUTUBE not in targets else "Uploading to YouTube…")
        self.reload()

    def _confirm_not_ready(self, problems: list[str]) -> bool:
        """The Ready gate in the app: show the checklist, schedule only on an explicit yes."""
        text = ("This ticket isn't ready to post automatically:\n\n" + "\n".join(f"• {p}" for p in problems)
                + "\n\nSchedule it anyway? Choose No to go back and fix it.")
        return QMessageBox.question(self, "Ticket Not Ready", text,
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def _on_upload_progress(self, row_id: int, pct: float) -> None:
        card = self.cards.get(row_id)
        if card is None:
            return
        card.progress, card.queued = pct, False
        self._column_for(card.row.stage).model.refresh_card(row_id)
        self.panel.set_progress(row_id, pct)

    @property
    def uploading(self) -> bool:
        return self.uploads.busy

    # ---------------------------------------------------------------- sync
    def sync(self, force: bool = False) -> None:
        """Ask YouTube what happened to scheduled uploads."""
        if not self.profile or (self.sync_worker and self.sync_worker.isRunning()):
            return
        if not force and time.time() - self._last_sync < SYNC_EVERY_SECONDS:
            return
        plan = self.pipeline.sync_plan(self.profile.id)
        for problem in plan.problems:
            self._status(problem)
        if not plan.jobs:
            if force and not plan.problems:
                self._status("Nothing scheduled to sync.")
            return
        self._last_sync = time.time()
        self.sync_btn.setEnabled(False)
        self._status("Syncing with YouTube…")
        self.sync_worker = CallWorker(self.pipeline.fetch_remote, plan, parent=self)
        self.sync_worker.succeeded.connect(lambda remote: self._apply_sync(plan, remote))
        self.sync_worker.failed.connect(lambda msg: self._status(f"Sync failed: {msg}"))
        self.sync_worker.finished.connect(lambda: self.sync_btn.setEnabled(True))
        self.sync_worker.start()

    def _apply_sync(self, plan, remote: dict) -> None:
        published = self.pipeline.apply_sync(plan, remote)
        self._status(f"Synced {clock(datetime.now())}" + (f" · {published} went live" if published else ""))
        self.reload()

    def shutdown(self) -> None:
        if getattr(self, "_bridge", None) is not None:
            self._bridge.changed.disconnect(self._on_store_change)
            self._bridge = None
        self._closed = True
        self._reload_timer.stop()
        self._advance_timer.stop()
        self.panel.video_tools.shutdown()
        self.player.shutdown()
        if self.sync_worker:
            self.sync_worker.wait(3000)
