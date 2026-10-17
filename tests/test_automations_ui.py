"""The Automations page in plain terms: a one-line purpose, an empty state with one set of buttons and a
three-step explainer, recipes that open filled in before anything is saved, and a rule editor with platform
toggles, a weekday-and-time picker, a source dropdown and Insert Variable chips."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from reelay import rules as R
from reelay.gui.automations_page import ActionRow, ConditionRow, RuleDialog

pytestmark = pytest.mark.gui


@pytest.fixture
def page(qtbot, tmp_path):
    from reelay.gui.app import create_window
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    store = Store(db_path())
    profile = store.create_profile("Be Unconventional", tmp_path / "lib")
    store.set_active_profile(profile.id)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    window.show()
    window.go("automations")
    yield window.automations_page, store, window
    window.close()
    store.close()


def test_header_says_what_automations_do_and_the_empty_state_has_one_set_of_buttons(page, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    p, store, window = page
    from PySide6.QtWidgets import QLabel
    texts = [lab.text() for lab in p.findChildren(QLabel)]
    assert ("Automatically select platforms, fill default captions or hashtags, and schedule videos when they "
            "land on your Board.") in texts
    assert p.empty.isVisibleTo(p) and not p.new_btn.isVisibleTo(p) and not p.recipes_btn.isVisibleTo(p)
    assert p.create_btn.isVisibleTo(p)
    steps = [s.findChildren(QLabel)[0].text() for s in p.steps]
    assert steps == ["1. When a video arrives", "2. Optional filter", "3. Auto-prep or schedule"]
    assert {b.text() for b in p.recipe_buttons.values()} == {"Use Recipe…"}
    p.add_recipe("ready_queue")
    assert not p.empty.isVisibleTo(p) and p.new_btn.isVisibleTo(p) and p.recipes_btn.isVisibleTo(p)


def test_use_recipe_opens_it_filled_in_and_only_saves_what_you_keep(page, monkeypatch):
    p, store, window = page
    seen = []

    def fake_exec(dialog):
        seen.append((dialog.windowTitle(), dialog.value()))
        if len(seen) == 2:
            return QDialog.Rejected                                  # skip the second of three
        dialog.name.setText(dialog.name.text() + " (mine)")
        return QDialog.Accepted
    monkeypatch.setattr(RuleDialog, "exec", fake_exec)
    p.use_recipe("hashtags")
    assert [t for t, _r in seen] == ["Use Recipe: Auto-hashtag by topic (1 of 3)",
                                     "Use Recipe: Auto-hashtag by topic (2 of 3)",
                                     "Use Recipe: Auto-hashtag by topic (3 of 3)"]
    assert seen[0][1].actions[0] == R.Action("add_hashtags", "", "#fitness #workout #fitnessmotivation")
    assert [r.name for r in R.load(store, window.profile.id)] == ["Hashtags: fitness (mine)",
                                                                   "Hashtags: recipe (mine)"]
    monkeypatch.setattr(RuleDialog, "exec", lambda d: QDialog.Rejected)
    p.use_recipe("cross_post")                                       # cancelled: nothing added
    assert len(R.load(store, window.profile.id)) == 2


def test_platforms_are_toggles_and_round_trip(qtbot):
    row = ActionRow([], R.Action("set_platforms", value="YouTube, Threads"))
    qtbot.addWidget(row)
    assert row.platforms.isVisibleTo(row) and not row.value.isVisibleTo(row)
    assert {k for k, b in row.platforms.pills.items() if b.isChecked()} == {"youtube", "threads"}
    assert all(not b.icon().isNull() and b.toolTip() for b in row.platforms.pills.values())
    row.platforms.pills["tiktok"].setChecked(True)
    row.platforms.pills["threads"].setChecked(False)
    action = row.action()
    assert action == R.Action("set_platforms", "", "YouTube, TikTok")
    assert R.parse_platforms(action.value) == ["youtube", "tiktok"]


def test_schedule_is_a_weekday_and_a_time(qtbot):
    from PySide6.QtCore import QTime
    row = ActionRow([], R.Action("schedule", value="Friday 17:30"))
    qtbot.addWidget(row)
    assert row.when.isVisibleTo(row) and not row.value.isVisibleTo(row)
    assert row.when.day.currentText() == "Friday" and row.when.time.time() == QTime(17, 30)
    row.when.day.setCurrentIndex(R.WEEKDAYS.index("Tuesday"))
    row.when.time.setTime(QTime(9, 5))
    assert row.action() == R.Action("schedule", "", "Tuesday 09:05")
    assert R.parse_when(row.action().value) is not None
    fresh = ActionRow([])
    qtbot.addWidget(fresh)
    fresh.kind.setCurrentIndex(fresh.kind.findData("schedule"))
    assert fresh.action().value == "Tuesday 09:00"                   # a sensible start, always valid
    R.validate(R.Rule(name="x", actions=[fresh.action()]))


def test_came_from_is_a_list_of_platforms(qtbot):
    row = ConditionRow(R.Condition("source", "tiktok"))
    qtbot.addWidget(row)
    assert row.source.isVisibleTo(row) and not row.value.isVisibleTo(row)
    assert [row.source.itemText(i) for i in range(row.source.count())] == ["TikTok", "Instagram", "YouTube",
                                                                           "Snapchat"]
    assert row.condition() == R.Condition("source", "TikTok")
    row.source.setCurrentIndex(row.source.findData("YouTube"))
    assert row.condition() == R.Condition("source", "YouTube")
    row.kind.setCurrentIndex(row.kind.findData("caption"))
    assert row.value.isVisibleTo(row) and not row.source.isVisibleTo(row)


def test_insert_variable_chips_put_placeholders_at_the_cursor(qtbot):
    row = ActionRow([], R.Action("fill_copy", "tiktok_text", "Link in bio"))
    qtbot.addWidget(row)
    assert row.chips.isVisibleTo(row)
    assert [b.text() for b in row.chip_buttons.values()] == ["+ Hook", "+ Original Caption", "+ Hashtags",
                                                             "+ Title", "+ Topic"]
    row.value.setCursorPosition(0)
    row.chip_buttons["{hook}"].click()
    row.value.end(False)
    row.chip_buttons["{hashtags}"].click()
    assert row.action().value == "{hook} Link in bio {hashtags}"
    for placeholder in row.chip_buttons:
        assert placeholder in R.PLACEHOLDERS
    row.kind.setCurrentIndex(row.kind.findData("add_hashtags"))
    assert not row.chips.isVisibleTo(row)


def test_the_cross_post_recipe_shows_its_platforms_and_sources_in_the_editor(page):
    p, store, window = page
    rule = R.recipe("cross_post")[0]
    dialog = RuleDialog(rule, [], p, title="Use Recipe: Cross-post short-form", intro="Filled in.")
    assert [r.condition() for r in dialog.cond_rows] == [R.Condition("source", "TikTok"),
                                                         R.Condition("source", "Instagram")]
    pills = dialog.act_rows[0].platforms.pills
    assert {k for k, b in pills.items() if b.isChecked()} == {"youtube", "threads"}
    assert dialog.value().actions == rule.actions                    # opening and saving changes nothing
    dialog.close()
