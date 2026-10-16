"""Small reusable widgets: the friendly file-naming picker, the growing search box, and the building
blocks every page and dialog used to write out for itself (a label, a search field, a read-only table,
a scrolling page body). One definition each, so a change to how they look or behave lands everywhere."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics, QKeyEvent
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPlainTextEdit, QScrollArea, QSizePolicy, QTableWidget, QVBoxLayout, QWidget)

from ..models import VideoItem
from ..naming import DEFAULT_TEMPLATE, PRESETS, TOKENS, render_stem
from .tips import tip

CUSTOM = "__custom__"


# ---------------------------------------------------------------------- building blocks
def label(text: str = "", prop: str | None = None, *, name: str | None = None, wrap: bool = True,
          rich: bool = False, links: bool = False, select: bool = False) -> QLabel:
    """A label. ``prop`` is its style role ("muted", "faint", "danger", …, see app.qss); ``rich`` forces rich
    text; ``links`` makes its links open in the browser (and implies rich); ``select`` lets the reader select
    and copy the text."""
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    if rich or links:
        lab.setTextFormat(Qt.RichText)
    if links:
        lab.setOpenExternalLinks(True)
    if select:
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
    if prop:
        lab.setProperty(prop, True)
    return lab


def search_field(placeholder: str, hint: str, *, name: str = "SearchField", width: int | None = None,
                 min_width: int | None = None) -> QLineEdit:
    """The one-line search box: a magnifier at the start, a clear button at the end, and a tip."""
    from . import icons
    field = QLineEdit(objectName=name)
    field.setPlaceholderText(placeholder)
    field.setClearButtonEnabled(True)
    field.setAttribute(Qt.WA_MacShowFocusRect, False)
    field.addAction(icons.icon("search", 16), QLineEdit.LeadingPosition)
    if width:
        field.setFixedWidth(width)
    if min_width:
        field.setMinimumWidth(min_width)
    return tip(field, hint)


def table(headers: list[str], *, rows: int = 0, select: str = "rows", grid: bool = True,
          stretch: tuple[int, ...] | None = None, fit: tuple[int, ...] = ()) -> QTableWidget:
    """A read-only table with column headings and no row numbers. ``select``: "rows" (one or more rows by
    clicking), "extended" (shift/cmd too) or "none". Columns in ``fit`` size to their contents; the rest
    (or just ``stretch``) share the width."""
    t = QTableWidget(rows, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide()
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setShowGrid(grid)
    if select == "none":
        t.setSelectionMode(QAbstractItemView.NoSelection)
    else:
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        if select == "extended":
            t.setSelectionMode(QAbstractItemView.ExtendedSelection)
    hh = t.horizontalHeader()
    for col in range(len(headers)):
        if col in fit:
            hh.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        elif stretch is None or col in stretch:
            hh.setSectionResizeMode(col, QHeaderView.Stretch)
    return t


class _VerticalBody(QWidget):
    """A scroll-area body that never insists on a minimum width, so QScrollArea shrinks it to the viewport
    width and lets children wrap instead of clipping their right edge."""

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, super().minimumSizeHint().height())


def scroll_page(margins: tuple[int, int, int, int] | None = None, spacing: int | None = None, *,
                name: str | None = None, body_name: str | None = None, frameless: bool = False,
                sideways: bool = True) -> tuple[QScrollArea, QWidget, QVBoxLayout]:
    """A vertically scrolling body: the scroll area, the widget inside it and that widget's layout.
    ``sideways=False`` never scrolls sideways (a side panel wraps its text instead)."""
    scroll = QScrollArea()
    if name:
        scroll.setObjectName(name)
    scroll.setWidgetResizable(True)
    if frameless:
        scroll.setFrameShape(QFrame.NoFrame)
    if not sideways:
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    body = QWidget() if sideways else _VerticalBody()
    if body_name:
        body.setObjectName(body_name)
    lay = QVBoxLayout(body)
    if margins is not None:                     # else the style's own margins, as a plain layout has
        lay.setContentsMargins(*margins)
    if spacing is not None:
        lay.setSpacing(spacing)
    scroll.setWidget(body)
    return scroll, body, lay


def _sample_item() -> VideoItem:
    return VideoItem(platform="TikTok", video_id="7692175258450677023", url="", custom_name="letting-go",
                     upload_date=date.today().strftime("%Y%m%d"), uploader="livingood.lifestyle",
                     description="the loneliness in the middle of who you've been")


class NamingPicker(QWidget):
    """How downloaded files are named: a plain-language choice plus a live
    example. Typing a pattern ({date}_{custom} …) is tucked behind "Custom pattern…"."""

    templateChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        lay = QHBoxLayout()
        lay.setSpacing(8)
        outer.addLayout(lay)
        self.combo = QComboBox()
        for label, template in PRESETS:
            self.combo.addItem(label, template)
        self.combo.addItem("Custom pattern…", CUSTOM)
        tip(self.combo, "How downloaded video files are named. The example on the right shows the result.")
        lay.addWidget(self.combo)
        self.pattern = QLineEdit()
        self.pattern.setPlaceholderText("{date}_{custom}")
        self.pattern.setMinimumWidth(170)
        self.pattern.setAttribute(Qt.WA_MacShowFocusRect, False)
        tip(self.pattern, "For power users: build a name from pieces. "
                          + "  ".join(f"{{{k}}} = {v}" for k, v in TOKENS.items()))
        self.pattern.hide()
        lay.addWidget(self.pattern)
        self.example = QLabel()
        self.example.setProperty("faint", True)
        self.example.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.example.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.example.setMinimumWidth(120)
        self.example.setContentsMargins(2, 0, 0, 0)
        tip(self.example, "What a file will be called with this choice, using a highlighted video "
                          "(or a sample when none is highlighted).")
        lay.addStretch(1)
        outer.addWidget(self.example)
        self._item: VideoItem | None = None
        self._loading = False
        self.combo.currentIndexChanged.connect(self._on_choice)
        self.pattern.textEdited.connect(self._on_pattern)

    def template(self) -> str:
        data = self.combo.currentData()
        if data == CUSTOM:
            return self.pattern.text().strip() or DEFAULT_TEMPLATE
        return data or DEFAULT_TEMPLATE

    def set_template(self, template: str) -> None:
        """Select the matching preset, or show the pattern box for anything else."""
        template = (template or DEFAULT_TEMPLATE).strip()
        self._loading = True
        index = self.combo.findData(template)
        if index >= 0:
            self.combo.setCurrentIndex(index)
            self.pattern.hide()
        else:
            self.combo.setCurrentIndex(self.combo.findData(CUSTOM))
            self.pattern.setText(template)
            self.pattern.show()
        self._loading = False
        self._update_example()
        self.templateChanged.emit(self.template())

    def set_example_item(self, item: VideoItem | None) -> None:
        self._item = item
        self._update_example()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_example()

    def _on_choice(self) -> None:
        custom = self.combo.currentData() == CUSTOM
        self.pattern.setVisible(custom)
        if custom and not self.pattern.text():
            self.pattern.setText(PRESETS[0][1])
        if custom:
            self.pattern.setFocus()
        self._changed()

    def _on_pattern(self, _text: str) -> None:
        self._changed()

    def _changed(self) -> None:
        self._update_example()
        if not self._loading:
            self.templateChanged.emit(self.template())

    def _update_example(self) -> None:
        name = render_stem(self.template(), self._item or _sample_item(), 1) + ".mp4"
        metrics = QFontMetrics(self.example.font())
        width = max(self.width(), 320) - 80   # the whole row under the choice, minus "Example: "
        self.example.setText("Example: " + metrics.elidedText(name, Qt.ElideMiddle, width))
        self.example.setToolTip(f"Example file name: {name}")


class GrowingSearch(QPlainTextEdit):
    """A search box that shows everything you type: it grows to three lines,
    then scrolls. Return doesn't add a new line."""

    textChangedTo = Signal(str)

    def __init__(self, placeholder: str, parent=None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setTabChangesFocus(True)
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.document().documentLayout().documentSizeChanged.connect(lambda _s: self._fit())
        self.textChanged.connect(lambda: self.textChangedTo.emit(self.toPlainText()))
        self._fit()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            event.accept()
            return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source) -> None:  # noqa: N802 (pasted text stays on one logical line)
        self.insertPlainText(" ".join(source.text().split()))

    def text(self) -> str:
        return self.toPlainText()

    def _fit(self) -> None:
        line = QFontMetrics(self.font()).lineSpacing()
        content = int(self.document().size().height())
        lines = max(1, min(3, content))
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded if content > 3 else Qt.ScrollBarAlwaysOff)
        self.setFixedHeight(line * lines + 18)


