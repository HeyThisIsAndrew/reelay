"""Right-hand panel: everything about the selected video, plus its custom name.

Top to bottom: the title; Details (every known fact once, as label and value
rows: platform with its logo, creator, date, length, views, likes, comments,
resolution); what the post says (caption, hashtags, tagged products); then the
file (its file label, the file name) and what to do with it."""

from __future__ import annotations

import html
import re
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy,
                               QStackedWidget, QVBoxLayout, QWidget)

from ..models import Status, VideoItem
from ..platform_specs import by_label
from ..text import short_title
from . import theme
from .tips import tip
from .table_model import fmt_count, fmt_date, fmt_duration
from .thumbnails import ThumbnailCache
from .widgets import label, scroll_page

COVER_SIZE = QSize(198, 352)  # 9:16


def reveal_in_file_manager(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", str(path)], check=False)
    elif sys.platform == "win32":
        subprocess.run(["explorer", "/select,", str(path)], check=False)
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))


def _label(text: str = "", *, prop: str | None = None, wrap: bool = True, select: bool = False) -> QLabel:
    if prop == "section":
        from .theme import heading
        text = heading(text)
    return label(text, prop, wrap=wrap, select=select)


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
        p.fillPath(path, QColor(theme.P.bg_raised))
        if self._pix and not self._pix.isNull():
            ratio = self.devicePixelRatioF()
            scaled = self._pix.scaled(self.size() * ratio, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            scaled.setDevicePixelRatio(ratio)
            x = (self.width() - scaled.width() / ratio) / 2
            y = (self.height() - scaled.height() / ratio) / 2
            p.drawPixmap(int(x), int(y), scaled)
        else:
            p.setPen(QColor(theme.P.text_muted))
            p.drawText(self.rect(), Qt.AlignCenter, "No cover")


class DetailPanel(QFrame):
    customNameEdited = Signal(str, str)   # key, name
    checkSelected = Signal(bool)          # for multi-selection
    autoNameSelected = Signal()
    tagClicked = Signal(str)              # a hashtag, without #
    productClicked = Signal(str)          # a tagged product's id

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
        scroll, body, lay = scroll_page((20, 20, 20, 20), 6, name="DetailScroll", body_name="DetailBody",
                                        sideways=False)

        self.cover = CoverView()
        lay.addWidget(self.cover, 0, Qt.AlignHCenter)
        lay.addSpacing(10)

        self.title = _label(select=True)
        self.title.setObjectName("DetailTitle")
        self.meta = _label("Loading details…", prop="faint")    # only while the full details are on their way
        lay.addWidget(self.title)
        lay.addWidget(self.meta)

        # ---- the facts, each once: label and value rows; unknown ones are left out
        lay.addSpacing(6)
        lay.addWidget(tip(_label("DETAILS", prop="section"), "Facts about the original post"))
        self.facts = QWidget(objectName="DetailFacts")
        self.facts_grid = QGridLayout(self.facts)
        self.facts_grid.setContentsMargins(0, 0, 0, 0)
        self.facts_grid.setHorizontalSpacing(14)
        self.facts_grid.setVerticalSpacing(5)
        self.facts_grid.setColumnStretch(1, 1)
        lay.addWidget(self.facts)
        self.fact_values: dict[str, QLabel] = {}          # label → its value, for the rows on show

        # ---- what the post says
        lay.addSpacing(6)
        lay.addWidget(tip(_label("CAPTION", prop="section"), "The caption from the original post"))
        self.caption = _label(select=True)
        lay.addWidget(self.caption)

        self.tags_header = tip(_label("HASHTAGS", prop="section"),
                               "Hashtags found in the caption. Click one to show only videos with it.")
        self.tags = _label(prop="muted")
        self.tags.setTextFormat(Qt.RichText)
        self.tags.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        self.tags.linkActivated.connect(lambda href: self.tagClicked.emit(href.removeprefix("tag:")))
        tip(self.tags, "Click a hashtag to show only videos with it")
        lay.addWidget(self.tags_header)
        lay.addWidget(self.tags)

        self.products_header = tip(_label("TAGGED PRODUCTS", prop="section"),
                                   "Products tagged on this post (TikTok Shop). Click one to show every video "
                                   "tagged with it.")
        self.products = _label(prop="muted")
        self.products.setTextFormat(Qt.RichText)
        self.products.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        self.products.linkActivated.connect(lambda href: self.productClicked.emit(href.removeprefix("product:")))
        tip(self.products, "Click a product to show every video tagged with it")
        lay.addWidget(self.products_header)
        lay.addWidget(self.products)

        # ---- the file: its label, and what to do with it
        lay.addSpacing(6)
        lay.addWidget(tip(_label("FILE LABEL (OPTIONAL)", prop="section"),
                          "Optional custom label used when renaming files on disk: it goes into the file name when "
                          "Name files includes the file label."))
        self.custom = QLineEdit()
        self.custom.setPlaceholderText("e.g. letting-go-of-who-you-were")
        tip(self.custom, "Type a short name for this video. Saved automatically.")
        self.custom.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.custom.textEdited.connect(self._emit_custom)
        lay.addWidget(self.custom)
        self.filename = _label(select=True)
        self.filename.setObjectName("FilenamePreview")
        tip(self.filename, "The file name this video has (or will have) on disk")
        lay.addWidget(self.filename)
        self.file_note = _label(prop="faint")
        lay.addWidget(self.file_note)

        lay.addSpacing(8)
        self.open_btn = tip(QPushButton("Open Original Post"), "Open the video on the platform it came from")
        self.open_btn.clicked.connect(lambda: self.item and QDesktopServices.openUrl(QUrl(self.item.url)))
        self.play_btn = tip(QPushButton("Play"), "Play the downloaded file in your video player")
        self.play_btn.clicked.connect(self._play)
        self.reveal_btn = tip(QPushButton("Show in Finder" if theme.IS_MAC else "Show in Explorer"),
                              "Show the downloaded file in its folder")
        self.reveal_btn.clicked.connect(self._reveal)
        row = QHBoxLayout()
        for b in (self.play_btn, self.reveal_btn, self.open_btn):
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        row.addWidget(self.play_btn)
        row.addWidget(self.reveal_btn)
        lay.addLayout(row)
        lay.addWidget(self.open_btn)
        lay.addStretch()
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
        for text, slot, hint_text in (
                ("Tick for Download", lambda: self.checkSelected.emit(True), "Download these when you click Download"),
                ("Untick", lambda: self.checkSelected.emit(False), "Leave these out of the download"),
                ("Suggest Names from Captions", self.autoNameSelected.emit,
                 "Name each video from the first words of its caption")):
            b = tip(QPushButton(text), hint_text)
            b.clicked.connect(slot)
            lay.addWidget(b)
        hint = _label("Tip: press Space to tick or untick the highlighted videos.", prop="faint")
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

        self.meta.setVisible(not item.has_details)
        if not same or not self.custom.hasFocus():
            self.custom.setText(item.custom_name)
        self.update_filename(planned_filename)

        caption = (item.description or "").strip()
        self.caption.setText(caption or "No caption.")
        tags = item.hashtags or [t.lower() for t in item.tags]
        self.tags.setText("&nbsp;&nbsp;".join(f"<a href='tag:{html.escape(t)}' style='text-decoration:none'>#"
                                              f"{html.escape(t)}</a>" for t in tags))
        self.tags_header.setVisible(bool(tags))
        self.tags.setVisible(bool(tags))
        from ..products import video_products
        found = video_products(item)
        self.products.setText("<br>".join(f"• <a href='product:{html.escape(p.id)}' style='text-decoration:none'>"
                                          f"{html.escape(p.label)}</a>" for p in found))
        self.products_header.setVisible(bool(found))
        self.products.setVisible(bool(found))

        self._set_facts(item)

        downloaded = item.is_downloaded and self._video_path() is not None
        self.play_btn.setEnabled(downloaded)
        self.reveal_btn.setEnabled(downloaded)

    @staticmethod
    def facts_for(item: VideoItem) -> list[tuple[str, str]]:
        """(label, value) for every fact that's known, in reading order. Nothing unknown is shown as a dash."""
        from .cards import resolution_label
        if item.width and item.height:
            label = resolution_label(item)
            resolution = f"{item.width}×{item.height}" + (f" · {label}" if label else "")
        else:            # listed, not downloaded: platforms often only say once there's a file
            resolution = "" if item.is_downloaded else "Known after download"
        spec = by_label(item.platform)
        rows = [("Platform", spec.label if spec else item.platform),
                ("Creator", item.uploader),
                ("Posted", fmt_date(item.upload_date) if item.upload_date else ""),
                ("Length", fmt_duration(item.duration) if item.duration else ""),
                ("Views", fmt_count(item.view_count) if item.view_count is not None else ""),
                ("Likes", fmt_count(item.like_count) if item.like_count is not None else ""),
                ("Comments", fmt_count(item.comment_count) if item.comment_count is not None else ""),
                ("Resolution", resolution),
                ("Watermark", ("None" if item.watermark_free is not False else "Possible") if item.is_downloaded
                 else ""),
                ("Video ID", item.video_id)]
        return [(k, str(v)) for k, v in rows if v not in (None, "", "—")]

    def _set_facts(self, item: VideoItem) -> None:
        from .logos import platform_pixmap
        grid = self.facts_grid
        while grid.count():
            w = grid.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        self.fact_values = {}
        spec = by_label(item.platform)
        for row, (key, value) in enumerate(self.facts_for(item)):
            grid.addWidget(_label(key, prop="faint", wrap=False), row, 0, Qt.AlignLeft | Qt.AlignTop)
            text = _label(value, select=True)
            if key == "Platform":            # the platform's logo beside its name
                holder = QWidget()
                hl = QHBoxLayout(holder)
                hl.setContentsMargins(0, 0, 0, 0)
                hl.setSpacing(6)
                logo = QLabel()
                logo.setPixmap(platform_pixmap(spec.key if spec else item.platform.lower(), 16))
                logo.setFixedSize(16, 16)
                hl.addWidget(logo)
                hl.addWidget(text, 1)
                grid.addWidget(holder, row, 1)
            else:
                grid.addWidget(text, row, 1)
            self.fact_values[key] = text

    def update_filename(self, planned: str) -> None:
        item = self.item
        if item is None:
            return
        # zero-width spaces let long names wrap at _ - . instead of widening the panel
        self.filename.setText(re.sub(r"([_\-.])", "\\1\u200b", planned) if planned else "—")
        if item.is_downloaded and planned != item.filename:
            self.file_note.setText(f"Currently “{item.filename}”. Click Rename Files… to rename it.")
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
