"""Table model, filter proxy and cover delegate for the video list."""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRectF, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from ..models import Status, VideoItem
from ..naming import DEFAULT_TEMPLATE, assign_filenames
from . import theme
from .thumbnails import ThumbnailCache


class Col(IntEnum):
    CHECK = 0
    COVER = 1
    DATE = 2
    HOOK = 3
    LENGTH = 4
    VIEWS = 5
    CUSTOM = 6
    FILENAME = 7
    STATUS = 8


HEADERS = {
    Col.CHECK: "", Col.COVER: "", Col.DATE: "DATE", Col.HOOK: "CAPTION",
    Col.LENGTH: "LENGTH", Col.VIEWS: "VIEWS", Col.CUSTOM: "YOUR NAME",
    Col.FILENAME: "FILE NAME", Col.STATUS: "STATUS",
}
HEADER_TIPS = {
    Col.CHECK: "Selected videos download when you click Download. Click a box to select or unselect a video.",
    Col.COVER: "Cover image",
    Col.DATE: "When the video was first posted",
    Col.HOOK: "The start of the video's caption",
    Col.LENGTH: "How long the video is",
    Col.VIEWS: "Views on the original platform",
    Col.CUSTOM: "Your own short name for the video. Double-click a cell (or use the side panel) to set it.",
    Col.FILENAME: "What the file is (or will be) called on disk",
    Col.STATUS: "Whether the video will download, is downloading, is done, or failed",
}
SORT_ROLE = Qt.UserRole + 1
COVER_W, COVER_H = 34, 60
ROW_HEIGHT = 72


def fmt_date(yyyymmdd: str) -> str:
    try:
        d = datetime.strptime(yyyymmdd, "%Y%m%d")
        return f"{d:%b} {d.day}, {d.year}"
    except (ValueError, TypeError):
        return "—"


def short_date(yyyymmdd: str) -> str:
    """'Oct 2' this year, 'Oct 2, 2025' otherwise."""
    try:
        d = datetime.strptime(yyyymmdd, "%Y%m%d")
    except (ValueError, TypeError):
        return ""
    return f"{d:%b} {d.day}" + ("" if d.year == datetime.now().year else f", {d.year}")


def fmt_duration(seconds) -> str:
    if not seconds:
        return "—"
    s = int(round(float(seconds)))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def fmt_count(n) -> str:
    if n is None:
        return "—"
    for div, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= div:
            v = n / div
            return (f"{v:.0f}" if v >= 100 else f"{v:.1f}".rstrip("0").rstrip(".")) + suffix
    return str(n)


