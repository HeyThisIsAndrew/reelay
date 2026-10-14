"""Automations (rules): matching, actions, once-per-video, never on the archive
by surprise, and the Ready gate for scheduling. Every platform is fake."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from reelay import rules as R
from reelay.models import Stage, VideoItem
from tests.fakes import FAKE_MP4, disable_publishing, make_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    disable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch, ready=False)
    yield w
    w.store.close()


def add_video(w, vid, description, duration=30):
    item = VideoItem(platform="TikTok", video_id=vid, url=f"https://www.tiktok.com/@x/video/{vid}",
                     description=description, title=description, duration=duration, uploader="livingood")
    item.filename, item.folder = f"{vid}.mp4", str(w.folder)
    (w.folder / item.filename).write_bytes(FAKE_MP4)
    return w.store.save_video(w.profile.id, item)


SUNNIES = R.Rule(name="Sunnies", conditions=[R.Condition("caption", "sunglasses")],
                 actions=[R.Action("fill_copy", "tiktok_text", "{hook} ☀️ {hashtags}"),
                          R.Action("add_hashtags", value="#summer #sunnies"),
                          R.Action("set_platforms", value="TikTok, Instagram")])


def test_parsing_and_dates():
    assert R.parse_when("Tuesday 09:00") == (1, time(9, 0)) and R.parse_when("tue 9:30") == (1, time(9, 30))
    assert R.parse_when("someday") is None and R.parse_when("Fri 25:00") is None
    assert R.parse_platforms("TikTok, instagram youtube, Myspace") == ["tiktok", "instagram", "youtube"]
    monday = datetime(2026, 10, 12, 8, 0).astimezone()
    nxt = R.next_occurrence(1, time(9, 0), monday).astimezone()
    assert (nxt.weekday(), nxt.hour, nxt.day) == (1, 9, 13)
    tuesday_late = datetime(2026, 10, 13, 10, 0).astimezone()
    assert R.next_occurrence(1, time(9, 0), tuesday_late).astimezone().day == 20     # next week, never the past


def test_validation_explains():
    with pytest.raises(R.RuleError, match="name"):
        R.validate(R.Rule(name=" ", actions=[R.Action("add_hashtags", value="#a")]))
    with pytest.raises(R.RuleError, match="at least one"):
        R.validate(R.Rule(name="x"))
    with pytest.raises(R.RuleError, match="weekday and time"):
        R.validate(R.Rule(name="x", actions=[R.Action("schedule", value="soon")]))
    with pytest.raises(R.RuleError, match="whole seconds"):
        R.validate(R.Rule(name="x", conditions=[R.Condition("shorter", "a minute")],
                          actions=[R.Action("add_hashtags", value="#a")]))


def test_new_rules_never_touch_the_archive_by_themselves(world):
    R.save(world.store, world.profile.id, [R.Rule.from_dict(SUNNIES.to_dict())])
    assert R.run(world.pipeline, world.profile.id) == []                     # existing videos: untouched
    new = add_video(world, "9001", "my favourite polarized sunglasses for the beach #beach")
    other = add_video(world, "9002", "morning routine")
    [entry] = R.run(world.pipeline, world.profile.id)
    assert entry["video"] == new and entry["rule"] == "Sunnies"
    post = world.store.get_post(new)
    assert post.tiktok_text.startswith("my favourite polarized sunglasses") and "#beach" in post.tiktok_text
    assert post.hashtags.endswith("#summer #sunnies") and post.target_platforms == ["tiktok", "instagram"]
    assert world.store.get_post(other) is None or world.store.get_post(other).tiktok_text in (None, "")
    world.pipeline.update_metadata(new, {"tiktok_text": "my own words"})
    assert R.run(world.pipeline, world.profile.id) == []                     # once per video: edits are safe
    assert world.store.get_post(new).tiktok_text == "my own words"
    assert R.activity(world.store, world.profile.id)[0]["done"][0] == "filled TikTok caption"


def test_templates_only_fill_empty_fields(world):
    row = add_video(world, "9003", "sunglasses haul")
    world.pipeline.update_metadata(row, {"tiktok_text": "already written", "hashtags": "#summer"})
    rule = R.Rule.from_dict(SUNNIES.to_dict())
    done = R.apply(world.pipeline, rule, row)
    assert done[0] == "TikTok caption already written: left alone" and done[1] == "added #sunnies"
    assert world.store.get_post(row).tiktok_text == "already written"


def test_conditions_all_or_any(world):
    row = world.store.get_video(add_video(world, "9004", "beach day #summer", duration=25))
    rule = R.Rule(name="r", conditions=[R.Condition("hashtag", "#summer"), R.Condition("shorter", "20")],
                  actions=[R.Action("add_hashtags", value="#x")])
    assert not R.matches(rule, row, None)
    rule.match = "any"
    assert R.matches(rule, row, None)
    assert R.matches(R.Rule(name="r", conditions=[R.Condition("source", "tiktok")]), row, None)
    assert not R.matches(R.Rule(name="r", conditions=[R.Condition("product", "blenders")]), row, None)


def test_schedule_goes_through_the_ready_gate(world):
    row = add_video(world, "9005", "sunglasses")
    rule = R.Rule(name="Tuesdays", actions=[R.Action("set_platforms", value="YouTube"),
                                            R.Action("schedule", value="Tuesday 09:00")])
    video = world.folder / "9005.mp4"
    video.rename(video.with_suffix(".moved"))                                 # file missing: not ready
    done = R.apply(world.pipeline, rule, row)
    assert done[1].startswith("couldn't schedule") and world.store.get_video(row).stage != Stage.SCHEDULED
    video.with_suffix(".moved").rename(video)
    done = R.apply(world.pipeline, rule, row, now=datetime.now(timezone.utc))
    assert done[1].startswith("scheduled for Tue"), done
    d = world.store.dispatches_for(row)[0]
    when = datetime.fromisoformat(d.publish_at.replace("Z", "+00:00")).astimezone()
    assert when.weekday() == 1 and when.hour == 9 and when > datetime.now(timezone.utc)


def test_moved_trigger_and_run_on_existing(world):
    rule = R.Rule(name="Staging tags", trigger="moved", column=Stage.STAGING,
                  actions=[R.Action("add_hashtags", value="#ready")])
    R.save(world.store, world.profile.id, [rule])
    world.pipeline.move(world.rows[0], Stage.STAGING)
    [entry] = R.run(world.pipeline, world.profile.id)
    assert entry["video"] == world.rows[0] and "#ready" in world.store.get_post(world.rows[0]).hashtags
    everyone = R.Rule(name="All", actions=[R.Action("set_ticket", "topic", "Wellness")])
    R.save(world.store, world.profile.id, [rule, everyone])
    assert R.run(world.pipeline, world.profile.id) == []
    assert len(R.matching_existing(world.pipeline, world.profile.id, everyone)) == 3    # the downloaded ones
    R.run_on_existing(world.pipeline, world.profile.id, everyone)
    assert world.store.get_video(world.rows[1]).ticket.topic == "Wellness"
    assert R.run(world.pipeline, world.profile.id) == []


def test_turning_a_rule_back_on_starts_from_now(world):
    rule = R.Rule(name="r", actions=[R.Action("add_hashtags", value="#x")])
    R.save(world.store, world.profile.id, [rule])
    rule.enabled = False
    R.save(world.store, world.profile.id, [rule])
    add_video(world, "9006", "while it was off")
    rule.enabled = True
    R.save(world.store, world.profile.id, [rule])
    assert R.run(world.pipeline, world.profile.id) == []


def test_automations_page_and_board(qtbot, tmp_path):
    from PySide6.QtWidgets import QApplication, QLabel

    from reelay.gui.app import create_window
    from reelay.gui.automations_page import RuleDialog
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    from tests.test_gui import seed_board
    store = Store(db_path())
    profile = store.create_profile("Lydia", tmp_path / "lib")
    store.set_active_profile(profile.id)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    window.go("automations")
    page = window.automations_page
    dialog = RuleDialog(R.Rule(name=""), window.board.column_config, page)
    dialog.name.setText("Great ones")
    dialog._add_condition(R.Condition("caption", "great"))
    dialog.act_rows[0].kind.setCurrentIndex(dialog.act_rows[0].kind.findData("add_hashtags"))
    dialog.act_rows[0].value.setText("#summer")
    rule = dialog.value()
    assert rule.conditions[0].value == "great" and rule.actions[0] == R.Action("add_hashtags", "", "#summer")
    assert page._save_all([rule])
    texts = [lab.text() for lab in page.findChildren(QLabel)]
    assert any("When: a video is added to the board. If: caption or copy contains “great”" in t for t in texts)
    items, ids = seed_board(store, window.profile, None)     # three videos arrive after the rule was made
    window.go("board")
    entries = R.activity(store, profile.id)
    assert len(entries) == 3 and all(e["rule"] == "Great ones" for e in entries)
    assert store.get_post(ids[items[0].key]).hashtags == "#summer"
    window.go("automations")
    assert page.log.count() == 3 and "added #summer" in page.log.item(0).text()
    window.close()
    store.close()


# ------------------------------------------------------------------ recipes and "Test this rule"
def test_recipes_install_and_start_from_now(world):
    w = world
    before = add_video(w, "9101", "my fitness routine")
    added = R.install_recipe(w.store, w.profile.id, "hashtags")
    assert [r.name for r in added] == ["Hashtags: fitness", "Hashtags: tech", "Hashtags: recipe"]
    assert len(R.load(w.store, w.profile.id)) == 3
    R.run(w.pipeline, w.profile.id)
    existing = w.store.get_post(before)
    assert existing is None or "#fitness" not in (existing.hashtags or "")      # already on the board: untouched
    after = add_video(w, "9102", "tech review of a new phone")
    R.run(w.pipeline, w.profile.id)
    assert "#techtok" in w.store.get_post(after).hashtags
    for key in R.RECIPES:
        for rule in R.recipe(key):
            R.validate(rule)                                       # every recipe is a valid rule
    with pytest.raises(R.RuleError):
        R.recipe("nope")


def test_preview_says_what_a_rule_would_do_without_doing_it(world):
    w = world
    vid = add_video(w, "9201", "these sunglasses are great #shades")
    pv = R.preview(w.pipeline, SUNNIES, vid)
    assert pv.trigger_matches and pv.conditions_met
    assert pv.conditions == [("Caption or copy contains “sunglasses”", True)]
    actions = dict(pv.actions)
    assert actions["TikTok caption"].startswith("these sunglasses are great")
    assert "#summer" in actions["Hashtags"] and actions["Platforms"] == "TikTok, Instagram"
    assert w.store.get_post(vid) is None or not w.store.get_post(vid).tiktok_text      # nothing was written
    miss = R.preview(w.pipeline, SUNNIES, add_video(w, "9202", "a calm morning"))
    assert not miss.conditions_met
    moved = R.Rule(name="Queue", trigger="moved", column="staging", actions=[R.Action("schedule", value="Tuesday 09:00")])
    pv = R.preview(w.pipeline, moved, vid)
    assert not pv.trigger_matches and "moved to" in pv.trigger_note
    assert "Tue" in dict(pv.actions)["Schedule"]


def test_empty_state_text_is_never_cut_off(qtbot, tmp_path):
    """The no-rules message wraps across its full width and gets the height its lines need (it was squeezed
    to a sliver when the label was centred by the layout)."""
    from PySide6.QtWidgets import QApplication

    from reelay.gui.app import create_window
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    store = Store(db_path())
    profile = store.create_profile("Be Unconventional", tmp_path / "lib")
    store.set_active_profile(profile.id)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    window.resize(1440, 900)
    window.show()
    window.go("automations")
    page = window.automations_page
    for width in (1440, 1000, 760):
        window.resize(width, 900)
        QApplication.processEvents()
        body = page.empty_body
        assert body.isVisible()
        assert body.width() >= min(600, page.empty.width() - 40)
        assert body.height() >= body.heightForWidth(body.width())
    window.close()
    store.close()
