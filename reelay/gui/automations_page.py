"""Automations page: "When … if … then …" rules, and what they did."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTime, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QMenu, QMessageBox, QPushButton, QTimeEdit,
                               QToolButton, QVBoxLayout, QWidget)

from .. import rules as R
from ..columns import load as load_columns
from ..models import MANUAL_LABELS, MANUAL_PLATFORMS
from ..platforms import PLATFORM_HOSTS
from ..text import local_text
from . import icons, theme
from .theme import set_prop
from .tips import tip
from .widgets import scroll_page


def _muted(text: str, prop: str = "muted") -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setProperty(prop, True)
    return lab


# Where downloaded videos come from (for "Came from")
SOURCES = tuple(dict.fromkeys(PLATFORM_HOSTS.values()))
# Insert Variable chips: (button text, placeholder)
VARIABLES = (("+ Hook", "{hook}"), ("+ Original Caption", "{caption}"), ("+ Hashtags", "{hashtags}"),
             ("+ Title", "{title}"), ("+ Topic", "{topic}"))
VARIABLE_TIPS = {"{hook}": "The video's opening line", "{caption}": "The caption the video was posted with",
                 "{hashtags}": "The ticket's hashtags (or the original post's)", "{title}": "The ticket's title",
                 "{topic}": "The ticket's topic"}


class PlatformPills(QWidget):
    """One toggle per platform, with its logo. Reads and writes the comma list rules store ("TikTok, YouTube")."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        from .logos import platform_icon
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.pills: dict[str, QToolButton] = {}
        for key in MANUAL_PLATFORMS:
            b = tip(QToolButton(text=MANUAL_LABELS[key], objectName="PlatformChip"),
                    f"Post to {MANUAL_LABELS[key]}")
            b.setCheckable(True)
            b.setIcon(platform_icon(key, 16))
            b.setIconSize(QSize(16, 16))
            b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            b.setCursor(Qt.PointingHandCursor)
            b.toggled.connect(lambda *_: self.changed.emit())
            self.pills[key] = b
            lay.addWidget(b)
        lay.addStretch()

    def set_value(self, text: str) -> None:
        chosen = set(R.parse_platforms(text))
        for key, b in self.pills.items():
            b.blockSignals(True)
            b.setChecked(key in chosen)
            b.blockSignals(False)

    def value(self) -> str:
        return ", ".join(MANUAL_LABELS[k] for k, b in self.pills.items() if b.isChecked())


