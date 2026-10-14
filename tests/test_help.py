"""The Help Center: its content points at real pictures and real controls, and its views navigate."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QAbstractButton, QApplication, QLineEdit

from reelay.gui import help_content as H
from reelay.gui import help_page as P

PAGES = {"download", "board", "automations", "accounts", "settings"}


# ------------------------------------------------------------------ content
def _visuals():
    for step in H.TOUR:
        yield step.key, step.visual, step.steps
    for guide in H.GUIDES.values():
        yield guide.key, guide.visual, guide.steps


def test_every_picture_exists_in_both_themes_and_every_marker_is_measured():
    marks = P.marks()
    for key, visual, steps in _visuals():
        if visual is None or visual.shot in H.DRAWN:
            continue
        for dark in (True, False):
            assert P.shot_path(visual.shot, dark).exists(), (key, visual.shot, dark)
        measured = marks[visual.shot]["marks"]
        inside = P.visible_marks(visual)                       # within the part of the picture shown
        for step in steps:
            if step.mark:
                assert step.mark in measured and step.mark in inside, (key, step.mark)
                x, y, w, h = measured[step.mark]
                assert 0 <= x < 1 and 0 <= y < 1 and w > 0 and h > 0 and x + w <= 1.01 and y + h <= 1.01
        assert visual.alt, key                                    # every picture is described


def test_the_tour_has_six_short_steps_and_guides_link_to_real_places():
    assert [s.key for s in H.TOUR] == ["profiles", "library", "prepare", "readiness", "publish", "find"]
    for step in H.TOUR:
        assert 2 <= len(step.steps) <= 4 and step.goal and step.next
        assert not step.go or step.go in PAGES
    for guide in H.GUIDES.values():
        assert guide.doing and guide.expect and 1 <= len(guide.steps) <= 5
        assert all(r in H.GUIDES and r != guide.key for r in guide.related)
        assert not guide.go or guide.go in PAGES
    assert len(H.CATEGORIES) == 6 and all(c.guides for c in H.CATEGORIES)


def test_platform_wording_is_accurate():
    """Never implies Reelay posts to TikTok or Instagram, or that every platform schedules."""
    by_hand = {key for key, _l, who, _n in H.POSTING if who == "You post"}
    assert by_hand == {"tiktok", "instagram", "snapchat"}
    import re
    everything = " ".join(s.text for g in H.GUIDES.values() for s in g.steps) + " ".join(
        line for g in H.GUIDES.values() for d in g.details for line in d.lines)
    assert not re.search(r"Reelay (?:posts|publishes|schedules) (?:it )?(?:to|on) (?:TikTok|Instagram)", everything)
    posting = H.GUIDES["settings-posting"]
    assert any("restart" in s.text.lower() for s in posting.steps)          # the switches need a restart
    privacy = H.GUIDES["privacy-local"]
    assert "doesn't sync or back up" in privacy.expect


def test_search_finds_guides_by_what_people_want_to_do():
    assert H.search("schedule")[0][1] == "publish-schedule"
    assert ("guide", "publish-by-hand", "Post by hand") in H.search("tiktok")
    assert H.search("archive")[0][1] == "board-find"
    assert any(key == "library" for _k, key, _t in H.search("download"))
    assert H.search("sched youtube")                                          # prefixes, several words
    assert H.search("zzqx") == [] and H.search("   ") == []


def test_self_test_reads_every_picture():
    assert "6-step tour" in P.selftest()


# ------------------------------------------------------------------ the page
@pytest.fixture
def help_window(qtbot, tmp_path):
    from reelay.gui.app import create_window
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    store = Store(db_path())
    profile = store.create_profile("Be Unconventional", tmp_path / "lib")
    store.set_active_profile(profile.id)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    window.resize(1100, 680)                                  # the smallest window Reelay allows
    window.show()
    window.go("help")
    yield window, store
    window.close()
    store.close()


def test_topic_cards_open_guides_and_back_returns_home(help_window, qtbot):
    window, _store = help_window
    page = window.help_page
    assert page.current is page.home
    for card, category in zip(page.home.cards, H.CATEGORIES):
        card.click()
        assert page.current is page.guides and page.guides.guide.key == category.guides[0].key
        assert category.title in page.guides.crumb.text()
        for guide in category.guides:                         # every guide in the topic opens
            page.guides.nav_buttons[guide.key].click()
            assert page.guides.guide.key == guide.key and page.guides.nav_buttons[guide.key].isChecked()
        page.guides.back_btn.click()
        assert page.current is page.home


def test_related_links_details_and_take_me_there(help_window):
    window, _store = help_window
    page = window.help_page
    page.open_guide("publish-schedule")
    view = page.guides
    assert view.details and not view.details[0].body.isVisibleTo(view)           # details stay closed
    view.details[0].button.click()
    assert view.details[0].body.isVisibleTo(view)
    target = view.related_buttons[0]
    target.click()
    assert view.guide.key == "publish-which"
    page.open_guide("accounts-connect")
    view.go_btn.click()                                                            # Take me there
    assert window.stack.currentWidget() is window.accounts_page


def test_the_tour_moves_jumps_exits_and_remembers(help_window):
    window, store = help_window
    page = window.help_page
    page.home.tour_btn.click()
    tour = page.tour
    assert page.current is tour and tour.progress.text() == "Step 1 of 6"
    assert not tour.back_btn.isEnabled()
    for n in range(2, 7):
        tour.next_btn.click()
        assert tour.progress.text() == f"Step {n} of 6" and tour.chips[n - 1].isChecked()
    assert tour.next_btn.text() == "Finish"
    tour.back_btn.click()
    assert tour.progress.text() == "Step 5 of 6"
    tour.chips[2].click()                                                          # jump to step 3
    assert tour.step.key == "prepare" and store.get_setting(P.TOUR_SETTING) == "2"
    tour.exit_btn.click()
    assert page.current is page.home and "Step 3 of 6" in page.home.tour_btn.text()
    page.home.tour_btn.click()                                                     # picks up where it was
    assert tour.step.key == "prepare"
    tour.go_btn.click()                                                            # Take me there: the Board
    assert window.stack.currentWidget() is window.board
    window.go("help")
    page.start_tour(5)
    tour.next_btn.click()                                                          # Finish
    assert page.current is page.home and page.home.tour_btn.text() == "Start the Tour"


def test_search_lists_results_and_return_opens_the_first(help_window):
    window, _store = help_window
    home = window.help_page.home
    home.search.setText("archive")
    assert home.results.isVisibleTo(home) and not home.topics.isVisibleTo(home)
    assert home.result_buttons and "Find, filter and archive" in home.result_buttons[0].text()
    home.search.returnPressed.emit()
    assert window.help_page.current is window.help_page.guides
    assert window.help_page.guides.guide.key == "board-find"
    home.search.setText("zzqx")
    assert not home.result_buttons
    home.search.clear()
    assert home.topics.isVisibleTo(home)


def test_pictures_load_and_fit_the_smallest_window(help_window, qtbot):
    window, _store = help_window
    page = window.help_page
    page.start_tour(1)
    QApplication.processEvents()
    shot = page.tour.visual
    assert isinstance(shot, P.HelpShot) and not shot.pixmap().isNull()
    assert shot.width() >= 360 and abs(shot.height() - shot.width() * shot.ratio) <= 1
    assert shot.geometry().right() <= page.tour.width()                        # nothing runs off the side
    assert page.tour.left.width() >= 280
    assert shot.numbered and shot.accessibleName() and shot.accessibleDescription()
    page.open_guide("publish-which")
    QApplication.processEvents()
    assert isinstance(page.guides.visual, P.PlatformChart)
    page.open_guide("privacy-local")
    QApplication.processEvents()
    assert isinstance(page.guides.visual, P.StorageDiagram)
    assert page.grab().width() > 0


def test_every_visible_control_has_a_tooltip(help_window):
    window, _store = help_window
    page = window.help_page
    for show in (page.show_home, lambda: page.start_tour(0), lambda: page.open_guide("board-captions")):
        show()
        QApplication.processEvents()
        controls = page.current.findChildren(QAbstractButton) + page.current.findChildren(QLineEdit)
        for w in controls:
            if isinstance(w.parentWidget(), QLineEdit):          # Qt's own clear button inside a field
                continue
            if w.isVisibleTo(page.current):
                assert w.toolTip(), (type(w).__name__, getattr(w, "text", lambda: "")())
