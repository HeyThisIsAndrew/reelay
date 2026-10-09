"""UX clarity pass: plain wording, tooltips everywhere (with an off switch),
naming picker, growing search, guided account setup, Help, default profile.
All sign-ins here use fakes; nothing talks to Google, Bluesky or Meta."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPoint
from PySide6.QtGui import QHelpEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from reelay.models import VideoItem
from reelay.naming import DEFAULT_TEMPLATE
from reelay.paths import db_path
from reelay.store import Store
from reelay.tokens import MemoryTokenStore
from reelay.youtube import OAuthClient, Tokens

pytestmark = pytest.mark.gui


@pytest.fixture
def ui(qtbot, tmp_path, monkeypatch):
    from reelay.gui.app import create_window
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    store = Store(db_path())
    tokens = MemoryTokenStore()
    window = create_window(QApplication.instance(), store, tokens)  # first launch: no profiles, no accounts
    qtbot.addWidget(window)
    window.show()
    yield window, store, tokens
    window.close()
    store.close()


# ------------------------------------------------------------------ first launch & default profile
def test_first_launch_opens_an_empty_my_channel(ui):
    window, store, _ = ui
    assert window.profile.name == "My Channel"
    assert store.list_accounts(window.profile.id) == [] and store.list_sources(window.profile.id) == []
    assert window.download.list_stack.currentIndex() == 0          # empty state, not a blank table
    window.go("accounts")
    assert window.accounts_page.setup_box.isVisible()               # guided setup offered


def test_default_profile_is_the_first_one_made(tmp_path):
    store = Store(tmp_path / "s.db")
    mine = store.create_profile("My Channel", tmp_path / "a")
    lydia = store.create_profile("Lydia Livingood", tmp_path / "b")   # sorts first by name
    assert store.list_profiles()[0].id == lydia.id
    store.set_setting("active_profile", "")
    assert store.active_profile(tmp_path / "x").id == mine.id
    store.set_active_profile(lydia.id)                                # switching only changes what opens
    assert store.active_profile(tmp_path / "x").id == lydia.id and store.get_profile(lydia.id).name
    store.close()


# ------------------------------------------------------------------ tooltips
def _bare_controls(window):
    """Controls a person can see and use that have no hover tip (Qt's own
    internals, like a date box's inner text field, are skipped)."""
    from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QAbstractSpinBox, QComboBox, QLabel,
                                   QLineEdit, QPlainTextEdit)
    bare = []
    for key, page in window.pages.items():
        for cls in (QAbstractButton, QLineEdit, QComboBox, QPlainTextEdit):
            for w in page.findChildren(cls):
                if isinstance(w.parent(), (QAbstractSpinBox, QComboBox, QAbstractItemView)) or w.toolTip():
                    continue
                if isinstance(w, QAbstractButton) and not w.text().strip():
                    continue   # line-edit clear buttons, the table's corner button
                bare.append(f"{key}: {type(w).__name__} {getattr(w, 'text', lambda: '')() or ''}")
        bare += [f"{key}: section {lab.text()}" for lab in page.findChildren(QLabel)
                 if lab.property("section") and not lab.toolTip()]
    return bare


def test_every_control_and_section_has_a_tip(ui):
    window, *_ = ui
    window.go("board")
    assert _bare_controls(window) == []


def test_tips_switch_turns_every_tooltip_off(ui):
    window, store, _ = ui
    from reelay.gui import tips
    window.go("settings")
    box = window.settings_page.tips_box
    assert box.isChecked() and tips.enabled()
    target = window.download.fetch_btn
    event = QHelpEvent(QEvent.ToolTip, QPoint(1, 1), target.mapToGlobal(QPoint(1, 1)))
    assert not tips._filter.eventFilter(target, event)               # tips on: shown
    box.click()
    assert store.get_setting(tips.SETTING) == "0" and not tips.enabled()
    assert tips._filter.eventFilter(target, event)                   # tips off: swallowed
    box.click()
    assert tips.enabled()


# ------------------------------------------------------------------ wording
def test_download_page_says_in_and_out_plainly(ui):
    window, *_ = ui
    page = window.download
    assert page.fetch_btn.text() == "List Videos" and page.save_source_btn.text() == "Save This Link…"
    assert page.apply_btn.text() == "Rename Files…" and page.manifest_btn.text() == "Open Video List"
    assert "permission to manage" in page.doors_note.text()
    assert "{" not in page.naming.combo.currentText()                  # no raw tokens in labels


def test_tiktok_posting_is_described_truthfully(ui):
    window, *_ = ui
    from PySide6.QtWidgets import QLabel
    texts = " ".join(l.text() for l in window.accounts_page.findChildren(QLabel))
    assert "TikTok only allows automatic posting from developer apps it has reviewed and approved" in texts
    assert "private" in texts and "drafts" in texts


