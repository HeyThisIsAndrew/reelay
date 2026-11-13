"""The Library's list view: a clean default set of columns that people can show, hide, reorder and resize
(and get back to the default), full dates, a Platform column, and plain words in the cells."""

from __future__ import annotations

from datetime import datetime

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QHeaderView

from reelay.gui.table_model import DEFAULT_COLUMNS, HEADERS, Col, VideoTableModel
from reelay.models import Status, VideoItem


def _item(**kw):
    base = dict(platform="YouTube", video_id="a", url="https://www.youtube.com/watch?v=a",
                upload_date=f"{datetime.now().year}1012", width=1080, height=1920, duration=40,
                description="#tiktoklive #livehighlights   The best lens you're leaving at home #sony")
    return VideoItem(**{**base, **kw})


@pytest.fixture
def model(qtbot, tmp_path):
    from reelay.gui.thumbnails import ThumbnailCache
    m = VideoTableModel(ThumbnailCache(tmp_path))
    m.set_items([_item(), _item(video_id="b", width=1920, height=1080, duration=900),
                 _item(video_id="c", platform="TikTok", url="https://www.tiktok.com/@x/video/1")])
    return m


def _text(model, row, col):
    return model.data(model.index(row, col), Qt.DisplayRole)


def test_dates_always_show_the_year(model):
    assert _text(model, 0, Col.DATE) == f"Oct 12, {datetime.now().year}"


def test_platform_column_names_the_source_and_its_format(model):
    assert _text(model, 0, Col.PLATFORM) == "YouTube Short"
    assert _text(model, 1, Col.PLATFORM) == "YouTube"
    assert _text(model, 2, Col.PLATFORM) == "TikTok"
    assert model.data(model.index(0, Col.PLATFORM), Qt.DecorationRole) is not None          # its logo
    assert HEADERS[Col.PLATFORM] == "PLATFORM"


def test_plain_headers_and_a_readable_hook(model):
    assert HEADERS[Col.CUSTOM] == "FILE LABEL" and HEADERS[Col.HOOK] == "CAPTION / HOOK"
    assert _text(model, 0, Col.HOOK) == "The best lens you're leaving at home #sony"       # leading tags dropped
    only_tags = _item(video_id="d", description="#one #two")
    model.set_items([only_tags])
    assert _text(model, 0, Col.HOOK) == "#one #two"                                         # nothing else to show
    tip = model.headerData(Col.CUSTOM, Qt.Horizontal, Qt.ToolTipRole)
    assert "Optional custom label used when renaming files on disk" in tip


def test_status_says_what_happens_to_the_file(model):
    model.set_items([_item(), _item(video_id="b")])
    model.set_checked([model.item(0).key], False)
    model.set_checked([model.item(1).key], True)
    assert _text(model, 0, Col.STATUS) == "Not downloaded"
    assert _text(model, 1, Col.STATUS) == "Ready to download"
    done = _item(video_id="c", status=Status.DOWNLOADED, filename=model.planned_filename(_item(video_id="c")))
    model.set_items([done])
    assert _text(model, 0, Col.STATUS).startswith("✓ ")          # "✓ Downloaded" (or "✓ Name will change")


# ------------------------------------------------------------------ the page
@pytest.fixture
def page(qtbot, tmp_path):
    from reelay.gui.app import create_window
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    QSettings().remove("library/columns")
    store = Store(db_path())
    profile = store.create_profile("Be Unconventional", tmp_path / "lib")
    store.set_active_profile(profile.id)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    window.show()
    window.go("download")
    window.download.view_toggle.set_mode("list", emit=True)
    yield window.download, store
    window.close()
    store.close()
    QSettings().remove("library/columns")


def _visible_in_order(table):
    hh = table.horizontalHeader()
    return [Col(hh.logicalIndex(v)) for v in range(hh.count()) if not hh.isSectionHidden(hh.logicalIndex(v))]


def test_the_default_list_is_clean_and_every_divider_moves(page):
    page, _store = page
    table = page.table
    assert _visible_in_order(table) == list(DEFAULT_COLUMNS) == [Col.CHECK, Col.COVER, Col.DATE, Col.FILENAME,
                                                                  Col.PLATFORM, Col.STATUS]
    hh = table.horizontalHeader()
    assert hh.sectionsMovable() and hh.stretchLastSection()
    assert hh.sectionResizeMode(Col.CHECK) == QHeaderView.Fixed and hh.sectionResizeMode(Col.COVER) == QHeaderView.Fixed
    for col in (Col.DATE, Col.HOOK, Col.LENGTH, Col.VIEWS, Col.CUSTOM, Col.FILENAME, Col.PLATFORM, Col.STATUS):
        assert hh.sectionResizeMode(col) == QHeaderView.Interactive, col


def test_columns_menu_shows_hides_and_resets(page, qtbot):
    page, _store = page
    menu = page.column_menu()
    labels = [a.text() for a in menu.actions() if a.isCheckable()]
    assert "Checkbox" not in labels and len(labels) == len(Col) - 1          # every column but the checkbox
    assert "Reset Columns to Default" in [a.text() for a in menu.actions()]
    hook = next(a for a in menu.actions() if a.text() == "Caption / Hook")
    hook.trigger()
    assert not page.table.isColumnHidden(Col.HOOK)
    assert page.columns_btn.isVisibleTo(page)                                # in List view
    page.view_toggle.set_mode("grid", emit=True)
    assert not page.columns_btn.isVisibleTo(page)                            # columns only matter in the list
    page.view_toggle.set_mode("list", emit=True)
    next(a for a in page.column_menu().actions() if a.text() == "Reset Columns to Default").trigger()
    assert page.table.isColumnHidden(Col.HOOK) and _visible_in_order(page.table) == list(DEFAULT_COLUMNS)


def test_column_choices_order_and_widths_are_remembered(page, qtbot, tmp_path):
    from reelay.gui.app import create_window
    from reelay.tokens import MemoryTokenStore
    page, store = page
    hh = page.table.horizontalHeader()
    page.set_column_visible(Col.VIEWS, True)
    hh.moveSection(hh.visualIndex(Col.STATUS), hh.visualIndex(Col.DATE))      # Status before Date
    page.table.setColumnWidth(Col.FILENAME, 333)
    page.save_columns()
    again = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(again)
    t = again.download.table
    order = _visible_in_order(t)
    assert Col.VIEWS in order and order.index(Col.STATUS) < order.index(Col.DATE)
    assert t.columnWidth(Col.FILENAME) == 333
    again.close()


def test_tests_never_touch_the_real_preferences(tmp_path):
    from PySide6.QtCore import QSettings
    settings = QSettings()
    assert settings.format() == QSettings.IniFormat
    assert str(tmp_path) in settings.fileName()
