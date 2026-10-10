"""Automations page: "When … if … then …" rules, and what they did."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .. import rules as R
from ..columns import load as load_columns
from ..text import local_text
from .theme import set_prop
from .tips import tip


def _muted(text: str, prop: str = "muted") -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setProperty(prop, True)
    return lab


class ConditionRow(QWidget):
    removed = Signal(object)

    def __init__(self, c: R.Condition | None = None, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.kind = QComboBox()
        for k, label in R.CONDITIONS.items():
            self.kind.addItem(label, k)
        self.value = QLineEdit()
        self.value.setPlaceholderText("e.g. sunglasses")
        x = tip(QPushButton("✕"), "Remove this condition")
        set_prop(x, "flat")
        x.clicked.connect(lambda: self.removed.emit(self))
        lay.addWidget(self.kind)
        lay.addWidget(self.value, 1)
        lay.addWidget(x)
        if c:
            self.kind.setCurrentIndex(max(self.kind.findData(c.kind), 0))
            self.value.setText(c.value)

    def condition(self) -> R.Condition:
        return R.Condition(self.kind.currentData(), self.value.text().strip())


class ActionRow(QWidget):
    removed = Signal(object)

    def __init__(self, columns, a: R.Action | None = None, parent=None):
        super().__init__(parent)
        self.columns = columns
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.kind = QComboBox()
        for k, label in R.ACTIONS.items():
            self.kind.addItem(label, k)
        self.field = QComboBox()
        self.value = QLineEdit()
        x = tip(QPushButton("✕"), "Remove this action")
        set_prop(x, "flat")
        x.clicked.connect(lambda: self.removed.emit(self))
        lay.addWidget(self.kind)
        lay.addWidget(self.field)
        lay.addWidget(self.value, 1)
        lay.addWidget(x)
        self.kind.currentIndexChanged.connect(self._sync)
        if a:
            self.kind.setCurrentIndex(max(self.kind.findData(a.kind), 0))
        self._sync()
        if a:
            self.field.setCurrentIndex(max(self.field.findData(a.field), 0))
            self.value.setText(a.value)

    def _sync(self) -> None:
        kind = self.kind.currentData()
        self.field.clear()
        options = {"fill_copy": R.COPY_FIELDS, "set_ticket": R.TICKET_FIELDS_SETTABLE,
                   "move": {c.primary: c.name for c in self.columns}}.get(kind)
        for key, label in (options or {}).items():
            self.field.addItem(label, key)
        self.field.setVisible(bool(options))
        self.value.setVisible(kind != "move")
        self.value.setPlaceholderText({
            "fill_copy": "Template, e.g. {hook} ☀️ Link in bio {hashtags}",
            "add_hashtags": "#summer #sunnies",
            "set_platforms": "TikTok, Instagram, YouTube",
            "set_ticket": "e.g. Eyewear",
            "schedule": "Tuesday 09:00"}.get(kind, ""))
        tip(self.value, "Placeholders: " + " ".join(R.PLACEHOLDERS) if kind in ("fill_copy", "set_ticket") else
            "Weekday and time (your local time). Only scheduled if the ticket is ready." if kind == "schedule" else
            "")

    def action(self) -> R.Action:
        kind = self.kind.currentData()
        return R.Action(kind, self.field.currentData() or "" if self.field.isVisibleTo(self) or kind == "move"
                        else "", self.value.text().strip() if kind != "move" else "")


class RuleDialog(QDialog):
    """Edit one rule."""

    def __init__(self, rule: R.Rule, columns, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Automation")
        self.resize(720, 560)
        self.rule, self.columns = rule, columns
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(10)
        self.name = QLineEdit(rule.name)
        self.name.setPlaceholderText("Name, e.g. Sunglasses → summer template, Tuesdays")
        lay.addWidget(self.name)

        lay.addWidget(QLabel("<b>When</b>"))
        wrow = QHBoxLayout()
        self.trigger = QComboBox()
        for k, label in R.TRIGGERS.items():
            self.trigger.addItem(label, k)
        self.trigger.setCurrentIndex(max(self.trigger.findData(rule.trigger), 0))
        self.column = QComboBox()
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
        self.match = QComboBox()
        self.match.addItem("all of these are true", "all")
        self.match.addItem("any of these is true", "any")
        self.match.setCurrentIndex(0 if rule.match == "all" else 1)
        irow.addWidget(self.match)
        irow.addStretch()
        add_c = QPushButton("Add Condition")
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
        add_a = QPushButton("Add Action")
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

    def _add_condition(self, c: R.Condition | None = None) -> None:
        row = ConditionRow(c)
        row.removed.connect(self._remove_condition)
        self.cond_rows.append(row)
        self.cond_box.addWidget(row)
        self._sync_empty()

    def _remove_condition(self, row) -> None:
        self.cond_rows.remove(row)
        row.setParent(None)
        self._sync_empty()

    def _add_action(self, a: R.Action | None = None) -> None:
        row = ActionRow(self.columns, a)
        row.removed.connect(self._remove_action)
        self.act_rows.append(row)
        self.act_box.addWidget(row)

    def _remove_action(self, row) -> None:
        self.act_rows.remove(row)
        row.setParent(None)

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
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget(objectName="Page")
        self.lay = QVBoxLayout(body)
        self.lay.setContentsMargins(24, 18, 24, 24)
        self.lay.setSpacing(12)
        scroll.setWidget(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        head = QHBoxLayout()
        title = QVBoxLayout()
        title.addWidget(QLabel("Automations", objectName="PageTitle"))
        title.addWidget(_muted("Rules that work your tickets for you: “When a video is added, if its caption "
                               "mentions sunglasses, fill the TikTok caption from a template and schedule it for "
                               "next Tuesday.”"))
        head.addLayout(title, 1)
        self.new_btn = tip(QPushButton("New Rule"), "Make a new automation")
        set_prop(self.new_btn, "primary")
        self.new_btn.clicked.connect(self.new_rule)
        head.addWidget(self.new_btn, 0, Qt.AlignTop)
        self.lay.addLayout(head)
        self.rules_box = QVBoxLayout()
        self.rules_box.setSpacing(8)
        self.lay.addLayout(self.rules_box)
        self.lay.addWidget(QLabel("Activity", objectName="DetailTitle"))
        self.log = QListWidget()
        self.log.setMinimumHeight(220)
        tip(self.log, "What each rule did, newest first")
        self.lay.addWidget(self.log)
        self.lay.addStretch()

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
        if not rules:
            self.rules_box.addWidget(_muted("No automations yet. New Rule makes one.", "faint"))
        for rule in rules:
            self.rules_box.addWidget(self._card(rule))
        self.log.clear()
        for e in R.activity(self.store, self.profile.id):
            self.log.addItem(f"{local_text(e.get('at'))} · {e.get('rule')} · {e.get('title')}: "
                             + "; ".join(e.get("done", [])))
        if not self.log.count():
            self.log.addItem("Nothing yet. When a rule acts on a video, it shows here.")
        read_only = getattr(self.store, "read_only", False)
        self.new_btn.setEnabled(not read_only)

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
        dialog = RuleDialog(rule, load_columns(self.store, self.profile.id), self)
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