# ------------------------------------------------------------------ naming picker & search
def test_naming_picker_shows_friendly_names_and_a_live_example(ui):
    window, store, _ = ui
    page = window.download
    picker = page.naming
    assert picker.template() == DEFAULT_TEMPLATE and picker.example.text().startswith("Example: TikTok_")
    picker.set_template("{date}_{custom}")
    assert picker.combo.currentText() == "Date and your name" and not picker.pattern.isVisibleTo(picker)
    assert store.get_profile(window.profile.id).template == "{date}_{custom}"
    assert "letting-go.mp4" in picker.example.text()
    picker.set_template("{uploader}-{id}")                            # anything else → power-user box
    assert picker.combo.currentText() == "Custom pattern…" and picker.pattern.isVisibleTo(picker)
    picker.set_example_item(VideoItem(platform="TikTok", video_id="42", url="", uploader="lydia"))
    assert "lydia-42.mp4" in picker.example.text()


def test_search_grows_to_show_long_text_and_filters(ui, qtbot):
    window, *_ = ui
    search = window.download.search
    one_line = search.height()
    search.setPlainText("letting go " * 40)
    qtbot.waitUntil(lambda: search.height() > one_line, timeout=2000)
    assert search.height() <= one_line * 3 + 4                       # caps at three lines, then scrolls
    assert window.download.proxy._query == ("letting go " * 40).strip()     # the whole text filters
    search.clear()
    qtbot.waitUntil(lambda: search.height() == one_line, timeout=2000)


# ------------------------------------------------------------------ guided YouTube setup
def test_youtube_wizard_walks_through_and_saves_the_client(ui, qtbot):
    window, *_ = ui
    from reelay.gui.setup_dialogs import GOOGLE_STEPS, YouTubeSetupWizard
    wizard = YouTubeSetupWizard(window)
    qtbot.addWidget(wizard)
    for _ in GOOGLE_STEPS:
        assert wizard.next_btn.isEnabled()
        wizard.next_btn.click()
    assert wizard.next_btn.text() == "Save and Continue" and not wizard.next_btn.isEnabled()
    wizard.client_id.setText("not-a-client-id")
    wizard.client_secret.setText("s")
    assert not wizard.next_btn.isEnabled() and "googleusercontent" in wizard.paste_error.text()
    wizard.client_id.setText("123-abc.apps.googleusercontent.com")
    assert wizard.next_btn.isEnabled()
    wizard.next_btn.click()
    assert wizard.result() == YouTubeSetupWizard.Accepted
    assert wizard.client == OAuthClient("123-abc.apps.googleusercontent.com", "s")


def test_sign_in_with_google_modal_connects_a_channel(ui, qtbot, monkeypatch):
    window, store, tokens = ui
    page = window.accounts_page
    window.accounts.set_oauth_client(OAuthClient("123-abc.apps.googleusercontent.com", "s"))
    page.refresh()
    assert page.connected_box.isVisibleTo(page) and page.connect_btn.text() == "Sign in with Google"
    from reelay.gui import setup_dialogs
    opened = []

    def fake_sign_in(oauth, open_browser, cancel):
        open_browser("https://accounts.google.com/o/oauth2/auth?fake")
        return {"id": "UCx", "title": "My Channel", "handle": "@me"}, Tokens("a", "r", 9e12)

    monkeypatch.setattr(window.accounts, "sign_in", fake_sign_in)
    monkeypatch.setattr(setup_dialogs.GoogleSignInDialog, "exec",
                        lambda self: (self.worker.start(), self.worker.wait(3000),
                                      QApplication.processEvents(), self.result())[-1])
    monkeypatch.setattr(setup_dialogs.webbrowser, "open", opened.append)
    page._connect()
    qtbot.waitUntil(lambda: bool(store.list_accounts(window.profile.id)), timeout=3000)
    assert store.list_accounts(window.profile.id)[0].display_name == "My Channel" and tokens.get("youtube:UCx")


def test_sign_in_modal_cancel_stops_waiting(qtbot):
    from reelay.gui.setup_dialogs import GoogleSignInDialog
    import threading
    started = threading.Event()

    def slow_sign_in(oauth, open_browser, cancel):
        started.set()
        cancel.wait(5)
        raise RuntimeError("cancelled")

    dialog = GoogleSignInDialog(slow_sign_in, OAuthClient("x", "y"), open_browser=lambda url: None)
    qtbot.addWidget(dialog)
    dialog.worker.start()
    assert started.wait(2)
    dialog.cancel_btn.click()
    assert dialog.cancel.is_set() and dialog.worker.wait(3000)


# ------------------------------------------------------------------ guided Bluesky / Meta setup
@pytest.mark.parametrize("platform", ["bluesky", "threads", "facebook"])
def test_token_setup_checks_before_saving(ui, qtbot, monkeypatch, platform):
    window, store, tokens = ui
    from reelay.gui.setup_dialogs import TokenSetupDialog
    card = window.accounts_page.platform_cards[platform]
    calls = []

    def verify(secret):
        calls.append(secret)
        if len(calls) == 1:
            from reelay.publishers.base import PublishError
            raise PublishError("That password didn't work.")
        return {"external_id": "id1", "display_name": "Lydia", "handle": "@lydia"}

    dialog = TokenSetupDialog(platform, card.spec.fields, verify, window)
    qtbot.addWidget(dialog)
    dialog.connected.connect(card._connected)
    dialog.connect_btn.click()
    assert "Fill in" in dialog.status.text() and not calls                    # nothing sent while empty
    for edit in dialog.inputs.values():
        edit.setText("value")
    dialog.connect_btn.click()
    qtbot.waitUntil(lambda: "didn't work" in dialog.status.text(), timeout=3000)
    assert store.list_accounts(window.profile.id, platform) == []              # failed check saves nothing
    dialog.connect_btn.click()
    qtbot.waitUntil(lambda: bool(store.list_accounts(window.profile.id, platform)), timeout=3000)
    account = store.list_accounts(window.profile.id, platform)[0]
    assert account.display_name == "Lydia" and window.accounts.has_credentials(account)