class Collapsible(QWidget):
    """A section with a clickable header (arrow, optional logo, title, and a
    one-line summary on the right) that shows or hides its body. Whether it's
    open is remembered per `key`."""

    toggled = Signal(bool)

    def __init__(self, title: str, key: str = "", icon=None, expanded: bool = False, hint: str = "", parent=None):
        super().__init__(parent)
        from PySide6.QtCore import QSettings, QSize
        from PySide6.QtWidgets import QFrame, QToolButton, QVBoxLayout
        self._key = f"sections/{key}" if key else ""
        if self._key:
            stored = QSettings().value(self._key)
            if stored is not None:
                expanded = str(stored).lower() in ("true", "1")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        head = QFrame(objectName="SectionHeader")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 2, 0, 2)
        hl.setSpacing(6)
        self.button = QToolButton()
        self.button.setCheckable(True)
        self.button.setAutoRaise(True)
        self.button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.button.setProperty("section", True)
        from .theme import heading
        title = heading(title)
        self._title = title
        if icon is not None:
            self.button.setIcon(icon)
            self.button.setIconSize(QSize(16, 16))
        self.button.setToolTip(hint or f"Show or hide {title.lower()}")
        hl.addWidget(self.button)
        self.summary = QLabel()
        self.summary.setProperty("faint", True)
        self.summary.setToolTip(hint)
        self.summary.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)   # clips rather than widening
        self.summary.setMinimumWidth(40)
        hl.addWidget(self.summary, 1)
        outer.addWidget(head)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 4)
        self.body_layout.setSpacing(6)
        outer.addWidget(self.body)
        self.button.toggled.connect(self._toggle)
        self.button.setChecked(expanded)
        self._toggle(expanded, save=False)

    def _toggle(self, on: bool, save: bool = True) -> None:
        from PySide6.QtCore import QSettings
        self.body.setVisible(on)
        self.button.setText(self._title + ("  ▾" if on else "  ▸"))
        if save and self._key:
            QSettings().setValue(self._key, on)
        self.toggled.emit(on)

    def set_expanded(self, on: bool) -> None:
        self.button.setChecked(on)

    @property
    def expanded(self) -> bool:
        return self.button.isChecked()

    def add(self, widget: QWidget) -> QWidget:
        self.body_layout.addWidget(widget)
        return widget

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit_summary()

    def _fit_summary(self) -> None:
        text = getattr(self, "_summary_text", "")
        width = max(self.summary.width(), 40)
        elided = QFontMetrics(self.summary.font()).elidedText(text, Qt.ElideRight, width)
        if elided != self.summary.text():
            self.summary.setText(elided)

    def set_summary(self, text: str, danger: bool = False) -> None:
        self._summary_text = text
        self._fit_summary()
        self.summary.setProperty("faint", not danger)
        self.summary.setProperty("danger", danger)
        self.summary.style().unpolish(self.summary)
        self.summary.style().polish(self.summary)