class VideoTableModel(QAbstractTableModel):
    checksChanged = Signal()
    namesChanged = Signal()           # template or a custom name changed
    customNameEdited = Signal(str)    # key

    def __init__(self, thumbs: ThumbnailCache, parent=None):
        super().__init__(parent)
        self.thumbs = thumbs
        self._items: list[VideoItem] = []
        self._rows: dict[str, int] = {}
        self._names: dict[str, str] = {}
        self._template = DEFAULT_TEMPLATE
        thumbs.loaded.connect(self._on_cover_loaded)

    # ---------------------------------------------------------------- items
    def set_items(self, items: list[VideoItem]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self._rows = {item.key: row for row, item in enumerate(self._items)}
        self._recompute_names()
        self.endResetModel()
        self.checksChanged.emit()

    def items(self) -> list[VideoItem]:
        return self._items

    def item(self, row: int) -> VideoItem:
        return self._items[row]

    def find(self, key: str) -> VideoItem | None:
        row = self._rows.get(key)
        return self._items[row] if row is not None else None

    def refresh(self, key: str, columns: tuple[Col, ...] | None = None) -> None:
        row = self._rows.get(key)
        if row is None:
            return
        first, last = (min(columns), max(columns)) if columns else (0, len(Col) - 1)
        self.dataChanged.emit(self.index(row, first), self.index(row, last))

    def refresh_all(self) -> None:
        self._recompute_names()
        if self._items:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._items) - 1, len(Col) - 1))

    # ---------------------------------------------------------------- naming
    @property
    def template(self) -> str:
        return self._template

    def set_template(self, template: str) -> None:
        self._template = template or DEFAULT_TEMPLATE
        self._names_changed()

    def planned_filename(self, item: VideoItem) -> str:
        return self._names.get(item.key, "")

    def _recompute_names(self) -> None:
        self._names = assign_filenames(self._items, self._template)

    def _names_changed(self) -> None:
        self._recompute_names()
        if self._items:
            self.dataChanged.emit(self.index(0, Col.CUSTOM), self.index(len(self._items) - 1, Col.STATUS))
        self.namesChanged.emit()

    def set_custom_names(self, names: dict[str, str]) -> None:
        for key, name in names.items():
            item = self.find(key)
            if item is not None:
                item.custom_name = name.strip()
                self.customNameEdited.emit(key)
        self._names_changed()

    # ---------------------------------------------------------------- checks
    def set_checked(self, keys, checked: bool) -> None:
        for key in keys:
            item = self.find(key)
            if item is not None and not item.is_downloaded:
                item.checked = checked
        if self._items:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._items) - 1, len(Col) - 1))
        self.checksChanged.emit()

    def checked_items(self) -> list[VideoItem]:
        return [i for i in self._items if i.checked and not i.is_downloaded]

    # ---------------------------------------------------------------- Qt API
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(Col)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return HEADERS[Col(section)]
        if role == Qt.ToolTipRole:
            return HEADER_TIPS.get(Col(section))
        if role == Qt.TextAlignmentRole and section in (Col.LENGTH, Col.VIEWS):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        item = self._items[index.row()]
        if index.column() == Col.CHECK and not item.is_downloaded:
            f |= Qt.ItemIsUserCheckable
        if index.column() == Col.CUSTOM:
            f |= Qt.ItemIsEditable
        return f

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        item = self._items[index.row()]
        col = Col(index.column())

        if role == Qt.CheckStateRole and col == Col.CHECK and not item.is_downloaded:
            return Qt.Checked if item.checked else Qt.Unchecked
        if role == Qt.DisplayRole:
            return self._display(item, col)
        if role == Qt.EditRole and col == Col.CUSTOM:
            return item.custom_name
        if role == SORT_ROLE:
            return self._sort_value(item, col)
        if role == Qt.ToolTipRole:
            return self._tooltip(item, col)
        if role == Qt.ForegroundRole:
            return self._foreground(item, col)
        if role == Qt.TextAlignmentRole:
            if col in (Col.LENGTH, Col.VIEWS):
                return int(Qt.AlignRight | Qt.AlignVCenter)
            if col == Col.CHECK:
                return int(Qt.AlignCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if not index.isValid():
            return False
        item = self._items[index.row()]
        if index.column() == Col.CHECK and role == Qt.CheckStateRole:
            item.checked = Qt.CheckState(value) == Qt.Checked
            self.dataChanged.emit(index.siblingAtColumn(0), index.siblingAtColumn(len(Col) - 1))
            self.checksChanged.emit()
            return True
        if index.column() == Col.CUSTOM and role == Qt.EditRole:
            item.custom_name = str(value or "").strip()
            self.customNameEdited.emit(item.key)
            self._names_changed()
            return True
        return False

    # ---------------------------------------------------------------- cells
    def _display(self, item: VideoItem, col: Col):
        if col == Col.DATE:
            return short_date(item.upload_date) if item.upload_date else ("…" if item.status != Status.DOWNLOADED else "—")
        if col == Col.HOOK:
            return " ".join(item.hook.split())
        if col == Col.LENGTH:
            return fmt_duration(item.duration)
        if col == Col.VIEWS:
            return fmt_count(item.view_count)
        if col == Col.CUSTOM:
            return item.custom_name
        if col == Col.FILENAME:
            return self.planned_filename(item)
        if col == Col.STATUS:
            return self._status_text(item)
        return None

    def _status_text(self, item: VideoItem) -> str:
        if item.status == Status.DOWNLOADED:
            planned = self.planned_filename(item)
            return "✓ Name will change" if planned and planned != item.filename else "✓ Downloaded"
        if item.status == Status.DOWNLOADING:
            return f"{item.progress:.0f}%" if item.progress < 100 else "Finalizing"
        if item.status == Status.QUEUED:
            return "Queued"
        if item.status == Status.FAILED:
            return "Failed"
        if item.availability in ("private", "scheduled"):
            return "Private on YouTube" if item.availability == "private" else "Scheduled on YouTube"
        return "Will download" if item.checked else "Not selected"

    def _sort_value(self, item: VideoItem, col: Col):
        return {
            Col.CHECK: (0 if item.is_downloaded else 2 if item.checked else 1),
            Col.DATE: item.upload_date or "",
            Col.HOOK: item.hook.lower(),
            Col.LENGTH: float(item.duration or 0),
            Col.VIEWS: int(item.view_count or 0),
            Col.CUSTOM: item.custom_name.lower(),
            Col.FILENAME: self.planned_filename(item).lower(),
            Col.STATUS: item.status,
        }.get(col, "")

    def _tooltip(self, item: VideoItem, col: Col):
        if col == Col.CHECK:
            if item.is_downloaded:
                return "Already downloaded. It's on the Board, in Backlog."
            return ("Selected: downloads when you click Download. Click to unselect." if item.checked else
                    "Not selected. Click to select it for downloading.")
        if col == Col.COVER:
            return "Click the row to see this video's caption, details and file name on the right."
        if col == Col.HOOK:
            return item.hook
        if col == Col.DATE and item.upload_date:
            return fmt_date(item.upload_date)
        if col == Col.STATUS and item.status == Status.FAILED:
            return item.error
        if col == Col.STATUS and item.availability in ("private", "scheduled") and not item.is_downloaded:
            return ("Only you can see this video. Downloading it needs “Use browser sign-in” set to a browser "
                    "signed into this YouTube channel.")
        if col == Col.FILENAME and item.is_downloaded and self.planned_filename(item) != item.filename:
            return f"Currently: {item.filename}\nClick “Rename Files…” to rename it."
        if col == Col.CUSTOM and not item.custom_name:
            return "Double-click to give this video your own name"
        if col == Col.STATUS:
            if item.status == Status.DOWNLOADED:
                return "Downloaded. Find it on the Board, in Backlog."
            if item.status in (Status.DOWNLOADING, Status.QUEUED):
                return "Downloading now, or waiting its turn."
            return ("Selected: downloads when you click Download." if item.checked else
                    "Not selected, so it won't download. Tick its box or click Select All.")
        if col == Col.VIEWS:
            return "Views on the original platform when the list was made"
        if col == Col.LENGTH:
            return "How long the video is"
        return None

    def _foreground(self, item: VideoItem, col: Col):
        if col == Col.STATUS and item.status == Status.FAILED:
            return QColor(theme.P.danger)
        if not item.checked and not item.is_downloaded and col not in (Col.CHECK, Col.STATUS):
            return QColor(theme.P.text_muted)
        if col in (Col.DATE, Col.LENGTH, Col.VIEWS, Col.FILENAME, Col.STATUS):
            if col == Col.STATUS and item.status == Status.DOWNLOADING:
                return QColor(theme.P.text_primary)
            return QColor(theme.P.text_secondary)
        return QColor(theme.P.text_primary)

    def _on_cover_loaded(self, key: str) -> None:
        self.refresh(key, (Col.COVER,))


class VideoFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._query = ""
        self._tag = ""
        self._product_ids: set | None = None
        self.setSortRole(SORT_ROLE)
        self.setDynamicSortFilter(False)  # don't reshuffle rows while downloading

    def set_query(self, text: str) -> None:
        self._query = text.strip().lower()
        self.invalidateFilter()

    def set_tag(self, tag: str | None) -> None:
        """Only videos with this hashtag (no #); empty shows every video."""
        self._tag = (tag or "").lstrip("#").lower()
        self.invalidateFilter()

    def set_product(self, product_id: str | None) -> None:
        """Only videos tagged with this product; empty shows every video."""
        self.set_product_ids({product_id} if product_id else None)

    def set_product_ids(self, ids: set | None) -> None:
        """Only videos tagged with one of these products (a brand's, or one); None = all."""
        self._product_ids = set(ids) if ids else None
        self.invalidateFilter()

    @property
    def filtering(self) -> bool:
        return bool(self._query or self._tag or self._product_ids)

    def filterAcceptsRow(self, source_row: int, source_parent) -> bool:
        item = self.sourceModel().item(source_row)
        if self._tag and self._tag not in item.hashtags:
            return False
        if self._product_ids and not any(p.get("id") in self._product_ids for p in item.products or []):
            return False
        if not self._query:
            return True
        products = " ".join(f"{p.get('name', '')} {p.get('title', '')}" for p in item.products or [])
        haystack = " ".join([item.hook, item.description, item.custom_name,
                             self.sourceModel().planned_filename(item), item.video_id, products]).lower()
        return all(word in haystack for word in self._query.split())


class CoverDelegate(QStyledItemDelegate):
    """Draws a rounded, center-cropped cover thumbnail."""

    def __init__(self, model: VideoTableModel, proxy: VideoFilterProxy, parent=None):
        super().__init__(parent)
        self.model, self.proxy = model, proxy

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        widget = option.widget
        style = widget.style() if widget else None
        if style:
            style.drawPrimitive(QStyle.PE_PanelItemViewItem, opt, painter, widget)

        item = self.model.item(self.proxy.mapToSource(index).row())
        r = option.rect
        box = QRectF(r.x() + (r.width() - COVER_W) / 2, r.y() + (r.height() - COVER_H) / 2, COVER_W, COVER_H)
        path = QPainterPath()
        path.addRoundedRect(box, 5, 5)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setClipPath(path)
        ratio = widget.devicePixelRatioF() if widget else 1.0
        pix = self.model.thumbs.thumb(item, COVER_W, COVER_H, ratio)
        if pix is not None:
            painter.drawPixmap(box.topLeft(), pix)
        else:
            painter.fillPath(path, QColor(theme.P.bg_raised))
        if not item.checked and not item.is_downloaded:
            painter.fillPath(path, QColor(0, 0, 0, 120))  # dim skipped videos
        painter.restore()
