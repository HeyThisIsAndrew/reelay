"""Even card grids: every row full, every card the same size (reelay/gui/grid.py, the Accounts page)."""

from __future__ import annotations

import pytest

from reelay.gui.grid import balanced_columns, max_columns_for


@pytest.mark.parametrize("count,max_columns,expected", [
    (6, 3, 3), (6, 2, 2), (9, 3, 3), (9, 2, 1), (4, 3, 2), (7, 3, 1), (4, 2, 2), (3, 3, 3), (6, 1, 1), (1, 3, 1),
])
def test_balanced_columns(count, max_columns, expected):
    assert balanced_columns(count, max_columns) == expected
    assert count % balanced_columns(count, max_columns) == 0          # never an orphan


def test_max_columns_by_width():
    assert [max_columns_for(w) for w in (1300, 1180, 1179, 900, 720, 719, 400)] == [3, 3, 2, 2, 2, 1, 1]


@pytest.fixture
def accounts_page(qapp, tmp_path):
    from reelay.accounts import AccountService
    from reelay.gui.accounts_page import AccountsPage
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    store = Store(tmp_path / "s.db")
    profile = store.create_profile("Be Unconventional", tmp_path / "videos")
    tokens = MemoryTokenStore()
    page = AccountsPage(store, AccountService(store, tokens))
    page.set_profile(profile)
    yield page, store, profile, tokens
    page.close()
    store.close()


def _rows(page):
    rows: dict[int, list] = {}
    for card in page._cards():
        row, _col, _rs, _cs = page.grid.getItemPosition(page.grid.indexOf(card))
        rows.setdefault(row, []).append(card)
    return list(rows.values())


@pytest.mark.parametrize("width,columns", [(1300, 3), (900, 2), (600, 1)])
def test_accounts_grid_is_even_at_every_width(accounts_page, qapp, width, columns):
    page, *_ = accounts_page
    page.resize(width, 900)
    page.show()
    qapp.processEvents()
    page._place_cards()
    rows = _rows(page)
    assert page._columns == columns
    assert {len(r) for r in rows} == {columns} and sum(len(r) for r in rows) == 6     # every row full
    assert len({card.height() for card in page._cards()}) == 1                       # every card the same height
    assert page.snapchat_strip not in page._cards() and page.grid.indexOf(page.snapchat_strip) == -1
    assert page.snapchat_strip.isVisibleTo(page) and page.snapchat_strip.pill.text() == "Posted by hand"
    assert page.count.text().endswith("of 6 connected")


def test_snapchat_can_never_offer_a_sign_in():
    from reelay.oauth import provider_for
    assert provider_for("snapchat") is None


def test_content_never_grows_a_card(accounts_page, qapp):
    """A long name, several accounts, a problem, or opening Advanced Setup: the cards stay equal."""
    from reelay.models import Account
    page, store, profile, tokens = accounts_page
    page.resize(1300, 900)
    page.show()
    qapp.processEvents()
    page._place_cards()
    before = {card.height() for card in page._cards()}
    for n in range(3):
        store.save_account(Account(profile_id=profile.id, platform="threads", external_id=f"t{n}",
                                   display_name="A very long Threads account name that goes on and on " * 2,
                                   handle=f"@acct{n}"))
        tokens.set(f"threads:t{n}", '{"access_token": "x", "user_id": "t%d"}' % n)
    store.save_account(Account(profile_id=profile.id, platform="facebook", external_id="p1",
                               display_name="Page", handle=""))                     # no credentials: a problem
    page.refresh()
    qapp.processEvents()
    threads = page.platform_cards["threads"]
    assert threads.line.full_text() == "3 connected" and threads.manage_btn.isVisibleTo(page)
    assert threads.pill.text() == "3 connected"
    assert page.platform_cards["facebook"].pill.text() == "Needs attention"
    assert {card.height() for card in page._cards()} == before                        # nothing grew
    assert not page.youtube_advanced.isVisible() and page.youtube_advanced.parent() is page   # a window, not inline
    assert page.count.text() == "2 of 6 connected"


def test_each_card_shows_one_action_at_a_time(accounts_page, qapp):
    page, *_ = accounts_page
    page.show()
    qapp.processEvents()
    youtube = [b for b in (page.setup_btn, page.connect_btn, page.disconnect_btn, page.manage_btn)
               if b.isVisibleTo(page)]
    assert len(youtube) == 1
    for key in ("tiktok", "instagram", "bluesky", "threads", "facebook"):
        card = page.platform_cards[key]
        actions = [card.footer.itemAt(i).widget() for i in range(card.footer.count())
                   if card.footer.itemAt(i).widget() is not None]
        primary = [b for b in actions if b.isVisibleTo(card) and b.objectName() != "QuietButton"]
        quiet = [b for b in actions if b.isVisibleTo(card) and b.objectName() == "QuietButton"]
        assert len(primary) <= 1 and len(quiet) <= 1, key


def test_download_sources_stay_two_by_two_or_one_column(accounts_page, qapp):
    page, *_ = accounts_page
    panel = page.sources_panel
    for asked, expected in ((3, 2), (2, 2), (1, 1)):
        panel._columns = 0
        panel.set_columns(asked)
        rows = {}
        for section in panel.sections.values():
            rows.setdefault(panel.grid.getItemPosition(panel.grid.indexOf(section))[0], []).append(section)
        assert panel._columns == expected and {len(r) for r in rows.values()} == {expected}