class ProductFilter(QWidget):
    """Brand first, then product: two menus. Brands come from the tagged
    products (renameable: a brand's name is remembered across the app)."""

    changed = Signal(object)   # set of product ids to show, or None for everything

    SETTING = "brand_names"

    def __init__(self, store=None, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QToolButton
        self.store = store
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.brand = QComboBox()
        self.brand.setToolTip("Show only videos tagged with one brand's products (TikTok Shop). Then narrow it to "
                              "one product on the right. The number is how many videos.")
        self.brand.setMinimumWidth(150)
        self.product = QComboBox()
        self.product.setToolTip("Show only videos tagged with one product. Pick a brand first to shorten this list.")
        self.product.setMinimumWidth(170)
        for combo in (self.brand, self.product):
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(12)
        self.rename_btn = QToolButton(text="✎")
        self.rename_btn.setToolTip("Rename the chosen brand (e.g. “Happy” → “Happy Saffron”)")
        self.rename_btn.setEnabled(False)
        self.rename_btn.clicked.connect(self._rename)
        lay.addWidget(self.brand)
        lay.addWidget(self.rename_btn)
        lay.addWidget(self.product)
        self._products: dict = {}      # id -> Product
        self._count: dict = {}         # id -> videos
        self._brand_of: dict = {}      # id -> brand shown
        self.brand.currentIndexChanged.connect(self._on_brand)
        self.product.currentIndexChanged.connect(self._emit)
        self._fill_brands()

    # ---------------------------------------------------------------- names
    def _overrides(self) -> dict:
        import json
        if self.store is None:
            return {}
        try:
            return json.loads(self.store.get_setting(self.SETTING, "") or "{}")
        except ValueError:
            return {}

    def _rename(self) -> None:
        import json

        from PySide6.QtWidgets import QInputDialog
        current = self.brand.currentData()
        if not current or self.store is None:
            return
        name, ok = QInputDialog.getText(self, "Rename Brand", "Brand name:", text=current)
        if not ok or not name.strip() or name.strip() == current:
            return
        overrides = self._overrides()
        auto = next((a for a, n in overrides.items() if n == current), current)
        overrides[auto] = name.strip()
        self.store.set_setting(self.SETTING, json.dumps(overrides))
        self.set_videos(self._videos, keep=(name.strip(), self.product.currentData()))

    # ---------------------------------------------------------------- data
    def set_videos(self, product_lists, keep=None) -> None:
        """product_lists: for each video, its list of Product."""
        from collections import Counter

        from ..products import brand_map
        self._videos = [list(ps) for ps in product_lists]
        self._products = {p.id: p for ps in self._videos for p in ps}
        self._count = Counter(pid for ps in self._videos for pid in {p.id for p in ps})
        overrides = self._overrides()
        self._brand_of = {pid: overrides.get(b, b) for pid, b in brand_map(self._products.values()).items()}
        brand, product = keep or (self.brand.currentData(), self.product.currentData())
        self._fill_brands(brand, product)

    def _fill_brands(self, brand=None, product=None) -> None:
        from collections import Counter
        videos_per_brand = Counter()
        for ps in getattr(self, "_videos", []):
            for b in {self._brand_of.get(p.id) for p in ps}:
                videos_per_brand[b] += 1
        wanted = [("", "All brands")] + [(b, f"{b}  ({n})") for b, n in
                                          sorted(videos_per_brand.items(), key=lambda kv: (-kv[1], kv[0].lower()))]
        self.brand.blockSignals(True)
        if [(self.brand.itemData(i), self.brand.itemText(i)) for i in range(self.brand.count())] != wanted:
            self.brand.clear()
            for key, label in wanted:
                self.brand.addItem(label, key)
        self.brand.setCurrentIndex(max(self.brand.findData(brand or ""), 0))
        self.brand.blockSignals(False)
        self._fill_products(product)

    def _fill_products(self, product=None) -> None:
        brand = self.brand.currentData() or ""
        ids = [pid for pid in self._products if not brand or self._brand_of.get(pid) == brand]
        ids.sort(key=lambda pid: (-self._count[pid], self._products[pid].label.lower()))
        self.product.blockSignals(True)
        self.product.clear()
        self.product.addItem("All products" if not brand else f"All {brand} products", "")
        for pid in ids:
            self.product.addItem(f"{self._products[pid].label}  ({self._count[pid]})", pid)
            self.product.setItemData(self.product.count() - 1, self._products[pid].title, Qt.ToolTipRole)
        self.product.setCurrentIndex(max(self.product.findData(product or ""), 0))
        self.product.blockSignals(False)
        self.rename_btn.setEnabled(bool(brand))
        self._emit()

    def _on_brand(self) -> None:
        self._fill_products()

    def _emit(self) -> None:
        selection = self.selection()
        if selection == getattr(self, "_last", None):
            return                     # rebuilt with the same choice: nothing to tell anyone
        self._last = selection
        self.changed.emit(selection)

    def selection(self):
        pid = self.product.currentData()
        if pid:
            return {pid}
        brand = self.brand.currentData()
        if brand:
            return {p for p, b in self._brand_of.items() if b == brand}
        return None

    def select_product(self, product_id: str) -> None:
        brand = self._brand_of.get(product_id)
        if brand is not None:
            self._fill_brands(brand, product_id)


class LineLabel(QLabel):
    """One line of text that ends in "…" when it doesn't fit (the full text is its tooltip), so a long name
    or hint never makes its card taller than the others."""

    def __init__(self, text: str = "", object_name: str = "CardLine", parent=None):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setWordWrap(False)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(20)
        self._full = ""
        self.set_full(text)

    def set_full(self, text: str) -> None:
        self._full = text or ""
        self.setToolTip(self._full if self._full else "")
        self._elide()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        elided = self.fontMetrics().elidedText(self._full, Qt.ElideRight, max(self.width(), 20))
        if elided != self.text():
            self.setText(elided)
