"""Board page: a Kanban pipeline (Backlog → Staging → Scheduled → Published)
with drag-and-drop, a post editor, YouTube uploads and status sync."""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from datetime import datetime, timezone

from PySide6.QtCore import (QAbstractListModel, QMimeData, QModelIndex, QRect, QRectF, QSize, Qt, QTimer,
                            Signal)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QLabel, QListView, QMessageBox,
                               QPushButton, QSplitter, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

from ..accounts import YOUTUBE, AccountService
from ..models import Post, PostStatus, Profile, Stage
from ..publishing import build_resource, default_post, reconcile, stage_after_upload, validate_post
from ..store import Store
from ..youtube import YouTubeError, watch_url
from . import theme
from .post_panel import Card, PostPanel, clock, local_text
from .table_model import fmt_duration, short_date
from .theme import set_prop
from .thumbnails import ThumbnailCache
from .workers import CallWorker, UploadWorker

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
    openAccounts = Signal()
    statusMessage = Signal(str)

    def __init__(self, store: Store, accounts: AccountService, thumbs: ThumbnailCache, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.store, self.accounts, self.thumbs = store, accounts, thumbs
        self.profile: Profile | None = None
        self.cards: dict[int, Card] = {}
        self.selected_id: int | None = None
        self.upload_worker: UploadWorker | None = None
        self.upload_queue: deque[tuple[int, object, object, dict]] = deque()
        self.sync_worker: CallWorker | None = None
        self._last_sync = 0.0
        self._build_ui()

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
        self.panel.saveRequested.connect(self._save_post)
        self.panel.uploadRequested.connect(self._request_upload)
        self.panel.cancelUploadRequested.connect(self._cancel_upload)
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

    def youtube_accounts(self):
        return self.store.list_accounts(self.profile.id, YOUTUBE) if self.profile else []

    def reload(self) -> None:
        if not self.profile:
            return
        accounts = {a.id: a for a in self.store.list_accounts(self.profile.id)}
        folder = self.profile.output_folder
        by_stage: dict[str, list[Card]] = {s: [] for s in Stage.ALL}
        old = self.cards
        self.cards = {}
        for row in self.store.load_videos(self.profile.id):
            row.item.refresh_download_status(folder)
            if row.stage == Stage.BACKLOG and not row.item.is_downloaded:
                continue  # fetched but not downloaded: lives on the Download page only
            post = self.store.get_post(row.id)
            card = Card(row=row, post=post, account=accounts.get(post.account_id) if post else None)
            prev = old.get(row.id)
            if prev is not None and post and post.status == PostStatus.UPLOADING:
                card.progress, card.queued = prev.progress, prev.queued
            self.cards[row.id] = card
            by_stage[row.stage].append(card)
        for stage, col in self.columns.items():
            col.set_cards(by_stage[stage])
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
        self.panel.show_card(card, self.youtube_accounts(), self._add_shorts_default())

    def _add_shorts_default(self) -> bool:
        return self.store.get_setting("add_shorts", "1") == "1"

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
        self.panel.show_card(card, self.youtube_accounts(), self._add_shorts_default())

    def _status(self, text: str) -> None:
        self.statusMessage.emit(text)
        self.sync_label.setText(text)

    # ---------------------------------------------------------------- moves
    def _ensure_post(self, card: Card) -> Post:
        if card.post is None:
            accounts = self.youtube_accounts()
            card.post = self.store.save_post(default_post(card.id, card.row.item,
                                                          accounts[0].id if accounts else None))
        return card.post

    def _on_move(self, ids: list[int], target: str, insert_row: int) -> None:
        for card_id in ids:
            card = self.cards.get(card_id)
            if card is None:
                continue
            source = card.row.stage
            if source == target:
                order = [c.id for c in self.columns[target].model.cards if c.id != card_id]
                order.insert(min(insert_row, len(order)), card_id)
                self.store.reorder(order)
                continue
            if source in (Stage.SCHEDULED, Stage.PUBLISHED) or card.uploading:
                self._status("Scheduled and published videos are managed on YouTube; they can't be moved back.")
                continue
            if target in (Stage.BACKLOG, Stage.STAGING):
                if target == Stage.STAGING:
                    self._ensure_post(card)
                self.store.set_stage([card_id], target)
                self.selected_id = card_id
            elif target == Stage.SCHEDULED:
                self._ensure_post(card)
                if source == Stage.BACKLOG:
                    self.store.set_stage([card_id], Stage.STAGING)
                self.selected_id = card_id
                self.reload()
                self._request_upload(self.panel.collect() if self.panel.card else card.post,
                                     self.panel.shorts.isChecked() if self.panel.card else self._add_shorts_default())
                return
            elif target == Stage.PUBLISHED:
                answer = QMessageBox.question(
                    self, "Mark as Published",
                    f"Mark “{card.title}” as published?\n\nUse this for videos you've posted yourself "
                    "outside Reelay. Nothing is uploaded.")
                if answer == QMessageBox.Yes:
                    self.store.set_stage([card_id], Stage.PUBLISHED)
                    self.selected_id = card_id
        self.reload()

    # ---------------------------------------------------------------- posts
    def _save_post(self, post: Post) -> None:
        card = self.cards.get(post.video_row)
        if card is None:
            return
        post.status, post.error = PostStatus.DRAFT, ""
        card.post = self.store.save_post(post)
        if card.row.stage == Stage.BACKLOG:
            self.store.set_stage([card.id], Stage.STAGING)
        self._status("Draft saved.")
        self.reload()

    def _request_upload(self, post: Post, add_shorts: bool) -> None:
        card = self.cards.get(post.video_row)
        if card is None or not self.profile:
            return
        path = card.row.item.path(self.profile.output_folder)
        errors = validate_post(post, path)
        if errors:
            post.status = PostStatus.DRAFT
            card.post = self.store.save_post(post)
            self.selected_id = card.id
            self.reload()
            self.panel.show_errors(errors)
            self._status("Fix the highlighted details, then upload again.")
            return
        account = self.store.get_account(post.account_id)
        try:
            client = self.accounts.client_for(account)
        except YouTubeError as exc:
            self.panel.show_errors([str(exc)])
            return
        when = f"goes live {local_text(post.publish_at)}" if post.publish_at else \
            f"is published immediately as {post.privacy.capitalize()}"
        answer = QMessageBox.question(
            self, "Upload to YouTube",
            f"Upload “{post.title}” to {account.display_name}?\n\nIt {when}.")
        if answer != QMessageBox.Yes:
            card.post = self.store.save_post(post)
            self.reload()
            return
        post.status, post.error = PostStatus.UPLOADING, ""
        card.post = self.store.save_post(post)
        self.store.set_stage([card.id], Stage.SCHEDULED)
        self.selected_id = card.id
        self.upload_queue.append((card.id, client, path, build_resource(post, add_shorts)))
        card.progress, card.queued = 0.0, bool(self.upload_worker)
        self.reload()
        self._next_upload()

    def _next_upload(self) -> None:
        if self.upload_worker is not None or not self.upload_queue:
            return
        row_id, client, path, resource = self.upload_queue.popleft()
        if row_id in self.cards:
            self.cards[row_id].queued = False
        worker = UploadWorker(row_id, client, path, resource, self)
        worker.progress.connect(self._on_upload_progress)
        worker.done.connect(self._on_upload_done)
        worker.failed.connect(self._on_upload_failed)
        worker.finished.connect(self._on_upload_finished)
        self.upload_worker = worker
        worker.start()
        self._status("Uploading to YouTube…")

    def _on_upload_progress(self, row_id: int, pct: float) -> None:
        card = self.cards.get(row_id)
        if card is None:
            return
        card.progress = pct
        self.columns[card.row.stage].model.refresh_card(row_id)
        self.panel.set_progress(row_id, pct)

    def _on_upload_done(self, row_id: int, resource: dict) -> None:
        post = self.store.get_post(row_id)
        if post is None:
            return
        post.remote_id = resource.get("id", "")
        post.remote_url = watch_url(post.remote_id) if post.remote_id else ""
        post.status = PostStatus.SCHEDULED if post.publish_at else PostStatus.PUBLISHED
        post.error = ""
        self.store.save_post(post)
        self.store.set_stage([row_id], stage_after_upload(post))
        self._status(f"Uploaded “{post.title}”." + (f" It goes live {local_text(post.publish_at)}."
                                                     if post.publish_at else ""))
        self.reload()

    def _on_upload_failed(self, row_id: int, reason: str) -> None:
        post = self.store.get_post(row_id)
        if post is not None:
            post.status, post.error = PostStatus.FAILED, reason
            self.store.save_post(post)
            self.store.set_stage([row_id], Stage.STAGING)
        self._status(f"Upload failed: {reason}")
        self.reload()

    def _on_upload_finished(self) -> None:
        self.upload_worker = None
        self._next_upload()

    def _cancel_upload(self, row_id: int) -> None:
        for i, queued in enumerate(self.upload_queue):
            if queued[0] == row_id:
                del self.upload_queue[i]
                self._on_upload_failed(row_id, "Upload cancelled.")
                return
        if self.upload_worker is not None and self.upload_worker.row_id == row_id:
            self.upload_worker.cancel()

    @property
    def uploading(self) -> bool:
        return self.upload_worker is not None or bool(self.upload_queue)

    # ---------------------------------------------------------------- sync
    def sync(self, force: bool = False) -> None:
        """Ask YouTube what happened to scheduled uploads."""
        if not self.profile or (self.sync_worker and self.sync_worker.isRunning()):
            return
        if not force and time.time() - self._last_sync < SYNC_EVERY_SECONDS:
            return
        posts = [p for p in self.store.posts_with_status(self.profile.id, [PostStatus.SCHEDULED]) if p.remote_id]
        if not posts:
            if force:
                self._status("Nothing scheduled to sync.")
            return
        jobs = []
        for account_id in {p.account_id for p in posts}:
            account = self.store.get_account(account_id) if account_id else None
            if account is None:
                continue
            try:
                client = self.accounts.client_for(account)
            except YouTubeError as exc:
                self._status(str(exc))
                continue
            jobs.append((client, [p.remote_id for p in posts if p.account_id == account_id]))
        if not jobs:
            return

        def fetch():
            out = {}
            for client, ids in jobs:
                statuses = client.video_statuses(ids)
                out.update({vid: statuses.get(vid) for vid in ids})
            return out

        self._last_sync = time.time()
        self.sync_btn.setEnabled(False)
        self._status("Syncing with YouTube…")
        self.sync_worker = CallWorker(fetch, parent=self)
        self.sync_worker.succeeded.connect(lambda result: self._apply_sync(posts, result))
        self.sync_worker.failed.connect(lambda msg: self._status(f"Sync failed: {msg}"))
        self.sync_worker.finished.connect(lambda: self.sync_btn.setEnabled(True))
        self.sync_worker.start()

    def _apply_sync(self, posts: list[Post], remote: dict) -> None:
        now = datetime.now(timezone.utc)
        published = 0
        for post in posts:
            status, note = reconcile(post, remote.get(post.remote_id), now)
            if status != post.status or note != post.error:
                post.status, post.error = status, note
                self.store.save_post(post)
            if status == PostStatus.PUBLISHED:
                self.store.set_stage([post.video_row], Stage.PUBLISHED)
                published += 1
        self._status(f"Synced {clock(datetime.now())}" + (f" · {published} went live" if published else ""))
        self.reload()

    def shutdown(self) -> None:
        self.upload_queue.clear()
        if self.upload_worker is not None:
            self.upload_worker.cancel()
            self.upload_worker.wait(5000)
