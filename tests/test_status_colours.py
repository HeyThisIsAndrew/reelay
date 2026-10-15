"""Status colours on the Board mean one thing each (theme.py): green live, blue scheduled, red blocked
or failed, amber waiting for your decision, and no colour for ready, the normal state. And they read:
glyphs on the small platform badges were white at 2.0:1 on amber and 2.2:1 on green."""

from __future__ import annotations

import pytest

from reelay.gui import theme
from tests.fakes import disable_publishing, make_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    disable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch)
    yield w
    w.store.close()


def _palettes():
    yield from theme.THEMES.values()
    for bg, accent in (("#1f1e1d", "#6f9ef0"), ("#f7f3ea", "#c2410c"), ("#0b1020", "#22c55e")):
        yield theme.derive_custom(bg, accent)


def test_badge_glyphs_read_on_every_status_colour():
    for p in _palettes():
        for color in (p.success, p.info, p.danger, p.warning, p.text_secondary):
            assert theme.contrast(p.ink_on(color), color) >= 3.0, (p.name, color)


def test_status_text_reads_on_cards():
    for p in _palettes():
        for color in (p.warning_text, p.info_text):
            for bg in (p.card_fill, p.bg_surface):
                assert theme.contrast(color, bg) >= 4.5, (p.name, color, bg)


def test_ready_is_neutral_never_amber(world):
    from reelay.gui.board_page import CardDelegate
    p = theme.P
    text, color = CardDelegate._status(world.pipeline.card(world.rows[0]))
    assert text.startswith("✓ Ready") and color in (p.text_secondary, p.text_muted)


def test_the_coverage_badges_keep_amber_for_decisions():
    import inspect

    from reelay.gui.board_page import CardDelegate
    source = inspect.getsource(CardDelegate._draw_platforms)
    assert "READY: p.text_secondary" in source and "p.warning" not in source


def test_a_single_post_on_its_way_says_when(world):
    """One platform scheduled (here Bluesky) used to fall through to "✓ Ready · not started"."""
    from datetime import datetime, timedelta, timezone

    from reelay.gui.board_page import CardDelegate
    from reelay.models import Dispatch, Stage
    from reelay.pipeline import utc_iso
    row = world.rows[0]
    when = utc_iso(datetime.now(timezone.utc) + timedelta(days=3))
    post = world.pipeline.ensure_post(row)
    world.store.schedule_post(post, [Dispatch(video_row=row, platform="bluesky", due_at=when, publish_at=when)],
                              Stage.SCHEDULED)
    text, color = CardDelegate._status(world.pipeline.card(row))
    assert text.startswith("Goes live") and color == theme.P.info_text


def test_scheduled_with_nothing_scheduled_says_so(world):
    from reelay.gui.board_page import CardDelegate
    from reelay.models import Stage
    world.store.set_stage([world.rows[1]], Stage.SCHEDULED)          # dragged there, nothing scheduled
    text, color = CardDelegate._status(world.pipeline.card(world.rows[1]))
    assert text == "Nothing scheduled yet: schedule it, or move it back" and color == theme.P.warning_text
