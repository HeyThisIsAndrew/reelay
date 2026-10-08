"""Board page: a Kanban pipeline (Backlog → Staging → Scheduled → Published)
with drag-and-drop, a post editor, YouTube uploads and status sync."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime

from PySide6.QtCore import (QAbstractListModel, QItemSelectionModel, QMimeData, QModelIndex, QRect, QRectF, QSize,
                            Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QLabel, QListView, QMenu,
                               QMessageBox, QPushButton, QSplitter, QStackedWidget, QStyle, QStyledItemDelegate,
                               QVBoxLayout, QWidget)

from ..models import Platform, Post, PostStatus, Profile, Stage
from ..pipeline import Card, Pipeline, PipelineError
from ..store import Change
from ..text import clock, local_text
from ..uploads import UploadManager
from . import theme
from .theme import set_prop
from .board_tools import ArchivedDialog, BulkPanel, ImportDialog
from .bridge import change_bridge
from .post_panel import PostPanel
from .table_model import fmt_duration, short_date
from .thumbnails import ThumbnailCache
from .tips import tip
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
        readiness = card.readiness
        if readiness is not None and readiness.blocking:
            names = ", ".join(Platform.LABELS.get(n, n) for n in readiness.blocking)
            return (f"⚠ Not ready for {names}" if readiness.ready else f"⚠ NOT READY · {names}"), p.danger
        if card.row.stage == Stage.STAGING:
            if post and post.account_id and card.account:
                when = f" · {local_text(post.publish_at)}" if post.publish_at else ""
                return f"✓ Ready · {card.account.display_name}{when}", p.text2
            return "✓ Ready · pick a channel", p.text2
        return "✓ Ready · not started", p.text3


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
        title.setToolTip({
            Stage.BACKLOG: "Videos you've downloaded and haven't started on yet",
            Stage.STAGING: "Videos you're writing posts for",
            Stage.SCHEDULED: "Videos handed to platforms, waiting to go live",
            Stage.PUBLISHED: "Videos that are live",
        }[stage])
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
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
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

    def set_cards(self, cards: list[Card], total: int | None = None) -> None:
        self.model.set_cards(cards)
        filtered = total is not None and total != len(cards)
        self.count.setText(f"{len(cards)} of {total}" if filtered else str(len(cards)))
        self.count.setToolTip(f"{len(cards)} shown by the filter, {total} in this column" if filtered else "")
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
        self.sync_worker: CallWorker | None = None
        self.task_worker: CallWorker | None = None
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
        self.filter_box = tip(QComboBox(), "Show only tickets that are ready, not ready, or blocked on a platform")
        for key, label in (("all", "Show: All tickets"), ("ready", "Show: Ready"), ("not_ready", "Show: Not ready"),
                           *((f"blocking:{p}", f"Show: Blocking {Platform.LABELS[p]}") for p in Platform.ALL)):
            self.filter_box.addItem(label, key)
        self.filter_box.currentIndexChanged.connect(self._on_filter)
        head.addWidget(self.filter_box)
        self.import_btn = tip(QPushButton("Import Sheet…"), "Fill tickets from a spreadsheet (CSV file or a Google "
                                                            "Sheets link), with a preview before anything changes")
        self.import_btn.clicked.connect(self.open_import)
        head.addWidget(self.import_btn)
        self.archived_btn = tip(QPushButton("Archived…"), "Videos you've archived. Bring them back from here.")
        set_prop(self.archived_btn, "flat")
        self.archived_btn.clicked.connect(self.open_archived)
        head.addWidget(self.archived_btn)
        self.sync_btn = QPushButton("Check YouTube for Updates")
        self.sync_btn.setToolTip("Ask YouTube whether scheduled videos have gone live, and move them to Published")
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
            col.view.selectionModel().selectionChanged.connect(lambda *_a, c=col: self._on_selection(c))
            col.view.customContextMenuRequested.connect(lambda pos, c=col: self._card_menu(c, pos))
            self.columns[stage] = col
            cl.addWidget(col, 1)

        self.panel = PostPanel(self.thumbs)
        self.panel.set_enabled_platforms(self.pipeline.enabled_platforms())
        self.panel.saveRequested.connect(self._save_post)
        self.panel.uploadRequested.connect(self._request_upload)
        self.panel.cancelUploadRequested.connect(self.pipeline.cancel_upload)
        self.panel.connectRequested.connect(self.openAccounts.emit)
        self.panel.readiness_for = lambda post: self.pipeline.readiness(post.video_row, post)
        self.panel.ticketFieldCommitted.connect(self._commit_ticket_field)
        self.panel.markPostedRequested.connect(self._mark_posted)
        self.panel.unmarkRequested.connect(self._unmark_posted)
        self.panel.revisionsRequested.connect(self._show_revisions)
        self.panel.fillRequested.connect(lambda row_id: self._fill([row_id]))
        self.panel.transcriptRequested.connect(lambda row_id: self._get_transcripts([row_id], ask_replace=True))
        self.bulk = BulkPanel()
        self.bulk.applyRequested.connect(self._bulk_apply)
        self.bulk.fillRequested.connect(lambda: self._fill(self.bulk_ids))
        self.bulk.transcriptsRequested.connect(lambda: self._get_transcripts(self.bulk_ids))
        self.bulk.archiveRequested.connect(lambda: self._archive(self.bulk_ids))
        self.side = QStackedWidget()
        self.side.addWidget(self.panel)
        self.side.addWidget(self.bulk)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(cols)
        split.addWidget(self.side)
        split.setStretchFactor(0, 1)
        split.setSizes([960, 400])
        split.setChildrenCollapsible(False)
        outer.addWidget(split, 1)
        self.thumbs.loaded.connect(lambda _k: [c.view.viewport().update() for c in self.columns.values()])

    # ---------------------------------------------------------------- data
    def set_profile(self, profile: Profile) -> None:
        self.profile = profile
        self.selected_id = None
        self.bulk_ids = []
        self.side.setCurrentWidget(self.panel)
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
            shown = [c for c in columns[stage] if self._matches(c)]
            col.set_cards(shown, total=len(columns[stage]))
        if self.bulk_ids:
            self._reselect_bulk()
        else:
            self._reselect()

    # ---------------------------------------------------------------- filter
    def _matches(self, card: Card) -> bool:
        r = card.readiness
        if self.filter == "all" or r is None:
            return True
        if self.filter == "ready":
            return r.ready and not r.blocking
        if self.filter == "not_ready":
            return bool(r.blocking)
        return self.filter.split(":", 1)[1] in r.blocking

    def _on_filter(self) -> None:
        self.filter = self.filter_box.currentData()
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
            self.side.setCurrentWidget(self.bulk)
        elif self.bulk_ids:
            self.bulk_ids = []
            self.side.setCurrentWidget(self.panel)
            if chosen:
                self.selected_id = None
                self._on_select(column, column.model.index(column.model.cards.index(chosen[0])))

    def _reselect_bulk(self) -> None:
        cards = [self.cards[i] for i in self.bulk_ids if i in self.cards]
        stages = {c.row.stage for c in cards}
        if len(cards) < 2 or len(stages) != 1:
            self.bulk_ids = []
            self.side.setCurrentWidget(self.panel)
            self.panel.show_empty()
            return
        col = self.columns[stages.pop()]
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
        self.side.setCurrentWidget(self.panel)
        self._status(f"Archived {n} video{'s' if n > 1 else ''}.")
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
        others = sorted(((c.id, c.title) for c in self.cards.values()), key=lambda t: t[1].lower())
        self.panel.set_ticket(others, self.pipeline.publishing_history(card.id))

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