# ------------------------------------------------------------------ help
def test_help_is_in_the_sidebar_and_works_offline(ui):
    window, store, _ = ui
    window.go("download")
    window.nav_buttons["help"].click()
    assert window.stack.currentWidget() is window.help_page
    text = window.help_page.browser.toPlainText()
    assert "Reelay Help" in text and "Videos come in" in text and "Sign in with Google" in text
    assert store.get_setting("last_page") == "download"                       # Help never becomes the start page


# ------------------------------------------------------------------ selection on the Download page
def _list_videos(window, qtbot, monkeypatch, n=4):
    import reelay.gui.workers as workers
    items = [VideoItem(platform="TikTok", video_id=str(500 + i), url=f"https://www.tiktok.com/@x/video/{500 + i}",
                       description=f"{'letting go' if i % 2 else 'healing'} caption {i}", upload_date=f"2026100{i + 1}")
             for i in range(n)]
    monkeypatch.setattr(workers, "list_videos", lambda url, opts, limit=None: ("x", [VideoItem(**i.__dict__)
                                                                                   for i in items]))
    page = window.download
    page.url.setText("https://www.tiktok.com/@livingood.lifestyle")
    page._fetch()
    qtbot.waitUntil(lambda: page.fetch_btn.isEnabled() and page.model.rowCount() == n, timeout=5000)
    return page


def test_new_videos_arrive_unselected(ui, qtbot, monkeypatch):
    window, *_ = ui
    page = _list_videos(window, qtbot, monkeypatch)
    assert page.model.checked_items() == []
    assert page.count_label.text() == "None of 4 selected" and not page.download_btn.isEnabled()
    assert page.select_all_btn.isEnabled() and not page.unselect_all_btn.isEnabled()
    assert "Nothing is selected yet" in page.status.text()


def test_select_all_and_unselect_all(ui, qtbot, monkeypatch):
    window, *_ = ui
    page = _list_videos(window, qtbot, monkeypatch)
    page.select_all_btn.click()
    assert len(page.model.checked_items()) == 4 and page.download_btn.text() == "Download 4 Videos"
    assert not page.select_all_btn.isEnabled() and page.unselect_all_btn.isEnabled()
    page.unselect_all_btn.click()
    assert page.model.checked_items() == [] and page.status.text() == "Nothing selected."
    page.search.setPlainText("letting go")                         # Select All follows the search
    page.select_all_btn.click()
    assert {i.video_id for i in page.model.checked_items()} == {"501", "503"}
    assert "search" in page.status.text()


def test_selections_survive_a_reload_and_new_videos_stay_unselected(ui, qtbot, monkeypatch):
    window, *_ = ui
    page = _list_videos(window, qtbot, monkeypatch, n=3)
    first = page.model.items()[0].key
    page.model.set_checked([first], True)
    page.reload()                                                   # e.g. after changing folders
    assert [i.key for i in page.model.checked_items()] == [first]
    _list_videos(window, qtbot, monkeypatch, n=5)                   # list again: two new videos
    assert [i.key for i in page.model.checked_items()] == [first]


def test_tooltips_explain_the_selection_and_actually_show(ui, qtbot, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QToolTip
    from reelay.gui.table_model import Col
    window, *_ = ui
    page = _list_videos(window, qtbot, monkeypatch, n=2)
    model = page.model
    cell = model.index(0, Col.CHECK)
    assert "Not selected" in model.data(cell, Qt.ToolTipRole)
    page.select_all_btn.click()
    assert "Selected: downloads" in model.data(cell, Qt.ToolTipRole)
    assert "won't download" not in model.data(model.index(0, Col.STATUS), Qt.ToolTipRole)
    for widget in (page.select_all_btn, page.unselect_all_btn, page.select_btn, page.count_label, page.status,
                   page.download_btn, page.fetch_btn, page.naming.combo, page.search):
        assert len(widget.toolTip()) > 20, widget
    # a real hover shows the tip (tips are on by default), and the Settings switch hides it
    btn = page.unselect_all_btn
    point = btn.rect().center()
    QApplication.sendEvent(btn, QHelpEvent(QEvent.ToolTip, point, btn.mapToGlobal(point)))
    qtbot.waitUntil(lambda: QToolTip.isVisible() and "Clear every selection" in QToolTip.text(), timeout=2000)
    QToolTip.hideText()
