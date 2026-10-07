"""Right-hand panel: everything about the selected video, plus its custom name."""

from __future__ import annotations

import html
import re
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
                               QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from ..models import Status, VideoItem
from ..text import short_title
from . import theme
from .table_model import fmt_count, fmt_date, fmt_duration
from .thumbnails import ThumbnailCache

COVER_SIZE = QSize(198, 352)  # 9:16


def reveal_in_file_manager(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", str(path)], check=False)
    elif sys.platform == "win32":
        subprocess.run(["explorer", "/select,", str(path)], check=False)
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))


def _label(text: str = "", *, prop: str | None = None, wrap: bool = True, select: bool = False) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(wrap)
    if prop:
        lab.setProperty(prop, True)
    if select:
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lab


class CoverView(QLabel):
    def __init__(self):
        super().__init__()
        self.setFixedSize(COVER_SIZE)
        self._pix: QPixmap | None = None

    def set_cover(self, pix: QPixmap | None) -> None:
        self._pix = pix
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 10, 10)
        p.setClipPath(path)
        p.fillPath(path, QColor(theme.P.raised))
        if self._pix and not self._pix.isNull():
            ratio = self.devicePixelRatioF()
            scaled = self._pix.scaled(self.size() * ratio, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            scaled.setDevicePixelRatio(ratio)
            x = (self.width() - scaled.width() / ratio) / 2
            y = (self.height() - scaled.height() / ratio) / 2
            p.drawPixmap(int(x), int(y), scaled)
        else:
            p.setPen(QColor(theme.P.text3))
            p.drawText(self.rect(), Qt.AlignCenter, "No cover")


class DetailPanel(QFrame):
    customNameEdited = Signal(str, str)   # key, name
    checkSelected = Signal(bool)          # for multi-selection
    autoNameSelected = Signal()

    def __init__(self, thumbs: ThumbnailCache, parent=None):
        super().__init__(parent)
        self.setObjectName("DetailFrame")
        self.thumbs = thumbs
        self.item: VideoItem | None = None
        self.folder: Path | None = None
        self.setMinimumWidth(300)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_empty())
        self.stack.addWidget(self._build_single())
        self.stack.addWidget(self._build_multi())
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.stack)
        thumbs.loaded.connect(self._on_cover_loaded)

    # ---------------------------------------------------------------- pages
    def _build_empty(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addStretch()
        t = _label("No video selected", prop="muted")
        t.setAlignment(Qt.AlignCenter)
        s = _label("Click a video to see its caption, stats and naming options.", prop="faint")
        s.setAlignment(Qt.AlignCenter)
        lay.addWidget(t)
        lay.addWidget(s)
        lay.addStretch()
        lay.setContentsMargins(24, 24, 24, 24)
        return w

    def _build_single(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setObjectName("DetailScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("DetailBody")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(6)

        self.cover = CoverView()
        lay.addWidget(self.cover, 0, Qt.AlignHCenter)
        lay.addSpacing(10)

        self.title = _label(select=True)
        self.title.setObjectName("DetailTitle")
        self.meta = _label(prop="muted")
        lay.addWidget(self.title)
        lay.addWidget(self.meta)

        lay.addWidget(_label("FILE NAME", prop="section"))
        self.custom = QLineEdit()
        self.custom.setPlaceholderText("Custom name, e.g. letting-go-of-who-you-were")
        self.custom.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.custom.textEdited.connect(self._emit_custom)
        lay.addWidget(self.custom)
        self.filename = _label(select=True)
        self.filename.setObjectName("FilenamePreview")
        lay.addWidget(self.filename)
        self.file_note = _label(prop="faint")
        lay.addWidget(self.file_note)

        lay.addWidget(_label("CAPTION", prop="section"))
        self.caption = _label(select=True)
        lay.addWidget(self.caption)

        self.tags_header = _label("HASHTAGS", prop="section")
        self.tags = _label(prop="muted", select=True)
        lay.addWidget(self.tags_header)
        lay.addWidget(self.tags)

        lay.addWidget(_label("DETAILS", prop="section"))
        self.details = _label(prop="muted", select=True)
        self.details.setTextFormat(Qt.RichText)
        lay.addWidget(self.details)

        lay.addSpacing(8)
        self.open_btn = QPushButton("Open Original Post")
        self.open_btn.clicked.connect(lambda: self.item and QDesktopServices.openUrl(QUrl(self.item.url)))
        self.play_btn = QPushButton("Play")
        self.play_btn.clicked.connect(self._play)
        self.reveal_btn = QPushButton("Show in Finder" if theme.IS_MAC else "Show in Explorer")
        self.reveal_btn.clicked.connect(self._reveal)
        row = QHBoxLayout()
        for b in (self.play_btn, self.reveal_btn, self.open_btn):
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        row.addWidget(self.play_btn)
        row.addWidget(self.reveal_btn)
        lay.addLayout(row)
        lay.addWidget(self.open_btn)
        lay.addStretch()
        scroll.setWidget(body)
        return scroll

    def _build_multi(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(20, 24, 20, 20)
        lay.setSpacing(8)
        self.multi_title = _label()
        self.multi_title.setObjectName("DetailTitle")
        self.multi_meta = _label(prop="muted")
        lay.addWidget(self.multi_title)
        lay.addWidget(self.multi_meta)
        lay.addSpacing(8)
        for text, slot in (("Include in Download", lambda: self.checkSelected.emit(True)),
                           ("Exclude from Download", lambda: self.checkSelected.emit(False)),
                           ("Name from Captions", self.autoNameSelected.emit)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            lay.addWidget(b)
        hint = _label("Tip: press Space to include or exclude the selected videos.", prop="faint")
        lay.addSpacing(6)
        lay.addWidget(hint)
        lay.addStretch()
        return w

    # ---------------------------------------------------------------- public
    def show_empty(self) -> None:
        self.item = None
        self.stack.setCurrentIndex(0)

    def show_multi(self, items: list[VideoItem]) -> None:
        self.item = None
        total = sum(float(i.duration or 0) for i in items)
        downloaded = sum(1 for i in items if i.is_downloaded)
        self.multi_title.setText(f"{len(items)} videos selected")
        parts = [f"{fmt_duration(total)} total"]
        if downloaded:
            parts.append(f"{downloaded} already downloaded")
        self.multi_meta.setText(" · ".join(parts))
        self.stack.setCurrentIndex(2)

    def show_item(self, item: VideoItem, planned_filename: str) -> None:
        same = self.item is not None and self.item.key == item.key
        self.item = item
        self.stack.setCurrentIndex(1)
        self.cover.set_cover(self.thumbs.cover(item))
        self.title.setText(short_title(item.hook))

        bits = [fmt_date(item.upload_date) if item.upload_date else None,
                fmt_duration(item.duration) if item.duration else None,
                f"{fmt_count(item.view_count)} views" if item.view_count is not None else None,
                f"{fmt_count(item.like_count)} likes" if item.like_count is not None else None]
        self.meta.setText(" · ".join(b for b in bits if b) or "Loading details…")

        if not same or not self.custom.hasFocus():
            self.custom.setText(item.custom_name)
        self.update_filename(planned_filename)

        caption = (item.description or "").strip()
        self.caption.setText(caption or "No caption.")
        tags = item.hashtags or [t.lower() for t in item.tags]
        self.tags.setText("  ".join(f"#{t}" for t in tags))
        self.tags_header.setVisible(bool(tags))
        self.tags.setVisible(bool(tags))

        rows = [("Platform", item.platform), ("Video ID", item.video_id),
                ("Creator", item.uploader or "—"),
                ("Comments", fmt_count(item.comment_count)),
                ("Resolution", f"{item.width}×{item.height}" if item.width and item.height else "—")]
        if item.is_downloaded:
            rows.append(("Watermark", "None" if item.watermark_free is not False else "Possible"))
        self.details.setText("<table cellspacing='0' cellpadding='2'>" + "".join(
            f"<tr><td style='color:{theme.P.text3}; padding-right:14px'>{k}</td><td>{html.escape(str(v))}</td></tr>"
            for k, v in rows) + "</table>")

        downloaded = item.is_downloaded and self._video_path() is not None
        self.play_btn.setEnabled(downloaded)
        self.reveal_btn.setEnabled(downloaded)

    def update_filename(self, planned: str) -> None:
        item = self.item
        if item is None:
            return
        # zero-width spaces let long names wrap at _ - . instead of widening the panel
        self.filename.setText(re.sub(r"([_\-.])", "\\1\u200b", planned) if planned else "—")
        if item.is_downloaded and planned != item.filename:
            self.file_note.setText(f"Currently “{item.filename}”. Click Apply Names to rename it.")
        elif item.is_downloaded:
            self.file_note.setText("Downloaded with this name.")
        elif item.status == Status.FAILED:
            self.file_note.setText(f"Last attempt failed: {item.error}")
        else:
            self.file_note.setText("The video will be saved with this name.")

    # ---------------------------------------------------------------- internals
    def _emit_custom(self, text: str) -> None:
        if self.item is not None:
            self.customNameEdited.emit(self.item.key, text)

    def _video_path(self) -> Path | None:
        path = self.item.path(self.folder) if self.item else None
        return path if path is not None and path.is_file() else None

    def _play(self) -> None:
        path = self._video_path()
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _reveal(self) -> None:
        path = self._video_path()
        if path:
            reveal_in_file_manager(path)

    def _on_cover_loaded(self, key: str) -> None:
        if self.item is not None and self.item.key == key:
            self.cover.set_cover(self.thumbs.cover(self.item))
