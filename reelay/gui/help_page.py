"""The Help Center: a six-step Get Started tour, topic cards, and short visual guides, all offline.

Three views in one page: home (search, the tour, six topics), the tour (Step n of 6, Back / Next, jump to any
step, Take me there), and a topic's guides (a list on the left, the open guide on the right). The words are in
help_content.py; the pictures are real screenshots in help_shots/ with numbered markers placed from
marks.json, so they point at the real controls.
"""

from __future__ import annotations

import html
import json
import re
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QScrollArea, QSizePolicy, QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from . import help_content as H
from . import icons, theme
from .tips import tip

SHOTS = Path(__file__).with_name("help_shots")
SHOT_RATIO = 1100 / 1760                       # every Help picture is a 1440 × 900 window
TOUR_SETTING = "help.tour_step"                # where the tour was left (0-based)


# ====================================================================== pictures
@lru_cache(maxsize=1)
def marks() -> dict:
    try:
        return json.loads((SHOTS / "marks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def shot_path(shot: str, dark: bool | None = None) -> Path:
    if dark is None:
        dark = theme.is_dark(theme.P.bg_primary)
    return SHOTS / f"{shot}-{'dark' if dark else 'light'}.webp"


@lru_cache(maxsize=48)
def _load(path: str) -> QPixmap:
    return QPixmap(path)


def rich(text: str) -> str:
    """**bold** to <b>bold</b>, everything else escaped."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(text, quote=False))


def plain(text: str) -> str:
    return text.replace("**", "")


def _label(text: str = "", name: str = "", wrap: bool = True, prop: str = "") -> QLabel:
    lab = QLabel(objectName=name) if name else QLabel()
    lab.setTextFormat(Qt.RichText)
    lab.setText(rich(text))
    lab.setWordWrap(wrap)
    if prop:
        lab.setProperty(prop, True)
    lab.setTextInteractionFlags(Qt.NoTextInteraction)
    return lab


FULL = (0.0, 0.0, 1.0, 1.0)


def visible_marks(visual: H.Visual | None) -> set[str]:
    """The marks of `visual`'s screenshot that fall inside its crop (so they can be pointed at)."""
    if visual is None:
        return set()
    cx, cy, cw, ch = H.crop_of(visual)
    found = set()
    for key, (x, y, w, h) in marks().get(visual.shot, {}).get("marks", {}).items():
        if x < cx + cw and x + w > cx and y < cy + ch and y + h > cy:
            found.add(key)
    return found


class HelpShot(QWidget):
    """A real screenshot of Reelay (or the part of it a step is about, enlarged), scaled to its width, with
    numbered markers on the controls the steps talk about. Picks the light or dark picture to match the theme
    each time it paints."""

    def __init__(self, shot: str, numbered: list[tuple[int, str]] | None = None, alt: str = "",
                 crop: tuple[float, float, float, float] | None = None, parent=None):
        super().__init__(parent)
        self.shot = shot
        self.crop = crop or FULL
        self.ratio = SHOT_RATIO * self.crop[3] / self.crop[2]
        inside = visible_marks(H.Visual(shot, crop=self.crop))
        self.numbered = [(n, key) for n, key in (numbered or []) if key in inside]
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumWidth(240)
        self.setAccessibleName(f"Screenshot: {plain(alt) or shot}")
        desc = "; ".join(f"marker {n} points at {key}" for n, key in self.numbered)
        self.setAccessibleDescription(plain(alt) + (f" ({desc})" if desc else ""))
        self.setToolTip(plain(alt))
        self._sync_height(self.width())

    def pixmap(self) -> QPixmap:
        return _load(str(shot_path(self.shot)))

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(560, int(560 * self.ratio))

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return int(width * self.ratio)

    def _sync_height(self, width: int) -> None:
        h = self.heightForWidth(max(width, 1))
        if self.minimumHeight() != h:
            self.setFixedHeight(h)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_height(event.size().width())

    def marker_rect(self, key: str) -> QRectF:
        x, y, w, h = marks()[self.shot]["marks"][key]
        cx, cy, cw, ch = self.crop
        sx, sy = self.width() / cw, self.height() / ch
        rect = QRectF((x - cx) * sx, (y - cy) * sy, w * sx, h * sy)
        return rect.intersected(QRectF(self.rect()))

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        frame = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        clip = QPainterPath()
        clip.addRoundedRect(frame, theme.RADIUS_CARD, theme.RADIUS_CARD)
        pix = self.pixmap()
        p.save()
        p.setClipPath(clip)
        if pix.isNull():
            p.fillRect(frame, QColor(theme.P.bg_raised))
            p.setPen(QColor(theme.P.text_muted))
            p.drawText(frame, Qt.AlignCenter, "Picture missing from this copy of Reelay")
        else:
            cx, cy, cw, ch = self.crop
            source = QRectF(cx * pix.width(), cy * pix.height(), cw * pix.width(), ch * pix.height())
            p.drawPixmap(QRectF(self.rect()), pix, source)
        p.restore()
        p.setPen(QPen(QColor(theme.P.card_edge), 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(clip)
        marker = QColor(theme.P.marker)
        soft = QColor(marker)
        soft.setAlphaF(0.14)
        font = QFont(self.font())
        font.setBold(True)
        font.setPixelSize(12)
        p.setFont(font)
        d = 22.0
        placed: list[QRectF] = []
        for n, key in self.numbered:
            r = self.marker_rect(key).adjusted(-3, -3, 3, 3)
            p.setPen(QPen(marker, 2))
            p.setBrush(soft)
            p.drawRoundedRect(r, 6, 6)
            cx = min(max(r.left(), d / 2 + 2), self.width() - d / 2 - 2)
            cy = min(max(r.top(), d / 2 + 2), self.height() - d / 2 - 2)
            badge = QRectF(cx - d / 2, cy - d / 2, d, d)
            while any(badge.intersects(other) for other in placed) and badge.right() + d < self.width():
                badge.translate(d + 3, 0)            # markers on neighbouring controls sit side by side
            placed.append(badge)
            p.setPen(QPen(QColor(theme.P.bg_primary), 2))
            p.setBrush(marker)
            p.drawEllipse(badge)
            p.setPen(QColor(theme.P.marker_ink))
            p.drawText(badge, Qt.AlignCenter, str(n))
        p.end()


class PlatformChart(QFrame):
    """The drawn "platforms" picture: who posts to each platform, Reelay or you."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from .logos import platform_pixmap
        self.setObjectName("HelpDiagram")
        self.setAccessibleName("Chart: which platforms Reelay posts to")
        grid = QGridLayout(self)
        grid.setContentsMargins(18, 14, 18, 14)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        for row, (key, label, who, note) in enumerate(H.POSTING):
            logo = QLabel()
            logo.setPixmap(platform_pixmap(key, 22))
            logo.setFixedSize(24, 24)
            grid.addWidget(logo, row, 0)
            grid.addWidget(_label(f"**{label}**"), row, 1)
            pill = QLabel(who, objectName="StatusPill")
            pill.setProperty("state", "connected" if who.startswith("Reelay") else "hand")
            grid.addWidget(pill, row, 2, Qt.AlignLeft)
            grid.addWidget(_label(note, prop="muted"), row, 3)
        grid.setColumnStretch(3, 1)


class StorageDiagram(QFrame):
    """The drawn "storage" picture: what Reelay keeps where, all on this Mac."""

    PLACES = (("folder", "Your videos", "The folder you choose (Download Settings)"),
              ("board", "Database and caches", "Reelay's data folder (Settings → Data)"),
              ("shield", "Passwords and tokens", "Your Mac's Keychain"))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("HelpDiagram")
        self.setAccessibleName("Diagram: where Reelay keeps your data, all on this Mac")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 16)
        outer.setSpacing(10)
        outer.addWidget(_label("**On this Mac**"))
        row = QHBoxLayout()
        row.setSpacing(10)
        for icon, title, where in self.PLACES:
            box = QFrame(objectName="HelpPlace")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(12, 10, 12, 12)
            bl.setSpacing(4)
            bl.addWidget(_IconMark(icon, 22))
            bl.addWidget(_label(f"**{title}**"))
            bl.addWidget(_label(where, prop="muted"))
            bl.addStretch()
            row.addWidget(box, 1)
        outer.addLayout(row)
        outer.addWidget(_label("Reelay has no cloud of its own: nothing here is synced or backed up by Reelay.",
                               prop="muted"))


class _IconMark(QWidget):
    """One of Reelay's line icons, tinted when it paints (so it follows the theme)."""

    def __init__(self, name: str, size: int = 22, parent=None):
        super().__init__(parent)
        self.name, self.px = name, size
        self.setFixedSize(size, size)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.drawPixmap(0, 0, icons.pixmap(self.name, theme.P.text_primary, self.px))
        p.end()


def visual_widget(visual: H.Visual | None, steps: tuple[H.Step, ...]) -> QWidget | None:
    if visual is None:
        return None
    if visual.shot == "platforms":
        return PlatformChart()
    if visual.shot == "storage":
        return StorageDiagram()
    numbered = [(n, s.mark) for n, s in enumerate(steps, 1) if s.mark]
    return HelpShot(visual.shot, numbered, visual.alt, H.crop_of(visual))


# ====================================================================== shared pieces
class StepList(QWidget):
    """Numbered steps; a step whose control is marked in the picture gets the marker's blue number."""

    def __init__(self, steps: tuple[H.Step, ...], visual: H.Visual | None, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        known = visible_marks(visual)
        for n, step in enumerate(steps, 1):
            row = QHBoxLayout()
            row.setSpacing(10)
            num = QLabel(str(n), objectName="HelpNum")
            num.setAlignment(Qt.AlignCenter)
            num.setFixedSize(22, 22)
            num.setProperty("marked", bool(step.mark and step.mark in known))
            if step.mark and step.mark in known:
                num.setToolTip(f"Marker {n} in the picture")
            row.addWidget(num, 0, Qt.AlignTop)
            row.addWidget(_label(step.text), 1)
            lay.addLayout(row)


def _callout(title: str, text: str) -> QFrame:
    box = QFrame(objectName="HelpCallout")
    bl = QVBoxLayout(box)
    bl.setContentsMargins(14, 10, 14, 12)
    bl.setSpacing(3)
    bl.addWidget(_label(title, "HelpKicker"))
    bl.addWidget(_label(text))
    return box


def _kicker(text: str) -> QLabel:
    return _label(text, "HelpKicker")


class DetailToggle(QWidget):
    """Optional detail, closed until asked for: a title you click, then a few short lines."""

    def __init__(self, detail: H.Detail, parent=None):
        super().__init__(parent)
        self.detail = detail
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.button = QToolButton(objectName="HelpDetail")
        self.button.setCheckable(True)
        self.button.setText(f"▸  {detail.title}")
        self.button.setCursor(Qt.PointingHandCursor)
        tip(self.button, f"Show or hide: {detail.title}")
        self.button.toggled.connect(self._toggle)
        lay.addWidget(self.button, 0, Qt.AlignLeft)
        self.body = QWidget()
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(20, 0, 0, 4)
        bl.setSpacing(6)
        for line in detail.lines:
            bl.addWidget(_label(f"•  {line}"))
        self.body.hide()
        lay.addWidget(self.body)

    def _toggle(self, on: bool) -> None:
        self.body.setVisible(on)
        self.button.setText(f"{'▾' if on else '▸'}  {self.detail.title}")


def _drop(widget: QWidget | None) -> None:
    """Take a widget out of its layout now (deleting it later), so a new step never shows the old one."""
    if widget is None:
        return
    widget.hide()
    if widget.parentWidget() is not None and widget.parentWidget().layout() is not None:
        widget.parentWidget().layout().removeWidget(widget)
    widget.setParent(None)
    widget.deleteLater()


def _back_button(text: str = "Help Center") -> QPushButton:
    b = QPushButton(f"‹  {text}", objectName="HelpBack")
    b.setCursor(Qt.PointingHandCursor)
    tip(b, "Back to the Help Center (Esc)")
    return b


# ====================================================================== home
class CategoryCard(QPushButton):
    """A topic: its icon, title, a one-line description and how many guides it has. The whole card clicks."""

    def __init__(self, category: H.Category, parent=None):
        super().__init__(parent)
        self.category = category
        self.setObjectName("HelpCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(118)
        tip(self, f"Open {category.title}: {category.blurb}")
        self.setAccessibleName(category.title)
        self.setAccessibleDescription(category.blurb)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(_IconMark(category.icon, 22))
        title = _label(f"**{category.title}**", "HelpCardTitle")
        top.addWidget(title, 1)
        lay.addLayout(top)
        blurb = _label(category.blurb, prop="muted")
        lay.addWidget(blurb)
        lay.addStretch()
        count = _label(f"{len(category.guides)} guides", prop="faint", wrap=False)
        lay.addWidget(count)
        for w in (title, blurb, count):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)


class HelpHome(QWidget):
    tourRequested = Signal(int)
    guideRequested = Signal(str)
    tourStepRequested = Signal(int)

    def __init__(self, store=None, parent=None):
        super().__init__(parent)
        self.store = store
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget(objectName="Page")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(28, 22, 28, 28)
        lay.setSpacing(18)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        head = QHBoxLayout()
        head.setSpacing(16)
        title = QLabel("Help", objectName="PageTitle")
        head.addWidget(title, 0, Qt.AlignVCenter)
        head.addStretch()
        self.search = QLineEdit(objectName="HelpSearch")
        self.search.setPlaceholderText("Search help: schedule, TikTok, archive…")
        self.search.setClearButtonEnabled(True)
        self.search.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.search.addAction(icons.icon("search", 16), QLineEdit.LeadingPosition)
        self.search.setMinimumWidth(320)
        tip(self.search, "Find a guide by what you want to do. Press Return to open the first result.")
        self.search.textChanged.connect(self._on_search)
        self.search.returnPressed.connect(self._open_first)
        head.addWidget(self.search)
        lay.addLayout(head)

        # ---- the tour
        hero = QFrame(objectName="HelpHero")
        hl = QHBoxLayout(hero)
        hl.setContentsMargins(22, 18, 18, 18)
        hl.setSpacing(22)
        text = QVBoxLayout()
        text.setSpacing(8)
        text.addWidget(_kicker("Get started"))
        text.addWidget(_label("**New to Reelay? Take the six-step tour.**", "HelpHeroTitle"))
        text.addWidget(_label("Profiles, bringing videos in, preparing posts, readiness, publishing and finding "
                              "your work, each in one screen. It's just a look around: nothing changes.",
                              prop="muted"))
        text.addSpacing(4)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.tour_btn = QPushButton("Start the Tour")
        self.tour_btn.setProperty("primary", True)
        tip(self.tour_btn, "Six short steps through the whole Reelay workflow")
        self.tour_btn.clicked.connect(lambda: self.tourRequested.emit(self._resume_step()))
        buttons.addWidget(self.tour_btn)
        self.restart_btn = QPushButton("Start Over")
        tip(self.restart_btn, "Start the tour again from step 1")
        self.restart_btn.clicked.connect(lambda: self.tourRequested.emit(0))
        buttons.addWidget(self.restart_btn)
        buttons.addStretch()
        text.addLayout(buttons)
        text.addStretch()
        hl.addLayout(text, 5)
        self.hero_shot = HelpShot("board", [], "The Board: four columns of video tickets")   # the whole window
        hl.addWidget(self.hero_shot, 4)
        lay.addWidget(hero)
        self.hero = hero

        # ---- topics, or search results
        self.topics_title = _kicker("Browse topics")
        lay.addWidget(self.topics_title)
        self.topics = QWidget()
        grid = QGridLayout(self.topics)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)
        self.cards: list[CategoryCard] = []
        for n, category in enumerate(H.CATEGORIES):           # six topics: an even 3 × 2 grid
            card = CategoryCard(category)
            card.clicked.connect(lambda _=False, c=category: self.guideRequested.emit(c.guides[0].key))
            self.cards.append(card)
            grid.addWidget(card, n // 3, n % 3)
        for col in range(3):
            grid.setColumnStretch(col, 1)
        lay.addWidget(self.topics)

        self.results = QWidget()
        self.results_lay = QVBoxLayout(self.results)
        self.results_lay.setContentsMargins(0, 0, 0, 0)
        self.results_lay.setSpacing(6)
        self.results.hide()
        lay.addWidget(self.results)
        lay.addStretch()
        self.result_buttons: list[QPushButton] = []
        self.sync_tour()

    def _resume_step(self) -> int:
        try:
            return min(max(int(self.store.get_setting(TOUR_SETTING, "0") or 0), 0), len(H.TOUR) - 1) \
                if self.store else 0
        except ValueError:
            return 0

    def sync_tour(self) -> None:
        step = self._resume_step()
        self.tour_btn.setText(f"Continue the Tour: Step {step + 1} of {len(H.TOUR)}" if step else "Start the Tour")
        self.restart_btn.setVisible(step > 0)

    # ---- search
    def _on_search(self, text: str) -> None:
        found = H.search(text)
        searching = bool(text.strip())
        self.hero.setVisible(not searching)          # results come straight under the search field
        self.topics.setVisible(not searching)
        self.results.setVisible(searching)
        self.topics_title.setText(rich("Results" if searching else "Browse topics"))
        self.result_buttons = []
        while self.results_lay.count():
            item = self.results_lay.takeAt(0)
            _drop(item.widget())
        if not searching:
            return
        if not found:
            self.results_lay.addWidget(_label(f"No guides match “{text.strip()}”. Try a simpler word, or clear "
                                              "the search to browse the topics.", prop="muted"))
            return
        for kind, key, title in found[:12]:
            if kind == "guide":
                where = H.CATEGORY_OF[key].title
            else:
                where = f"Get started · Step {[s.key for s in H.TOUR].index(key) + 1} of {len(H.TOUR)}"
            b = QPushButton(objectName="HelpResult")
            b.setText(f"{title}   ·   {where}".replace("&", "&&"))     # a lone & would underline a letter
            b.setCursor(Qt.PointingHandCursor)
            tip(b, f"Open “{title}”")
            if kind == "guide":
                b.clicked.connect(lambda _=False, k=key: self.guideRequested.emit(k))
            else:
                b.clicked.connect(lambda _=False, k=key: self.tourStepRequested.emit(
                    [s.key for s in H.TOUR].index(k)))
            self.results_lay.addWidget(b)
            self.result_buttons.append(b)

    def _open_first(self) -> None:
        if self.result_buttons:
            self.result_buttons[0].click()


# ====================================================================== the tour
class TourView(QWidget):
    exitRequested = Signal()
    navigate = Signal(str)
    stepChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.index = 0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 18, 28, 18)
        outer.setSpacing(12)

        top = QHBoxLayout()
        top.setSpacing(12)
        self.exit_btn = _back_button()
        self.exit_btn.clicked.connect(self.exitRequested.emit)
        top.addWidget(self.exit_btn)
        top.addStretch()
        self.progress = QLabel(objectName="HelpProgress")
        self.progress.setToolTip("Where you are in the tour")
        top.addWidget(self.progress)
        outer.addLayout(top)

        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.chip_group = QButtonGroup(self)
        self.chips: list[QPushButton] = []
        for n, step in enumerate(H.TOUR):
            chip = QPushButton(f"{n + 1}  {step.short or step.title}", objectName="TourChip")
            chip.setCheckable(True)
            chip.setCursor(Qt.PointingHandCursor)
            tip(chip, f"Go to step {n + 1}: {step.title}")
            chip.clicked.connect(lambda _=False, i=n: self.show_step(i))
            self.chip_group.addButton(chip)
            self.chips.append(chip)
            chips.addWidget(chip)
        chips.addStretch()
        outer.addLayout(chips)

        body = QHBoxLayout()
        body.setSpacing(28)
        left = QVBoxLayout()
        left.setSpacing(12)
        self.title = QLabel(objectName="HelpStepTitle")
        self.title.setWordWrap(True)
        left.addWidget(self.title)
        self.goal = _label(prop="muted")
        left.addWidget(self.goal)
        self.steps_holder = QVBoxLayout()
        self.steps_holder.setSpacing(0)
        left.addLayout(self.steps_holder)
        self.note = _label(prop="faint")
        left.addWidget(self.note)
        self.next_box = _callout("What's next", "")
        left.addWidget(self.next_box)
        left.addStretch()
        inner = QWidget(objectName="Page")
        inner.setLayout(left)
        self.left = QScrollArea()                 # a short window scrolls the words, never squeezes them
        self.left.setWidgetResizable(True)
        self.left.setFrameShape(QFrame.NoFrame)
        self.left.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.left.setWidget(inner)
        self.left.setMinimumWidth(280)
        self.left.setMaximumWidth(400)
        body.addWidget(self.left, 4)
        self.visual_holder = QVBoxLayout()
        self.visual_holder.setContentsMargins(0, 4, 0, 0)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addLayout(self.visual_holder)
        rl.addStretch()
        body.addWidget(right, 6)
        outer.addLayout(body, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.back_btn = QPushButton("Back")
        tip(self.back_btn, "The previous step (←)")
        self.back_btn.clicked.connect(lambda: self.show_step(self.index - 1))
        bottom.addWidget(self.back_btn)
        self.go_btn = QPushButton()
        self.go_btn.clicked.connect(self._go)
        bottom.addWidget(self.go_btn)
        bottom.addStretch()
        self.next_btn = QPushButton("Next")
        self.next_btn.setProperty("primary", True)
        tip(self.next_btn, "The next step (→)")
        self.next_btn.clicked.connect(self._next)
        bottom.addWidget(self.next_btn)
        outer.addLayout(bottom)

        for key, fn in ((Qt.Key_Left, lambda: self.index > 0 and self.show_step(self.index - 1)),
                        (Qt.Key_Right, self._next_if_any), (Qt.Key_Escape, self.exitRequested.emit)):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)
        self.visual: QWidget | None = None
        self.step_list: QWidget | None = None
        self.show_step(0)

    @property
    def step(self) -> H.TourStep:
        return H.TOUR[self.index]

    def show_step(self, index: int) -> None:
        self.index = min(max(index, 0), len(H.TOUR) - 1)
        step = self.step
        total = len(H.TOUR)
        self.progress.setText(f"Step {self.index + 1} of {total}")
        self.chips[self.index].setChecked(True)
        self.title.setText(step.title)
        self.goal.setText(rich(step.goal))
        _drop(self.step_list)
        self.step_list = StepList(step.steps, step.visual)
        self.steps_holder.addWidget(self.step_list)
        self.note.setText(rich(step.note))
        self.note.setVisible(bool(step.note))
        self.next_box.findChildren(QLabel)[-1].setText(rich(step.next))
        _drop(self.visual)
        self.visual = visual_widget(step.visual, step.steps)
        self.visual_holder.addWidget(self.visual)
        self.back_btn.setEnabled(self.index > 0)
        last = self.index == total - 1
        self.next_btn.setText("Finish" if last else "Next")
        self.next_btn.setToolTip("Back to the Help Center" if last else "The next step (→)")
        self.go_btn.setVisible(bool(step.go))
        if step.go:
            self.go_btn.setText(f"Take Me There: {step.go_label.replace('Open ', '')}")
            tip(self.go_btn, f"{step.go_label} (the tour remembers where you were)")
        self.stepChanged.emit(self.index)

    def _next(self) -> None:
        if self.index == len(H.TOUR) - 1:
            self.exitRequested.emit()
        else:
            self.show_step(self.index + 1)

    def _next_if_any(self) -> None:
        if self.index < len(H.TOUR) - 1:
            self.show_step(self.index + 1)

    def _go(self) -> None:
        if self.step.go:
            self.navigate.emit(self.step.go)


# ====================================================================== a topic's guides
class GuideView(QWidget):
    backRequested = Signal()
    navigate = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.guide: H.Guide | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 18, 28, 0)
        outer.setSpacing(12)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.back_btn = _back_button()
        self.back_btn.clicked.connect(self.backRequested.emit)
        top.addWidget(self.back_btn)
        self.crumb = QLabel(objectName="HelpCrumb")
        self.crumb.setToolTip("The topic you're in")
        top.addWidget(self.crumb)
        top.addStretch()
        outer.addLayout(top)

        body = QHBoxLayout()
        body.setSpacing(24)
        self.nav = QWidget()
        self.nav.setFixedWidth(232)
        self.nav_lay = QVBoxLayout(self.nav)
        self.nav_lay.setContentsMargins(0, 0, 0, 0)
        self.nav_lay.setSpacing(2)
        self.nav_group = QButtonGroup(self)
        self.nav_buttons: dict[str, QPushButton] = {}
        body.addWidget(self.nav, 0, Qt.AlignTop)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body.addWidget(self.scroll, 1)
        outer.addLayout(body, 1)
        sc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        sc.setContext(Qt.WidgetWithChildrenShortcut)
        sc.activated.connect(self.backRequested.emit)
        self.go_btn: QPushButton | None = None
        self.related_buttons: list[QPushButton] = []
        self.details: list[DetailToggle] = []
        self.visual: QWidget | None = None
        self._category: H.Category | None = None

    def _build_nav(self, category: H.Category) -> None:
        if self._category is category:
            return
        self._category = category
        for b in self.nav_buttons.values():
            self.nav_group.removeButton(b)
            _drop(b)
        self.nav_buttons = {}
        while self.nav_lay.count():
            self.nav_lay.takeAt(0)
        for guide in category.guides:
            b = QPushButton(guide.title.replace("&", "&&"), objectName="HelpNav")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            tip(b, guide.doing)
            b.clicked.connect(lambda _=False, k=guide.key: self.show_guide(k))
            self.nav_group.addButton(b)
            self.nav_buttons[guide.key] = b
            self.nav_lay.addWidget(b)
        self.nav_lay.addStretch()

    def show_guide(self, key: str) -> None:
        guide = H.GUIDES[key]
        category = H.CATEGORY_OF[key]
        self.guide = guide
        self._build_nav(category)
        self.nav_buttons[key].setChecked(True)
        self.crumb.setText(f"›   {category.title}")

        page = QWidget(objectName="Page")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 8, 28)
        lay.setSpacing(14)
        title = QLabel(guide.title, objectName="HelpStepTitle")
        title.setWordWrap(True)
        lay.addWidget(title)
        lay.addWidget(_kicker("What you're doing"))
        lay.addWidget(_label(guide.doing))
        self.visual = visual_widget(guide.visual, guide.steps)
        if self.visual is not None:
            lay.addWidget(self.visual)
        lay.addWidget(_kicker("What to do"))
        lay.addWidget(StepList(guide.steps, guide.visual))
        lay.addWidget(_callout("What to expect", guide.expect))
        if guide.note:
            lay.addWidget(_label(guide.note, prop="faint"))
        self.go_btn = None
        if guide.go:
            self.go_btn = QPushButton(f"Take Me There: {guide.go_label.replace('Open ', '')}")
            tip(self.go_btn, guide.go_label)
            self.go_btn.clicked.connect(lambda _=False, g=guide.go: self.navigate.emit(g))
            lay.addWidget(self.go_btn, 0, Qt.AlignLeft)
        self.details = []
        if guide.details:
            lay.addWidget(_kicker("More details"))
            for d in guide.details:
                toggle = DetailToggle(d)
                self.details.append(toggle)
                lay.addWidget(toggle)
        self.related_buttons = []
        if guide.related:
            lay.addWidget(_kicker("Need more help?"))
            for rel in guide.related:
                other = H.GUIDES[rel]
                b = QPushButton(f"{other.title}  ›".replace("&", "&&"), objectName="HelpLink")
                b.setCursor(Qt.PointingHandCursor)
                tip(b, f"{other.doing} ({H.CATEGORY_OF[rel].title})")
                b.clicked.connect(lambda _=False, k=rel: self.show_guide(k))
                self.related_buttons.append(b)
                lay.addWidget(b, 0, Qt.AlignLeft)
        lay.addStretch()
        _drop(self.scroll.takeWidget())
        self.scroll.setWidget(page)
        self.scroll.verticalScrollBar().setValue(0)


# ====================================================================== the page
class HelpPage(QWidget):
    """Help in the sidebar: home, the tour, and guides. `navigate` asks the window to open a page."""

    navigate = Signal(str)

    def __init__(self, store=None, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.store = store
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget(objectName="HelpStack")
        self.home = HelpHome(store)
        self.tour = TourView()
        self.guides = GuideView()
        for w in (self.home, self.tour, self.guides):
            self.stack.addWidget(w)
        lay.addWidget(self.stack)
        self.home.tourRequested.connect(self.start_tour)
        self.home.tourStepRequested.connect(self.start_tour)
        self.home.guideRequested.connect(self.open_guide)
        self.tour.exitRequested.connect(self._finish_or_exit)
        self.tour.stepChanged.connect(self._remember_step)
        self.tour.navigate.connect(self.navigate.emit)
        self.guides.backRequested.connect(self.show_home)
        self.guides.navigate.connect(self.navigate.emit)

    def show_home(self) -> None:
        self.home.sync_tour()
        self.stack.setCurrentWidget(self.home)
        self.home.search.setFocus()

    def start_tour(self, step: int = 0) -> None:
        self.tour.show_step(step)
        self.stack.setCurrentWidget(self.tour)
        self.tour.next_btn.setFocus()

    def open_guide(self, key: str) -> None:
        self.guides.show_guide(key)
        self.stack.setCurrentWidget(self.guides)
        self.guides.nav_buttons[key].setFocus()

    def _remember_step(self, index: int) -> None:
        if self.store is not None:
            self.store.set_setting(TOUR_SETTING, str(index))

    def _finish_or_exit(self) -> None:
        if self.tour.index == len(H.TOUR) - 1 and self.store is not None:
            self.store.set_setting(TOUR_SETTING, "0")      # finished: next time starts from the top
        self.show_home()

    @property
    def current(self) -> QWidget:
        return self.stack.currentWidget()


def selftest() -> str:
    """For `reelay --self-test`: every guide's picture and marker is in this copy of Reelay."""
    from PySide6.QtGui import QImageReader
    found = marks()
    if not found:
        raise AssertionError("help_shots/marks.json is missing or damaged")
    shots = {v.shot for v in [s.visual for s in H.TOUR] + [g.visual for g in H.GUIDES.values() if g.visual]
             if v.shot not in H.DRAWN}
    for shot in shots:
        for dark in (True, False):
            path = shot_path(shot, dark)
            if not QImageReader(str(path)).canRead():
                raise AssertionError(f"{path.name} is missing or can't be read")
    return f"{len(H.TOUR)}-step tour, {len(H.GUIDES)} guides, {len(shots) * 2} pictures"
