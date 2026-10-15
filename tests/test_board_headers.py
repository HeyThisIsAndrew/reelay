"""Board column headers: the column's name leads, a line under it says what it holds, and a column
narrowed by a filter or search says so ("1 of 4", outlined), with the page saying how many tickets
show and a way back to all of them."""

from __future__ import annotations

import pytest

from reelay.columns import Column
from reelay.gui.board_page import column_purpose
from reelay.models import Stage
from tests.test_workflow import board  # noqa: F401 — the shared Board fixture

pytestmark = pytest.mark.gui


def test_every_column_says_what_it_holds(board):  # noqa: F811
    _window, _store, b, _rows = board
    assert [b.columns[s].purpose.text() for s in Stage.ALL] == [
        "Downloaded, not started", "Writing the posts", "Waiting to go live", "Live"]
    merged = Column(id="x", name="Done", primary=Stage.PUBLISHED, stages=[Stage.SCHEDULED, Stage.PUBLISHED])
    assert column_purpose(merged) == "Waiting to go live · Live"
    assert column_purpose(Column(id="c", name="Edit", primary="custom:edit", stages=["custom:edit"])) == "Your own step"


def test_a_filtered_board_says_so_and_clears(board):  # noqa: F811
    _window, _store, b, rows = board
    col = b.columns[Stage.BACKLOG]
    assert col.count.text() == "4" and not col.count.property("filtered")
    assert b.subtitle.text() == "Drag cards to move them along" and not b.clear_filters_btn.isVisibleTo(b)
    b.filter_box.setCurrentIndex(b.filter_box.findData("not_ready"))
    assert col.count.text() == "1 of 4" and col.count.property("filtered") is True
    assert b.subtitle.text() == "Showing 1 of 4 tickets" and b.clear_filters_btn.isVisibleTo(b)
    b.clear_filters_btn.click()
    assert b.filter_box.currentData() == "all" and col.count.text() == "4"
    assert not col.count.property("filtered") and not b.clear_filters_btn.isVisibleTo(b)