class WhenPicker(QWidget):
    """A weekday and a time, stored the way rules read them ("Tuesday 09:00")."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.day = tip(QComboBox(), "The day of the week to post on")
        for day in R.WEEKDAYS:
            self.day.addItem(day, day)
        self.time = tip(QTimeEdit(QTime(9, 0)), "The time to post (your local time)")
        self.time.setDisplayFormat("HH:mm")
        self.day.setCurrentIndex(1)                                   # Tuesday, like the recipe
        lay.addWidget(self.day)
        lay.addWidget(_muted("at", "faint"))
        lay.addWidget(self.time)
        lay.addStretch()
        self.day.currentIndexChanged.connect(lambda *_: self.changed.emit())
        self.time.timeChanged.connect(lambda *_: self.changed.emit())

    def set_value(self, text: str) -> None:
        when = R.parse_when(text)
        if when:
            day, at = when
            self.day.setCurrentIndex(day)
            self.time.setTime(QTime(at.hour, at.minute))

    def value(self) -> str:
        return f"{self.day.currentData()} {self.time.time().toString('HH:mm')}"


class ConditionRow(QWidget):
    removed = Signal(object)
    changed = Signal()

    def __init__(self, c: R.Condition | None = None, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.kind = QComboBox()
        for k, label in R.CONDITIONS.items():
            self.kind.addItem(label, k)
        tip(self.kind, "What to check about the video")
        self.value = tip(QLineEdit(), "The word, hashtag or number to look for")
        self.value.setPlaceholderText("e.g. sunglasses")
        # "Came from" picks a platform instead of typing its name
        self.source = tip(QComboBox(), "Where the video was downloaded from")
        for label in SOURCES:
            self.source.addItem(label, label)
        x = tip(QPushButton("✕"), "Remove this condition")
        set_prop(x, "flat")
        x.clicked.connect(lambda: self.removed.emit(self))
        lay.addWidget(self.kind)
        lay.addWidget(self.value, 1)
        lay.addWidget(self.source, 1)
        lay.addWidget(x)
        if c:
            self.kind.setCurrentIndex(max(self.kind.findData(c.kind), 0))
            if c.kind == "source":
                found = next((i for i in range(self.source.count())
                              if self.source.itemData(i).lower() == c.value.strip().lower()), -1)
                if found < 0 and c.value.strip():
                    self.source.addItem(c.value.strip(), c.value.strip())   # a name typed before the list
                    found = self.source.count() - 1
                self.source.setCurrentIndex(max(found, 0))
            else:
                self.value.setText(c.value)
        self._sync()
        self.kind.currentIndexChanged.connect(self._sync)
        self.kind.currentIndexChanged.connect(lambda *_: self.changed.emit())
        self.value.textChanged.connect(lambda *_: self.changed.emit())
        self.source.currentIndexChanged.connect(lambda *_: self.changed.emit())

    def _sync(self) -> None:
        kind = self.kind.currentData()
        self.value.setVisible(kind != "source")
        self.source.setVisible(kind == "source")
        self.value.setPlaceholderText({"hashtag": "e.g. summer", "shorter": "Seconds, e.g. 60",
                                       "longer": "Seconds, e.g. 60"}.get(kind, "e.g. sunglasses"))

    def condition(self) -> R.Condition:
        kind = self.kind.currentData()
        return R.Condition(kind, self.source.currentData() if kind == "source" else self.value.text().strip())


class ActionRow(QWidget):
    removed = Signal(object)
    changed = Signal()

    def __init__(self, columns, a: R.Action | None = None, parent=None):
        super().__init__(parent)
        self.columns = columns
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        lay = QHBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(lay)
        self.kind = tip(QComboBox(), "What the rule does")
        for k, label in R.ACTIONS.items():
            self.kind.addItem(label, k)
        self.field = tip(QComboBox(), "Which field or column")
        self.value = QLineEdit()
        self.platforms = PlatformPills()
        self.when = WhenPicker()
        x = tip(QPushButton("✕"), "Remove this action")
        set_prop(x, "flat")
        x.clicked.connect(lambda: self.removed.emit(self))
        lay.addWidget(self.kind)
        lay.addWidget(self.field)
        lay.addWidget(self.value, 1)
        lay.addWidget(self.platforms, 1)
        lay.addWidget(self.when, 1)
        lay.addWidget(x)
        # Insert Variable chips, for templates
        self.chips = QWidget()
        chips = QHBoxLayout(self.chips)
        chips.setContentsMargins(0, 0, 0, 0)
        chips.setSpacing(6)
        chips.addWidget(_muted("Insert:", "faint"))
        self.chip_buttons: dict[str, QPushButton] = {}
        for text, placeholder in VARIABLES:
            b = tip(QPushButton(text, objectName="VariableChip"), f"Insert {placeholder}: "
                    f"{VARIABLE_TIPS[placeholder].lower()}")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, v=placeholder: self.insert_variable(v))
            self.chip_buttons[placeholder] = b
            chips.addWidget(b)
        chips.addStretch()
        outer.addWidget(self.chips)
        self.kind.currentIndexChanged.connect(self._sync)
        if a:
            self.kind.setCurrentIndex(max(self.kind.findData(a.kind), 0))
        self._sync()
        if a:
            self.field.setCurrentIndex(max(self.field.findData(a.field), 0))
            if a.kind == "set_platforms":
                self.platforms.set_value(a.value)
            elif a.kind == "schedule":
                self.when.set_value(a.value)
            else:
                self.value.setText(a.value)
        for signal in (self.kind.currentIndexChanged, self.field.currentIndexChanged, self.value.textChanged,
                       self.platforms.changed, self.when.changed):
            signal.connect(lambda *_: self.changed.emit())

    def insert_variable(self, placeholder: str) -> None:
        """Put a placeholder where the cursor is (with a space either side as needed)."""
        text, pos = self.value.text(), self.value.cursorPosition()
        before = "" if not text[:pos] or text[:pos].endswith((" ", "\n")) else " "
        after = "" if not text[pos:] or text[pos:].startswith(" ") else " "
        self.value.insert(before + placeholder + after)
        self.value.setFocus()

    def _sync(self) -> None:
        kind = self.kind.currentData()
        self.field.clear()
        options = {"fill_copy": R.COPY_FIELDS, "set_ticket": R.TICKET_FIELDS_SETTABLE,
                   "move": {c.primary: c.name for c in self.columns}}.get(kind)
        for key, label in (options or {}).items():
            self.field.addItem(label, key)
        self.field.setVisible(bool(options))
        self.value.setVisible(kind not in ("move", "set_platforms", "schedule"))
        self.platforms.setVisible(kind == "set_platforms")
        self.when.setVisible(kind == "schedule")
        self.chips.setVisible(kind in ("fill_copy", "set_ticket"))
        self.value.setPlaceholderText({
            "fill_copy": "Template, e.g. {hook} ☀️ Link in bio {hashtags}",
            "add_hashtags": "#summer #sunnies",
            "set_ticket": "e.g. Eyewear"}.get(kind, ""))
        tip(self.value, "The template: type text and use Insert to add the video's own words. Placeholders: "
            + " ".join(R.PLACEHOLDERS) if kind in ("fill_copy", "set_ticket") else
            "Hashtags to add, e.g. #summer #sunnies" if kind == "add_hashtags" else "")
        self.when.setToolTip("Scheduled for the next one of these, only if the ticket is ready")

    def action(self) -> R.Action:
        kind = self.kind.currentData()
        field = self.field.currentData() or "" if self.field.isVisibleTo(self) or kind == "move" else ""
        value = {"move": "", "set_platforms": self.platforms.value(), "schedule": self.when.value()}.get(
            kind, self.value.text().strip())
        return R.Action(kind, field, value)


class RuleDialog(QDialog):
    """Edit one rule."""

    def __init__(self, rule: R.Rule, columns, parent=None, pipeline=None, profile_id: int | None = None,
                 title: str = "Automation", intro: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 720)
        self.rule, self.columns = rule, columns
        self.pipeline, self.profile_id = pipeline, profile_id
        self._preview_timer = QTimer(self, singleShot=True, interval=250, timeout=self._update_preview)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(10)
        if intro:
            lay.addWidget(_muted(intro))
        self.name = tip(QLineEdit(rule.name), "A name for this rule, shown on the Automations page")
        self.name.setPlaceholderText("Name, e.g. Sunglasses → summer template, Tuesdays")
        lay.addWidget(self.name)

        lay.addWidget(QLabel("<b>When</b>"))
        wrow = QHBoxLayout()
        self.trigger = tip(QComboBox(), "What starts the rule")
        for k, label in R.TRIGGERS.items():
            self.trigger.addItem(label, k)
        self.trigger.setCurrentIndex(max(self.trigger.findData(rule.trigger), 0))
        self.column = tip(QComboBox(), "The column a video is moved to")
        for c in columns:
            self.column.addItem(c.name, c.primary)
        self.column.setCurrentIndex(max(self.column.findData(rule.column), 0))
        self.trigger.currentIndexChanged.connect(lambda: self.column.setVisible(self.trigger.currentData() == "moved"))
        self.column.setVisible(rule.trigger == "moved")
        wrow.addWidget(self.trigger, 1)
        wrow.addWidget(self.column)
        lay.addLayout(wrow)

        irow = QHBoxLayout()
        irow.addWidget(QLabel("<b>If</b>"))
        self.match = tip(QComboBox(), "Whether every condition must be true, or just one")
        self.match.addItem("all of these are true", "all")
        self.match.addItem("any of these is true", "any")
        self.match.setCurrentIndex(0 if rule.match == "all" else 1)
        irow.addWidget(self.match)
        irow.addStretch()
        add_c = tip(QPushButton("Add Condition"), "Only act on videos that match, e.g. a word in the caption")
        add_c.clicked.connect(lambda: self._add_condition())
        irow.addWidget(add_c)
        lay.addLayout(irow)
        self.cond_box = QVBoxLayout()
        lay.addLayout(self.cond_box)
        self.cond_rows: list[ConditionRow] = []
        self.no_conditions = _muted("No conditions: the rule applies to every video.", "faint")
        lay.addWidget(self.no_conditions)

        trow = QHBoxLayout()
        trow.addWidget(QLabel("<b>Then</b>"))
        trow.addStretch()
        add_a = tip(QPushButton("Add Action"), "Something else the rule does")
        add_a.clicked.connect(lambda: self._add_action())
        trow.addWidget(add_a)
        lay.addLayout(trow)
        self.act_box = QVBoxLayout()
        lay.addLayout(self.act_box)
        self.act_rows: list[ActionRow] = []
        lay.addWidget(_muted("Templates only fill empty fields. Scheduling only happens if the ticket is ready. "
                             "Each rule acts on a video once.", "faint"))
        self.enabled = QCheckBox("Rule is on")
        self.enabled.setChecked(rule.enabled)
        lay.addWidget(self.enabled)
        self.error = _muted("", "danger")
        lay.addWidget(self.error)

        # ---- try it on a real video before saving (nothing is changed)
        self.preview_box = QFrame(objectName="Card")
        pl = QVBoxLayout(self.preview_box)
        pl.setContentsMargins(14, 10, 14, 12)
        prow = QHBoxLayout()
        prow.addWidget(QLabel("<b>Test this rule on a video</b>"))
        prow.addStretch()
        self.test_video = tip(QComboBox(), "Pick any video on the board: you'll see what the rule would do to it. "
                                           "Nothing is changed.")
        self.test_video.setMinimumWidth(280)
        prow.addWidget(self.test_video)
        pl.addLayout(prow)
        self.preview_text = QLabel()
        self.preview_text.setWordWrap(True)
        self.preview_text.setTextFormat(Qt.RichText)
        pl.addWidget(self.preview_text)
        lay.addWidget(self.preview_box)
        self._fill_test_videos()
        self.test_video.currentIndexChanged.connect(self._update_preview)
        for signal in (self.name.textChanged, self.trigger.currentIndexChanged, self.column.currentIndexChanged,
                       self.match.currentIndexChanged):
            signal.connect(self._preview_soon)

        lay.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Save)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        for c in rule.conditions:
            self._add_condition(c)
        for a in rule.actions or [R.Action("fill_copy", "tiktok_text", "")]:
            self._add_action(a)
        self._sync_empty()
        self._update_preview()

    def _add_condition(self, c: R.Condition | None = None) -> None:
        row = ConditionRow(c)
        row.removed.connect(self._remove_condition)
        row.changed.connect(self._preview_soon)
        self.cond_rows.append(row)
        self.cond_box.addWidget(row)
        self._sync_empty()

    def _remove_condition(self, row) -> None:
        self.cond_rows.remove(row)
        row.setParent(None)
        self._sync_empty()
        self._preview_soon()

    def _add_action(self, a: R.Action | None = None) -> None:
        row = ActionRow(self.columns, a)
        row.removed.connect(self._remove_action)
        row.changed.connect(self._preview_soon)
        self.act_rows.append(row)
        self.act_box.addWidget(row)

    def _remove_action(self, row) -> None:
        self.act_rows.remove(row)
        row.setParent(None)
        self._preview_soon()

    # ------------------------------------------------------------ preview
    def _fill_test_videos(self) -> None:
        if self.pipeline is None or self.profile_id is None:
            self.preview_box.setVisible(False)
            return
        try:
            cards = [c for cards in self.pipeline.board(self.profile_id).values() for c in cards]
        except Exception:  # noqa: BLE001 — the preview is a helper, never a blocker
            cards = []
        for card in sorted(cards, key=lambda c: c.title.lower()):
            self.test_video.addItem(card.title[:70], card.id)
        if not cards:
            self.preview_text.setText("Add videos to the board to test a rule on one.")
            self.test_video.setEnabled(False)

    def _preview_soon(self, *_args) -> None:
        self._preview_timer.start()

    def _column_name(self, stage: str) -> str:
        return next((c.name for c in self.columns if stage in c.stages), stage)

    def _update_preview(self) -> None:
        import html
        row_id = self.test_video.currentData() if self.preview_box.isVisible() or self.pipeline else None
        if row_id is None or self.pipeline is None:
            return
        try:
            pv = R.preview(self.pipeline, self.value(), row_id, column_name=self._column_name)
        except Exception as exc:  # noqa: BLE001
            self.preview_text.setText(html.escape(f"Can't preview this yet: {exc}"))
            return

        def yes(ok: bool) -> str:
            return "<span style='color:%s'><b>%s</b></span>" % ((theme.P.success, "Yes") if ok
                                                                else (theme.P.danger, "No"))
        lines = [f"Trigger matches: {yes(pv.trigger_matches)} &nbsp;<span style='color:{theme.P.text_muted}'>"
                 f"{html.escape(pv.trigger_note)}</span>",
                 f"Conditions met: {yes(pv.conditions_met)}"]
        for label, ok in pv.conditions:
            lines.append(f"&nbsp;&nbsp;&nbsp;{'✓' if ok else '✗'} {html.escape(label)}")
        if not pv.conditions:
            lines.append(f"&nbsp;&nbsp;&nbsp;<span style='color:{theme.P.text_muted}'>No conditions: every video "
                         "counts.</span>")
        verb = "Would do" if pv.trigger_matches and pv.conditions_met else "Would do (if it matched)"
        lines.append(f"<br><b>{verb}:</b>")
        for what, result in pv.actions:
            lines.append(f"&nbsp;&nbsp;&nbsp;{html.escape(what)} → <i>{html.escape(result)}</i>")
        self.preview_text.setText("<br>".join(lines))

    def _sync_empty(self) -> None:
        self.no_conditions.setVisible(not self.cond_rows)

    def value(self) -> R.Rule:
        return R.Rule(name=self.name.text().strip(), trigger=self.trigger.currentData(),
                      column=self.column.currentData() if self.trigger.currentData() == "moved" else "",
                      match=self.match.currentData(), conditions=[r.condition() for r in self.cond_rows],
                      actions=[r.action() for r in self.act_rows], enabled=self.enabled.isChecked(), id=self.rule.id)

    def _save(self) -> None:
        try:
            R.validate(self.value())
        except R.RuleError as exc:
            self.error.setText(str(exc))
            return
        self.accept()


class AutomationsPage(QWidget):
    def __init__(self, pipeline, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.pipeline, self.store = pipeline, pipeline.store
        self.profile = None
        self._recipe_columns = 0
        scroll, body, self.lay = scroll_page((24, 18, 24, 24), 12, body_name="Page", sideways=False)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        head = QHBoxLayout()
        title = QVBoxLayout()
        title.addWidget(QLabel("Automations", objectName="PageTitle"))
        title.addWidget(_muted("Automatically select platforms, fill default captions or hashtags, and schedule "
                               "videos when they land on your Board."))
        head.addLayout(title, 1)
        self.recipes_btn = tip(QToolButton(text="Recipes  ▾"), "Start from a ready-made automation: you see it and "
                                                                "can change it before it's saved")
        self.recipes_btn.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.recipes_btn)
        for key, (name, _desc) in R.RECIPES.items():
            menu.addAction(f"{name}…", lambda k=key: self.use_recipe(k))
        self.recipes_btn.setMenu(menu)
        head.addWidget(self.recipes_btn, 0, Qt.AlignTop)
        self.new_btn = tip(QPushButton("New Rule"), "Make a new automation")
        set_prop(self.new_btn, "primary")
        self.new_btn.clicked.connect(self.new_rule)
        head.addWidget(self.new_btn, 0, Qt.AlignTop)
        self.lay.addLayout(head)
        self.empty = self._build_empty_state()
        self.lay.addWidget(self.empty)
        self.rules_box = QVBoxLayout()
        self.rules_box.setSpacing(8)
        self.lay.addLayout(self.rules_box)
        self.activity_title = QLabel("Activity", objectName="DetailTitle")
        self.lay.addWidget(self.activity_title)
        self.log = QListWidget()
        self.log.setWordWrap(True)
        self.log.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.log.setMinimumHeight(220)
        tip(self.log, "What each rule did, newest first")
        self.lay.addWidget(self.log)
        self.lay.addStretch()

    def _build_empty_state(self) -> QWidget:
        """No rules yet: what automations are for, one button to make one, and starter recipes."""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 24, 0, 8)
        lay.setSpacing(10)
        mark = QLabel(objectName="EmptyIcon")
        mark.setPixmap(icons.pixmap("bolt", theme.P.accent_color, 44))
        mark.setAlignment(Qt.AlignCenter)
        self.empty_mark = mark
        title = QLabel("Automate your workflow", objectName="EmptyTitle")
        title.setAlignment(Qt.AlignCenter)
        body = _muted("Rules do the repetitive parts for you. They only ever act on new videos, and everything "
                      "they do shows under Activity.")
        body.setAlignment(Qt.AlignCenter)
        body.setMaximumWidth(620)
        self.empty_body = body
        create = tip(QPushButton("Create Rule"), "Start a rule from scratch")
        set_prop(create, "primary")
        create.clicked.connect(self.new_rule)
        self.create_btn = create
        for widget in (mark, title):
            lay.addWidget(widget, 0, Qt.AlignHCenter)
        # centred by stretches, not an alignment flag: an aligned word-wrapped label is sized to its one-line
        # hint and gets too little height for its lines, so the text was cut off
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(body, 100)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(self._steps_strip())
        lay.addWidget(create, 0, Qt.AlignHCenter)
        lay.addSpacing(18)
        section = QLabel("Quick-start recipes")
        section.setProperty("section", True)
        tip(section, "Ready-made rules: add one, then edit it to fit")
        lay.addWidget(section)
        grid = QGridLayout()
        grid.setSpacing(12)
        self.recipe_grid = grid
        self.recipe_cards: list[QFrame] = []
        self.recipe_buttons: dict[str, QPushButton] = {}
        for key, (name, desc) in R.RECIPES.items():
            card = QFrame(objectName="Card")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(16, 14, 16, 14)
            cl.setSpacing(8)
            cl.addWidget(QLabel(name, objectName="DetailTitle"))
            text = _muted(desc)
            text.setAlignment(Qt.AlignLeft | Qt.AlignTop)     # short descriptions sit under the title, not mid-card
            cl.addWidget(text, 1)
            add = tip(QPushButton("Use Recipe…"), f"Open “{name}” ready to go: check it, change anything, then "
                                                  "save. It starts with videos added from now on.")
            add.clicked.connect(lambda _=False, k=key: self.use_recipe(k))
            self.recipe_buttons[key] = add
            cl.addWidget(add, 0, Qt.AlignLeft)
            self.recipe_cards.append(card)
        lay.addLayout(grid)
        self._place_recipes()
        return w

    def _steps_strip(self) -> QWidget:
        """How a rule works, in three steps."""
        strip = QWidget()
        row = QHBoxLayout(strip)
        row.setContentsMargins(0, 6, 0, 6)
        row.setSpacing(10)
        row.addStretch(1)
        self.steps: list[QFrame] = []
        for n, (head, text) in enumerate((("When a video arrives", "Added to the Board, or moved to a column"),
                                          ("Optional filter", "Only videos with a word, hashtag, length or "
                                                              "source"),
                                          ("Auto-prep or schedule", "Pick platforms, fill captions and hashtags, "
                                                                    "or schedule it")), 1):
            if n > 1:
                arrow = QLabel("→")
                arrow.setProperty("faint", True)
                row.addWidget(arrow)
            step = QFrame(objectName="Card")
            step.setMaximumWidth(264)
            sl = QVBoxLayout(step)
            sl.setContentsMargins(14, 10, 14, 10)
            sl.setSpacing(2)
            head_label = QLabel(f"{n}. {head}", objectName="DetailTitle")
            head_label.setWordWrap(True)
            sl.addWidget(head_label)
            sl.addWidget(_muted(text, "faint"))
            self.steps.append(step)
            row.addWidget(step, 3)
        row.addStretch(1)
        return strip

    def _place_recipes(self) -> None:
        from .grid import balanced_columns, max_columns_for
        columns = balanced_columns(len(self.recipe_cards), max_columns_for((self.width() or 720) - 48, medium=560))
        if columns == self._recipe_columns:
            return
        self._recipe_columns = columns
        for card in self.recipe_cards:
            self.recipe_grid.removeWidget(card)
        for n, card in enumerate(self.recipe_cards):
            self.recipe_grid.addWidget(card, n // columns, n % columns)
        for c in range(len(self.recipe_cards)):
            self.recipe_grid.setColumnStretch(c, 1 if c < columns else 0)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_recipes()

    def use_recipe(self, key: str) -> None:
        """Open a recipe's rule(s) in the rule editor, filled in; only what's saved is added."""
        if not self.profile:
            return
        try:
            rules = R.recipe(key)
        except R.RuleError as exc:
            QMessageBox.warning(self, "Automations", str(exc))
            return
        name = R.RECIPES[key][0]
        columns = load_columns(self.store, self.profile.id)
        kept: list[R.Rule] = []
        for n, rule in enumerate(rules, 1):
            count = f" ({n} of {len(rules)})" if len(rules) > 1 else ""
            dialog = RuleDialog(rule, columns, self, self.pipeline, self.profile.id,
                                title=f"Use Recipe: {name}{count}",
                                intro="Here's the recipe, filled in. Change anything you like, then Save. It "
                                      "applies to videos added from now on.")
            if dialog.exec() == QDialog.Accepted:
                kept.append(dialog.value())
        if kept:
            self._save_all(R.load(self.store, self.profile.id) + kept)

    def add_recipe(self, key: str) -> None:
        """Add a recipe as it is, without opening it (the API and tests use this)."""
        if not self.profile:
            return
        try:
            added = R.install_recipe(self.store, self.profile.id, key)
        except R.RuleError as exc:
            QMessageBox.warning(self, "Automations", str(exc))
            return
        self.refresh()
        names = ", ".join(r.name for r in added)
        QMessageBox.information(self, "Recipe added", f"Added {names}. It applies to videos added from now on; "
                                "use Edit… to change it, or Run on Existing Videos… to apply it to the board.")

    def set_profile(self, profile) -> None:
        self.profile = profile
        self.refresh()

    def _column_name(self, stage: str) -> str:
        cols = load_columns(self.store, self.profile.id) if self.profile else []
        return next((c.name for c in cols if stage in c.stages), stage)

    def refresh(self) -> None:
        while self.rules_box.count():
            w = self.rules_box.takeAt(0).widget()
            if w:
                w.setParent(None)
        if not self.profile:
            return
        rules = R.load(self.store, self.profile.id)
        self.empty.setVisible(not rules)
        self.new_btn.setVisible(bool(rules))           # the empty state has its own Create Rule and recipes
        self.recipes_btn.setVisible(bool(rules))
        self.empty_mark.setPixmap(icons.pixmap("bolt", theme.P.accent_color, 44))
        for rule in rules:
            self.rules_box.addWidget(self._card(rule))
        self.log.clear()
        for e in R.activity(self.store, self.profile.id):
            self.log.addItem(f"{local_text(e.get('at'))} · {e.get('rule')} · {e.get('title')}: "
                             + "; ".join(e.get("done", [])))
        if not self.log.count():
            self.log.addItem("Nothing yet. When a rule acts on a video, it shows here.")
        has_activity = bool(R.activity(self.store, self.profile.id))
        self.activity_title.setVisible(bool(rules) or has_activity)
        self.log.setVisible(bool(rules) or has_activity)
        read_only = getattr(self.store, "read_only", False)
        for b in [self.new_btn, self.create_btn, self.recipes_btn, *self.recipe_buttons.values()]:
            b.setEnabled(not read_only)

    def _card(self, rule: R.Rule) -> QFrame:
        frame = QFrame(objectName="Card")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(14, 10, 14, 10)
        top = QHBoxLayout()
        name = QLabel(f"<b>{rule.name}</b>" + ("" if rule.enabled else "  <i>(off)</i>"))
        top.addWidget(name, 1)
        toggle = tip(QPushButton("Turn Off" if rule.enabled else "Turn On"),
                     "Turning a rule on doesn't touch videos already on the board")
        toggle.clicked.connect(lambda: self._toggle(rule))
        edit = QPushButton("Edit…")
        edit.clicked.connect(lambda: self.edit_rule(rule))
        existing = tip(QPushButton("Run on Existing Videos…"),
                       "Apply this rule to matching videos already on the board (you'll see how many first)")
        existing.clicked.connect(lambda: self.run_existing(rule))
        delete = QPushButton("Delete")
        set_prop(delete, "flat")
        delete.clicked.connect(lambda: self._delete(rule))
        for b in (toggle, edit, existing, delete):
            b.setEnabled(not getattr(self.store, "read_only", False))
            top.addWidget(b)
        fl.addLayout(top)
        fl.addWidget(_muted(rule.summary(self._column_name), "faint"))
        return frame

    def _save_all(self, rules: list[R.Rule]) -> bool:
        try:
            R.save(self.store, self.profile.id, rules)
        except R.RuleError as exc:
            QMessageBox.warning(self, "Automations", str(exc))
            return False
        self.refresh()
        return True

    def new_rule(self) -> None:
        if self.profile:
            self.edit_rule(R.Rule(name=""), new=True)

    def edit_rule(self, rule: R.Rule, new: bool = False) -> None:
        dialog = RuleDialog(rule, load_columns(self.store, self.profile.id), self, self.pipeline, self.profile.id)
        if dialog.exec() != QDialog.Accepted:
            return
        updated = dialog.value()
        rules = R.load(self.store, self.profile.id)
        rules = rules + [updated] if new else [updated if r.id == updated.id else r for r in rules]
        self._save_all(rules)

    def _toggle(self, rule: R.Rule) -> None:
        rules = R.load(self.store, self.profile.id)
        for r in rules:
            if r.id == rule.id:
                r.enabled = not r.enabled
        self._save_all(rules)

    def _delete(self, rule: R.Rule) -> None:
        if QMessageBox.question(self, "Delete Rule", f"Delete “{rule.name}”? What it already did stays.") \
                == QMessageBox.Yes:
            self._save_all([r for r in R.load(self.store, self.profile.id) if r.id != rule.id])

    def run_existing(self, rule: R.Rule) -> None:
        n = len(R.matching_existing(self.pipeline, self.profile.id, rule))
        if not n:
            QMessageBox.information(self, "Run on Existing Videos", "No videos on the board match this rule.")
            return
        if QMessageBox.question(self, "Run on Existing Videos",
                                f"{n} video{'s' if n != 1 else ''} on the board match “{rule.name}”. Apply it to "
                                f"{'them' if n != 1 else 'it'} now?\n\n" + rule.summary(self._column_name)) \
                != QMessageBox.Yes:
            return
        entries = R.run_on_existing(self.pipeline, self.profile.id, rule)
        self.refresh()
        QMessageBox.information(self, "Run on Existing Videos", f"Done: {len(entries)} video"
                                f"{'s' if len(entries) != 1 else ''}. See Activity for details.")
