"""Per-platform tags: Going / Scheduled / Posted for every platform (TikTok and
Instagram included), automatic card moves when every target is scheduled or
live, logos on cards, and the collapsible ticket sections. All fakes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import requests
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from reelay.models import DispatchStatus, Platform, Post, Stage
from reelay.pipeline import PipelineError
from tests.fakes import approve_and_run, SECRETS, disable_publishing, make_world

NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def world(tmp_path, monkeypatch):
    disable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch, ready=False)
    yield w
    w.store.close()


def stage(world, row):
    return world.store.get_video(row).stage


def test_hand_posted_platforms_move_the_card_on_their_own(world):
    p, row = world.pipeline, world.rows[0]
    p.set_targets(row, ["tiktok", "instagram"])
    assert stage(world, row) == Stage.BACKLOG
    p.set_platform_state(row, "tiktok", "scheduled", NOW + timedelta(hours=2), now=NOW)
    assert stage(world, row) == Stage.STAGING                       # being worked on; Instagram isn't scheduled
    p.set_platform_state(row, "instagram", "posted", now=NOW)
    assert stage(world, row) == Stage.SCHEDULED                     # every target scheduled or live
    states = p.platform_states(row, NOW)
    assert (states["tiktok"].state, states["instagram"].state) == ("scheduled", "published")
    assert p.advance_stages(world.profile.id, now=NOW + timedelta(hours=1)) == []
    assert p.advance_stages(world.profile.id, now=NOW + timedelta(hours=3)) == [row]   # TikTok's time passed
    assert stage(world, row) == Stage.PUBLISHED
    p.set_platform_state(row, "instagram", "none")                  # unticking moves it back
    assert stage(world, row) == Stage.STAGING
    history = p.publishing_history(row)
    assert history["tiktok"][0].method == "manual" and "instagram" not in history


def test_ticking_a_platform_makes_it_a_target_and_rules_hold(world):
    p, row = world.pipeline, world.rows[1]
    p.set_platform_state(row, "tiktok", "posted", now=NOW)
    post = world.store.get_post(row)
    assert post.target_platforms == ["tiktok"] and stage(world, row) == Stage.PUBLISHED
    with pytest.raises(PipelineError) as err:
        p.set_platform_state(row, "instagram", "posted", NOW + timedelta(days=1), now=NOW)
    assert err.value.code == "invalid_time"
    for bad in (("myspace", "posted"), ("tiktok", "maybe")):
        with pytest.raises(PipelineError):
            p.set_platform_state(row, *bad)
    assert world.store.manual_posts(row)[0].kind == "posted"


def test_reelay_posts_and_hand_posts_combine(world):
    p, store, row = world.pipeline, world.store, world.rows[0]
    p.update_metadata(row, {"description": "Ready to go.", "target_platforms": ["youtube", "tiktok"]})
    p.schedule(row, scheduled_at=NOW + timedelta(days=1))
    assert stage(world, row) == Stage.SCHEDULED
    with pytest.raises(PipelineError) as err:                       # Reelay handles YouTube itself
        p.set_platform_state(row, "youtube", "posted")
    assert err.value.code == "locked"
    with pytest.raises(PipelineError):                              # can't drop a platform Reelay is posting
        p.set_targets(row, ["tiktok"])
    p.set_targets(row, ["youtube", "tiktok", "instagram"])          # adding hand platforms is fine
    youtube = store.dispatches_for(row)[0]
    store.finish_dispatch(youtube.id, DispatchStatus.PUBLISHED, remote_id="YT1", remote_url="https://youtu.be/YT1")
    p.refresh_post(row)
    assert stage(world, row) == Stage.SCHEDULED                     # still waiting on TikTok and Instagram
    p.set_platform_state(row, "tiktok", "posted")
    p.set_platform_state(row, "instagram", "posted")
    assert stage(world, row) == Stage.PUBLISHED
    states = p.platform_states(row)
    assert states["youtube"].automatic and states["youtube"].state == "published"


def test_cards_without_marks_are_left_alone(world):
    p, row = world.pipeline, world.rows[2]
    p.set_targets(row, ["youtube", "tiktok"])
    p.move(row, Stage.STAGING)
    assert p.advance_stages(world.profile.id) == [] and stage(world, row) == Stage.STAGING
    card = p.card(row)
    assert {n for n, s in card.platform_states.items() if s.target} == {"youtube", "tiktok"}


def test_v6_to_v7_keeps_manual_posts_as_posted(tmp_path, monkeypatch):
    import reelay.store as store_module
    current = store_module.SCHEMA_VERSION
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", 6)
    old = store_module.Store(tmp_path / "v6.db")
    profile = old.create_profile("Lydia", tmp_path)
    old.conn.execute("INSERT INTO videos (profile_id, platform, video_id, data, updated_at) "
                     "VALUES (?, 'TikTok', '1', '{\"platform\": \"TikTok\", \"video_id\": \"1\", \"url\": \"u\"}', 'x')",
                     (profile.id,))
    old.conn.execute("INSERT INTO manual_posts (video_row, platform, posted_at, origin, created_at) "
                     "VALUES (1, 'tiktok', '2026-10-01T00:00:00Z', 'local', 'x')")
    old.conn.commit()
    old.close()
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", current)
    new = store_module.Store(tmp_path / "v6.db")
    assert new.conn.execute("PRAGMA user_version").fetchone()[0] == current
    assert [m.kind for m in new.manual_posts(1)] == ["posted"]
    new.close()


# ------------------------------------------------------------------ API
@pytest.fixture
def api(tmp_path, monkeypatch):
    from reelay.api import ApiServer, create_app
    disable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch, ready=False)
    server = ApiServer(create_app(w.store, w.pipeline, w.accounts, lambda: SECRETS["agent"]), host="127.0.0.1", port=0)
    assert server.start()
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {SECRETS['agent']}"
    s.base, s.world = server.url, w
    yield s
    server.stop()
    w.store.close()


def test_api_platform_tags(api):
    vid = api.world.rows[0]
    r = api.patch(f"{api.base}/v1/videos/{vid}", json={"target_platforms": ["tiktok", "instagram"]}, timeout=10)
    assert r.status_code == 200 and r.json()["target_platforms"] == ["tiktok", "instagram"]
    when = (NOW + timedelta(days=1)).isoformat()
    r = api.post(f"{api.base}/v1/videos/{vid}/platforms/tiktok", json={"state": "scheduled", "at": when}, timeout=10)
    assert r.status_code == 202 and api.world.store.manual_posts(vid) == []          # waits for a person
    approve_and_run(api.world.store, api.world.pipeline, r)
    states = api.world.pipeline.platform_states(vid)
    assert states["tiktok"].state == "scheduled" and not states["tiktok"].automatic
    assert states["instagram"].state == "none" and api.world.store.get_video(vid).stage == "staging"
    r = api.post(f"{api.base}/v1/videos/{vid}/platforms/instagram", json={"state": "posted"}, timeout=10)
    approve_and_run(api.world.store, api.world.pipeline, r)
    assert api.world.store.get_video(vid).stage == "scheduled"
    assert api.post(f"{api.base}/v1/videos/{vid}/platforms/myspace", json={"state": "posted"},
                    timeout=10).status_code == 422
    assert api.post(f"{api.base}/v1/videos/{vid}/platforms/tiktok", json={"state": "later"},
                    timeout=10).status_code == 422


# ------------------------------------------------------------------ desktop ticket
@pytest.fixture
def ticket(qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QMessageBox

    from reelay.gui.app import create_window
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    from tests.test_gui import seed_board
    disable_publishing(monkeypatch)
    QSettings().remove("sections")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    store = Store(db_path())
    profile = store.create_profile("Lydia", tmp_path / "lib")
    store.set_active_profile(profile.id)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    window.show()
    items, ids = seed_board(store, window.profile, None)
    row = ids[items[0].key]
    store.save_post(Post(video_row=row, title="Letting go", description="A note.", target_platforms=["tiktok"]))
    window.go("board")
    b = window.board
    col = b.columns[Stage.BACKLOG]
    b._on_select(col, next(col.model.index(r) for r, c in enumerate(col.model.cards) if c.id == row))
    yield window, store, b, row
    window.close()
    store.close()


def test_ticket_chips_and_menus_drive_the_card(ticket, monkeypatch):
    from reelay.gui import ticket_widgets
    _window, store, b, row = ticket
    panel = b.panel
    chips, sections = panel.platform_boxes, panel.sections
    assert chips["tiktok"].isChecked() and not chips["instagram"].isChecked()
    assert sections["tiktok"].isVisibleTo(panel) and not sections["instagram"].isVisibleTo(panel)
    assert sections["tiktok"].status.full_text() == "Preparing"
    chips["instagram"].click()                                      # commits straight away
    assert store.get_post(row).target_platforms == ["tiktok", "instagram"]
    assert sections["instagram"].isVisibleTo(panel) and sections["instagram"].expanded   # ticked: its box opens
    monkeypatch.setattr(ticket_widgets.MarkPostedDialog, "exec", lambda self: ticket_widgets.QDialog.Accepted)
    sections["tiktok"].act_posted.trigger()                         # asks when (and for the link); recorded now
    assert store.get_video(row).stage == Stage.STAGING             # Instagram still to do
    b.reload()

    def accepted(self):
        self.url.setText("https://www.instagram.com/reel/abc")
        return ticket_widgets.QDialog.Accepted
    monkeypatch.setattr(ticket_widgets.MarkPostedDialog, "exec", accepted)
    b.panel.sections["instagram"].act_posted.trigger()
    assert store.get_video(row).stage == Stage.PUBLISHED
    assert [m.url for m in store.manual_posts(row) if m.platform == "instagram"] == ["https://www.instagram.com/reel/abc"]
    b.reload()
    tiktok, instagram = b.panel.sections["tiktok"], b.panel.sections["instagram"]
    assert tiktok.status.full_text() == "Posted" and tiktok.status.toolTip().startswith("Live since")
    assert not tiktok.act_posted.isEnabled() and tiktok.act_preparing.isEnabled()
    assert instagram.link.isVisibleTo(instagram) and "instagram.com/reel/abc" in instagram.link.text()
    assert b.panel.picker.summary.text() == "0 preparing · 0 scheduled · 2 posted"
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a) or QMessageBox.No)
    b.panel.platform_boxes["instagram"].click()                     # marked Posted: asks first, and No keeps it
    assert asked and b.panel.platform_boxes["instagram"].isChecked()
    assert store.get_post(row).target_platforms == ["tiktok", "instagram"] and len(store.manual_posts(row)) == 2
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    b.panel.platform_boxes["instagram"].click()                     # not posting there after all
    assert store.get_post(row).target_platforms == ["tiktok"] and not store.manual_posts(row)[1:]
    assert not b.panel.sections["instagram"].isVisibleTo(b.panel)
    card = b.columns[Stage.PUBLISHED].model.cards[0]
    tip = b.columns[Stage.PUBLISHED].model.data(b.columns[Stage.PUBLISHED].model.index(0), 3)   # ToolTipRole
    assert card.id == row and "TikTok: Published" in tip and "Instagram: Published" in tip


def test_mark_posted_link_is_checked(ticket, monkeypatch):
    from reelay.gui import ticket_widgets
    _window, store, b, row = ticket
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))

    def wrong_site(self):
        self.url.setText("https://www.instagram.com/reel/abc")       # not a TikTok link
        return ticket_widgets.QDialog.Accepted
    monkeypatch.setattr(ticket_widgets.MarkPostedDialog, "exec", wrong_site)
    b.panel.sections["tiktok"].act_posted.trigger()
    assert warned == ["That isn't a TikTok link."] and not store.manual_posts(row)


def test_scheduled_asks_when_and_back_to_preparing(ticket, monkeypatch):
    from reelay.gui import ticket_widgets
    _window, store, b, row = ticket
    later = datetime.now(timezone.utc) + timedelta(days=2)
    monkeypatch.setattr(ticket_widgets.WhenDialog, "exec", lambda self: ticket_widgets.QDialog.Accepted)
    monkeypatch.setattr(ticket_widgets.WhenDialog, "when", lambda self: later)
    b.panel.sections["tiktok"].act_scheduled.trigger()
    [mark] = store.manual_posts(row)
    assert mark.kind == "scheduled" and store.get_video(row).stage == Stage.SCHEDULED
    b.reload()
    tiktok = b.panel.sections["tiktok"]
    assert tiktok.status.full_text().startswith("Scheduled · ") and not tiktok.act_scheduled.isEnabled()
    monkeypatch.setattr(ticket_widgets.WhenDialog, "exec", lambda self: ticket_widgets.QDialog.Rejected)
    b.panel.platform_boxes["instagram"].click()
    b.panel.sections["instagram"].act_scheduled.trigger()          # cancelled: nothing recorded
    assert len(store.manual_posts(row)) == 1
    b.reload()
    b.panel.sections["tiktok"].act_preparing.trigger()              # the same "none" mark as unticking
    assert not store.manual_posts(row) and store.get_video(row).stage == Stage.STAGING


def test_reelay_handled_platforms_cannot_be_changed_by_hand(ticket):
    from reelay.pipeline import PlatformState
    _window, _store, b, _row = ticket
    panel = b.panel
    states = {p: PlatformState(p, p in ("youtube", "tiktok")) for p in panel.sections}
    states["youtube"] = PlatformState("youtube", True, "scheduled", "2030-01-01T09:00:00Z", automatic=True)
    panel.set_platform_states(states, None)
    youtube = panel.sections["youtube"]
    assert not panel.platform_boxes["youtube"].isEnabled()            # can't untick what Reelay is sending
    assert not any(a.isEnabled() for a in (youtube.act_scheduled, youtube.act_posted, youtube.act_preparing))
    assert "by Reelay" in youtube.status.full_text() and "Reelay is handling" in youtube.menu_btn.toolTip()


def test_one_section_per_ticked_platform_with_logos_and_counts(ticket):
    _window, _store, b, _row = ticket
    panel = b.panel
    sections = panel.sections
    assert list(sections) == ["youtube", "tiktok", "instagram", "bluesky", "threads", "facebook"]
    assert {type(s) for s in sections.values()} == {type(sections["youtube"])}          # one widget class
    assert [p for p, s in sections.items() if s.isVisibleTo(panel)] == ["tiktok"]       # only the ticked one
    assert not panel.none_note.isVisibleTo(panel)
    assert not sections["tiktok"].toggle.icon().isNull()
    assert sections["tiktok"].count.full_text() == "empty"
    panel.blocks["tiktok_text"].set_text("letting go 🦋")
    assert sections["tiktok"].count.full_text() == "12/4,000"
    panel.blocks["bluesky_text"].set_text("x" * 301)
    assert sections["bluesky"].count.full_text() == "⚠ too long"
    panel.blocks["title"].set_text("T" * 120)
    assert sections["youtube"].count.full_text() == "⚠ too long"   # any of its boxes over
    assert sections["tiktok"].expanded
    sections["tiktok"].toggle.click()                               # the header folds it
    assert not sections["tiktok"].expanded and not sections["tiktok"].body.isVisibleTo(panel)
    for chip in panel.platform_boxes.values():                      # every chip keyboard-reachable, with a tip
        assert chip.focusPolicy() & Qt.TabFocus and chip.toolTip() and chip.isCheckable()


def test_new_ticket_opens_sections_calmly(ticket, monkeypatch):
    _window, store, b, row = ticket
    store.save_post(Post(video_row=row, title="Letting go", description="A note.",
                         target_platforms=["youtube", "tiktok", "instagram"]))
    b.reload()
    b.close_inspector()
    col = b.columns[Stage.BACKLOG]
    b._on_select(col, next(col.model.index(r) for r, c in enumerate(col.model.cards) if c.id == row))
    open_ = [p for p, s in b.panel.sections.items() if s.isVisibleTo(b.panel) and s.expanded]
    assert open_ == ["youtube"]                                     # three or more: only the first is open


def test_logos_render_for_every_platform(qapp):
    from reelay.gui.logos import platform_pixmap
    for name in Platform.LABELS:
        pix = platform_pixmap(name, 16)
        img = pix.toImage()
        assert not pix.isNull() and any(img.pixelColor(x, y).alpha() for x in range(0, 32, 3) for y in range(0, 32, 3))


# ------------------------------------------------------------------ audit fixes
def test_dispatcher_tick_settles_passed_times(tmp_path, monkeypatch):
    """Facebook schedules natively: once its time passes, the post is live. Hand-
    scheduled posts move their card on even when the Board isn't open."""
    from tests.fakes import connect_platform, enable_publishing
    enable_publishing(monkeypatch, Platform.FACEBOOK)
    w = make_world(tmp_path, monkeypatch, connect=(Platform.FACEBOOK,))
    p, store = w.pipeline, w.store
    fb_row, hand_row = w.rows[0], w.rows[1]
    when = NOW + timedelta(hours=1)
    p.schedule(fb_row, scheduled_at=when, platforms=["facebook"])
    [d] = store.dispatches_for(fb_row)
    store.claim_dispatch(d.id, "x")
    store.finish_dispatch(d.id, DispatchStatus.SCHEDULED, remote_id="fb1")
    p.set_platform_state(hand_row, "tiktok", "scheduled", when, now=NOW)
    p.set_targets(hand_row, ["tiktok"])
    assert p.settle_due(NOW) == 0
    assert stage(w, fb_row) == Stage.SCHEDULED and stage(w, hand_row) == Stage.SCHEDULED
    assert p.settle_due(when + timedelta(minutes=1)) >= 1
    assert store.dispatches_for(fb_row)[0].status == DispatchStatus.PUBLISHED
    assert stage(w, fb_row) == Stage.PUBLISHED and stage(w, hand_row) == Stage.PUBLISHED
    w.store.close()


def test_download_list_right_click_archive(ticket, monkeypatch):
    window, store, _b, row = ticket
    page = window.download
    page.reload()
    key = store.get_video(row).item.key
    item = page.model.find(key)
    page._archive([item])
    assert store.get_video(row).archived_at and page.model.find(key) is None
