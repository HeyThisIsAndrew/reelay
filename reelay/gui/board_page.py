"""Board page: a Kanban pipeline (Backlog → Staging → Scheduled → Published)
with drag-and-drop, a post editor, YouTube uploads and status sync."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime

from PySide6.QtCore import (QAbstractListModel, QMimeData, QModelIndex, QRect, QRectF, QSize, Qt, QTimer,
                            Signal)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QLabel, QListView, QMessageBox,
                               QPushButton, QSplitter, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

from ..models import Post, PostStatus, Profile, Stage
from ..pipeline import Card, Pipeline, PipelineError
from ..store import Change
from ..text import clock, local_text
from ..uploads import UploadManager
from . import theme
from .bridge import change_bridge
from .post_panel import PostPanel
from .table_model import fmt_duration, short_date
from .thumbnails import ThumbnailCache
from .workers import CallWorker

log = logging.getLogger("reelay")

MIME = "application/x-reelay-cards"
CARD_HEIGHT = 112
COVER_W, COVER_H = 52, 92
SYNC_EVERY_SECONDS = 600


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
            return card.row.item.hook
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


def _two_lines(fm: QFontMetrics, text: str, width: int) -> list[str]:
    words, line1 = text.split(), ""
    while words and fm.horizontalAdvance((line1 + " " + words[0]).strip()) <= width:
        line1 = (line1 + " " + words.pop(0)).strip()
    if not line1 and words:  # one very long word
        return [fm.elidedText(text, Qt.ElideRight, width)]
    rest = " ".join(words)
    return [line1] + ([fm.elidedText(rest, Qt.ElideRight, width)] if rest else [])


class CardDelegate(QStyledItemDelegate):
    def __init__(self, thumbs: ThumbnailCache, parent=None):
        super().__init__(parent)
        self.thumbs = thumbs

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), CARD_HEIGHT)

    def paint(self, painter: QPainter, option, index) -> None:
        card: Card = index.data(Qt.UserRole)
        p = theme.P
        r = QRectF(option.rect).adjusted(2, 4, -2, -4)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(r, 9, 9)
        painter.fillPath(path, QColor(p.hover if (selected or hovered) else p.raised))
        painter.setPen(QPen(QColor(p.text3 if selected else p.border), 1))
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
            painter.fillPath(cpath, QColor(p.pressed))
        painter.restore()

        # text
        x = int(cover.right() + 12)
        w = int(r.right() - 10 - x)
        y = int(r.y() + 12)
        title_font = QFont(option.font)
        title_font.setWeight(QFont.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(p.text))
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
        painter.setPen(QColor(p.text2))
        painter.drawText(QRect(x, y + 4, w, sfm.height()), Qt.AlignLeft | Qt.AlignVCenter,
                         sfm.elidedText(meta, Qt.ElideRight, w))

        status, color = self._status(card)
        painter.setPen(QColor(color))
        painter.drawText(QRect(x, int(r.bottom()) - sfm.height() - 10, w, sfm.height()),
                         Qt.AlignLeft | Qt.AlignVCenter, sfm.elidedText(status, Qt.ElideRight, w))
        if card.uploading and card.progress is not None and not card.queued:
            bar = QRectF(x, r.bottom() - 7, w, 3)
            painter.fillRect(bar, QColor(p.pressed))
            painter.fillRect(QRectF(bar.x(), bar.y(), bar.width() * card.progress / 100, 3), QColor(p.text))
        painter.restore()

    @staticmethod
    def _status(card: Card) -> tuple[str, str]:
        p, post, item = theme.P, card.post, card.row.item
        if len(card.dispatches) > 1 and not (post and post.status == PostStatus.UPLOADING):
            marks = {"pending": "⏱", "dispatching": "…", "scheduled": "⏱", "published": "✓", "failed": "⚠"}
            short = {"youtube": "YT", "bluesky": "Bsky", "threads": "Thr", "facebook": "FB"}
            text = "  ".join(f"{short.get(d.platform, d.platform)} {marks.get(d.status, '?')}" for d in card.dispatches)
            failed = any(d.status == "failed" for d in card.dispatches)
            return text, p.danger if failed else (p.success if post and post.status == PostStatus.PUBLISHED else p.text)
        if post and post.status == PostStatus.UPLOADING:
            return ("Waiting to upload…" if card.queued else f"Uploading {(card.progress or 0):.0f}%"), p.text
        if post and post.status == PostStatus.SCHEDULED:
            if post.error:
                return "⚠ Still private on YouTube", p.danger
            return f"Goes live {local_text(post.publish_at)}", p.text
        if post and post.status == PostStatus.PUBLISHED:
            return f"Live on {card.account.display_name}" if card.account else "Live on YouTube", p.success
        if card.row.stage == Stage.PUBLISHED:
            return "Posted outside Reelay", p.text2
        if post and post.status == PostStatus.FAILED:
            return f"⚠ {post.error}", p.danger
        if not item.is_downloaded:
            return "⚠ Video file missing", p.danger
        if card.row.stage == Stage.STAGING:
            if post and post.account_id and card.account:
                when = f" · {local_text(post.publish_at)}" if post.publish_at else ""
                return f"Draft for {card.account.display_name}{when}", p.text2
            return "Draft · pick a channel", p.text2
        return "Ready to stage", p.text3


class BoardColumn(QFrame):
    def __init__(self, stage: str, thumbs: ThumbnailCache, parent=None):
        super().__init__(parent)
        self.setObjectName("BoardColumn")
        self.stage = stage
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 12, 10, 6)
        lay.setSpacing(6)
        head = QHBoxLayout()
        title = QLabel(Stage.LABELS[stage].upper())
        title.setProperty("section", True)
        self.count = QLabel("0")
        self.count.setProperty("faint", True)
        head.addWidget(title)
        head.addStretch()
        head.addWidget(self.count)
        lay.addLayout(head)
        self.model = BoardModel(stage, self)
        self.view = QListView(objectName="BoardList")
        self.view.setModel(self.model)
        self.view.setItemDelegate(CardDelegate(thumbs, self.view))
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setDragDropMode(QAbstractItemView.DragDrop)
        self.view.setDefaultDropAction(Qt.MoveAction)
        self.view.setDragEnabled(True)
        self.view.setAcceptDrops(True)
        self.view.setDropIndicatorShown(True)
        self.view.setMouseTracking(True)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.setUniformItemSizes(True)
        self.view.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.empty = QLabel({
            Stage.BACKLOG: "Downloaded videos land here.",
            Stage.STAGING: "Drag videos here to write their post.",
            Stage.SCHEDULED: "Drop a ready post here to upload and schedule it.",
            Stage.PUBLISHED: "Live videos show up here.",
        }[stage])
        self.empty.setProperty("faint", True)
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.empty)

    def set_cards(self, cards: list[Card]) -> None:
        self.model.set_cards(cards)
        self.count.setText(str(len(cards)))
        self.empty.setVisible(not cards)


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
        self.sync_worker: CallWorker | None = None
        self._last_sync = 0.0
        self._closed = False
        self._reload_timer = QTimer(self, singleShot=True, interval=30, timeout=self.reload)
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
        title = QLabel("Board", objectName="PageTitle")
        self.subtitle = QLabel()
        self.subtitle.setProperty("muted", True)
        tv = QVBoxLayout()
        tv.setSpacing(2)
        tv.addWidget(title)
        tv.addWidget(self.subtitle)
        head.addLayout(tv)
        head.addStretch()
        self.sync_label = QLabel()
        self.sync_label.setProperty("faint", True)
        head.addWidget(self.sync_label)
        self.sync_btn = QPushButton("Sync with YouTube")
        self.sync_btn.clicked.connect(lambda: self.sync(force=True))
        head.addWidget(self.sync_btn)
        outer.addLayout(head)

        cols = QWidget()
        cl = QHBoxLayout(cols)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(10)
        self.columns: dict[str, BoardColumn] = {}
        for stage in Stage.ALL:
            col = BoardColumn(stage, self.thumbs)
            col.model.moveRequested.connect(self._on_move)
            col.view.selectionModel().currentChanged.connect(
                lambda cur, _prev, c=col: self._on_select(c, cur))
            col.view.clicked.connect(lambda idx, c=col: self._on_select(c, idx))
            self.columns[stage] = col
            cl.addWidget(col, 1)

        self.panel = PostPanel(self.thumbs)
        self.panel.set_enabled_platforms(self.pipeline.enabled_platforms())
        self.panel.saveRequested.connect(self._save_post)
        self.panel.uploadRequested.connect(self._request_upload)
        self.panel.cancelUploadRequested.connect(self.pipeline.cancel_upload)
        self.panel.connectRequested.connect(self.openAccounts.emit)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(cols)
        split.addWidget(self.panel)
        split.setStretchFactor(0, 1)
        split.setSizes([1000, 360])
        split.setChildrenCollapsible(False)
        outer.addWidget(split, 1)
        self.thumbs.loaded.connect(lambda _k: [c.view.viewport().update() for c in self.columns.values()])

    # ---------------------------------------------------------------- data
    def set_profile(self, profile: Profile) -> None:
        self.profile = profile
        self.selected_id = None
        self.panel.show_empty()
        self.subtitle.setText(f"{profile.name}'s pipeline · drag cards to move them along")
        self._last_sync = 0.0
        self.reload()

    def _on_store_change(self, change: Change) -> None:
        if change.entity in ("video", "post", "account", "dispatch") and self.profile and \
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
        self.cards = {card.id: card for cards in columns.values() for card in cards}
        for stage, col in self.columns.items():
            col.set_cards(columns[stage])
        self._reselect()

    def _reselect(self) -> None:
        card = self.cards.get(self.selected_id) if self.selected_id else None
        if card is None:
            self.panel.show_empty()
            return
        col = self.columns[card.row.stage]
        for row, c in enumerate(col.model.cards):
            if c.id == card.id:
                col.view.setCurrentIndex(col.model.index(row))
        self._show_card(card)

    def _show_card(self, card: Card) -> None:
        self.panel.show_card(card, self.pipeline.youtube_accounts(self.profile.id),
                             self.pipeline.add_shorts_default())

    def _on_select(self, column: BoardColumn, index) -> None:
        if not index.isValid():
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
        for card_id in ids:
            card = self.cards.get(card_id)
            if card is None:
                continue
            try:
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
                             else self.pipeline.add_shorts_default())

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
        if QMessageBox.question(self, "Upload to YouTube", self.pipeline.describe_upload(post)) != QMessageBox.Yes:
            self.pipeline.save_post(post)
            self.reload()
            return
        try:
            self.pipeline.start_upload(post, add_shorts)
        except PipelineError as exc:
            self.panel.show_errors(exc.problems)
            return
        self._status("Uploading to YouTube…")
        self.reload()

    def _on_upload_progress(self, row_id: int, pct: float) -> None:
        card = self.cards.get(row_id)
        if card is None:
            return
        card.progress, card.queued = pct, False
        self.columns[card.row.stage].model.refresh_card(row_id)
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
        if self.sync_worker:
            self.sync_worker.wait(3000)
