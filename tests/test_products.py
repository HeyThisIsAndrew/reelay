"""Tagged products (TikTok Shop): reading them from the video page, filtering
and searching by them. The page fixture mirrors the real structure seen on
Lydia's videos (2026-10-09); no network."""

from __future__ import annotations

import json

import pytest

from reelay.models import VideoItem
from reelay.products import Product, products_from_item, products_from_page, video_products

CANDY = ("1729544740539830811", "Blenders Polarized Sunglasses",
         "Blenders Eyewear Candy Cove Sunglasses - Polarized Pink Mirror Lenses, UV400 Protection")
NORTH = ("1729558631047467547", "Blenders Premium Sunglasses",
         "Blenders North Park Rosemary Beach Sunglasses – Polarized, UV400")


def item_struct(*products, capcut=True):
    entries = [{"keyword": name, "id": pid, "type": 33,
                "extra": json.dumps({"product_id": int(pid), "title": title, "currency": "USD",
                                     "seo_url": f"https://shop.tiktok.com/us/pdp/blenders/{pid}?source=anchor"})}
               for pid, name, title in products]
    anchors = [{"type": 35, "schema": "https://www.capcut.com", "extra": json.dumps(entries)}] if capcut else []
    return {"id": "7618456548737699102", "isECVideo": 1 if products else None, "anchors": anchors}


def page(item):
    data = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": item}}}}
    return f'<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{json.dumps(data)}</script>'


def test_products_are_read_from_the_page():
    [p] = products_from_page(page(item_struct(CANDY)))
    assert (p.id, p.name, p.platform) == (CANDY[0], "Blenders Polarized Sunglasses", "tiktok")
    assert p.title.startswith("Blenders Eyewear Candy Cove") and p.url.endswith(CANDY[0])   # query dropped
    assert "?" not in p.url and p.label.endswith("…") and len(p.label) <= 60
    assert [x.id for x in products_from_item(item_struct(CANDY, NORTH, CANDY))] == [CANDY[0], NORTH[0]]


def test_videos_without_products_or_odd_pages_give_none():
    assert products_from_item({"anchors": [{"type": 28, "keyword": "cinematic 4K", "id": "2227034855"}]}) == []
    assert products_from_item({"anchors": [{"type": 35, "extra": "not json"}]}) == []
    assert products_from_page("<html>nothing here</html>") == []
    assert products_from_page(page("not a dict")) == []


def test_products_survive_saving_and_merging(tmp_path):
    from reelay.store import Store
    store = Store(tmp_path / "s.db")
    profile = store.create_profile("Lydia", tmp_path)
    item = VideoItem(platform="TikTok", video_id="1", url="u")
    assert item.products is None                                   # not looked up yet
    item.products = [Product(*CANDY).to_record()]
    row = store.save_video(profile.id, item)
    loaded = store.get_video(row).item
    assert [p.id for p in video_products(loaded)] == [CANDY[0]]
    fresh = VideoItem(platform="TikTok", video_id="1", url="u", description="new caption")
    loaded.merge_metadata(fresh)                                   # a re-fetch doesn't forget products
    assert video_products(loaded)[0].id == CANDY[0]
    store.close()


# ------------------------------------------------------------------ desktop
@pytest.fixture
def ui(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox

    from reelay.gui.app import create_window
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    store = Store(db_path())
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    window.show()
    yield window, store
    window.close()
    store.close()


def test_download_page_looks_up_and_filters_by_product(ui, qtbot, monkeypatch):
    import reelay.gui.workers as workers
    window, store = ui
    captions = {"1": "if you like quality sunglasses check out Blenders", "2": "sunnies for your adventures",
                "3": "protect your eyes in style", "4": "manifesting my dream life"}
    tagged = {"1": [CANDY], "2": [CANDY], "3": [NORTH], "4": []}
    monkeypatch.setattr(workers, "list_videos", lambda url, opts, limit=None: ("x", [
        VideoItem(platform="TikTok", video_id=v, url=f"https://www.tiktok.com/@x/video/{v}", description=c,
                  upload_date="20261001") for v, c in captions.items()]))
    looked_up = []

    def fake_lookup(url, opts):
        vid = url.rsplit("/", 1)[1]
        looked_up.append(vid)
        return [Product(*p, url=f"https://shop.tiktok.com/us/pdp/x/{p[0]}") for p in tagged[vid]]
    monkeypatch.setattr("reelay.products.tiktok_products", fake_lookup)
    page = window.download
    page.url.setText("https://www.tiktok.com/@livingood.lifestyle")
    page._fetch()
    qtbot.waitUntil(lambda: sorted(looked_up) == ["1", "2", "3", "4"] and not page.products_worker.isRunning(),
                    timeout=5000)
    page._refresh_products()
    labels = [page.product_filter.itemText(i) for i in range(page.product_filter.count())]
    assert labels[0] == "All products" and labels[1].startswith("Blenders Eyewear Candy Cove") and \
        labels[1].endswith("(2)") and labels[2].endswith("(1)")
    assert "Found tagged products on 3 videos" in page.status.text()
    page.filter_by_product(CANDY[0])                                # like clicking it in the side panel
    shown = {page.model.item(page.proxy.mapToSource(page.proxy.index(r, 0)).row()).video_id
             for r in range(page.proxy.rowCount())}
    assert shown == {"1", "2"}
    page.product_filter.setCurrentIndex(0)
    page.search.setPlainText("blenders")                            # the brand: caption OR tagged product
    assert page.proxy.rowCount() == 3
    rows = {r.item.video_id: r.item for r in store.load_videos(window.profile.id)}
    assert rows["4"].products == [] and rows["1"].products[0]["id"] == CANDY[0]   # saved; 4 won't be re-asked


def test_board_product_filter_and_search(ui):
    from reelay.models import Stage
    from tests.test_gui import seed_board
    window, store = ui
    items, ids = seed_board(store, window.profile, None, n=3)
    for item, products in zip(items, ([CANDY], [NORTH], [])):
        item.products = [Product(*p).to_record() for p in products]
    store.save_videos(window.profile.id, items)
    window.go("board")
    b = window.board
    col = b.columns[Stage.BACKLOG]
    assert b.product_box.count() == 3
    b.product_box.setCurrentIndex(b.product_box.findData(NORTH[0]))
    assert [c.id for c in col.model.cards] == [ids[items[1].key]]
    b.product_box.setCurrentIndex(0)
    b.search.setText("candy cove")
    b.reload()
    assert [c.id for c in col.model.cards] == [ids[items[0].key]]


def test_api_lists_tagged_products(tmp_path, monkeypatch):
    import requests

    from reelay.api import ApiServer, create_app
    from tests.fakes import SECRETS, disable_publishing, make_world
    disable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch, ready=False)
    row = w.store.get_video(w.rows[0])
    row.item.products = [Product(*CANDY, url="https://shop.tiktok.com/us/pdp/x/1").to_record()]
    w.store.save_video(w.profile.id, row.item)
    server = ApiServer(create_app(w.store, w.pipeline, w.accounts, lambda: SECRETS["agent"]), host="127.0.0.1", port=0)
    assert server.start()
    try:
        board = requests.get(f"{server.url}/v1/board", params={"profile": "Lydia Livingood"},
                             headers={"Authorization": f"Bearer {SECRETS['agent']}"}, timeout=10).json()
        by_id = {v["id"]: v for v in board["videos"]}
        assert by_id[w.rows[0]]["source"]["products"][0]["id"] == CANDY[0]
        assert by_id[w.rows[1]]["source"]["products"] is None       # not looked up yet
    finally:
        server.stop()
        w.store.close()


# ------------------------------------------------------------------ brand → product
def test_brands_group_a_sellers_products():
    from reelay.products import brand_map
    ps = [Product("1", "a", "Blenders Eyewear Candy Cove Sunglasses", seller_id="s1"),
          Product("2", "b", "Blenders North Park Rosemary Beach", seller_id="s1"),
          Product("5", "c", "Ray-Ban Meta Wayfarer Gen 2", seller_id="s9"),
          Product("6", "d", "Ray-Ban Meta Skyler", seller_id="s9"),
          Product("7", "e", "Happy Saffron Plus | BrainMD")]
    assert brand_map(ps) == {"1": "Blenders", "2": "Blenders", "5": "Ray-Ban Meta", "6": "Ray-Ban Meta",
                             "7": "Happy"}


def test_brand_then_product_filter_and_renaming(ui, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    from reelay.gui.widgets import ProductFilter
    _window, store = ui
    f = ProductFilter(store)
    videos = [[Product(*CANDY, seller_id="s1")], [Product(*CANDY, seller_id="s1")],
              [Product(*NORTH, seller_id="s1")], [Product("7", "Saffron", "Happy Saffron Plus | BrainMD")]]
    seen = []
    f.changed.connect(seen.append)
    f.set_videos(videos)
    assert [f.brand.itemText(i) for i in range(f.brand.count())] == ["All brands", "Blenders  (3)", "Happy  (1)"]
    assert f.selection() is None and f.product.count() == 4
    f.brand.setCurrentIndex(1)                                      # Blenders: both its products
    assert f.selection() == {CANDY[0], NORTH[0]} and f.product.count() == 3
    assert f.product.itemText(0) == "All Blenders products"
    f.product.setCurrentIndex(f.product.findData(NORTH[0]))
    assert f.selection() == {NORTH[0]} and seen[-1] == {NORTH[0]}
    f.brand.setCurrentIndex(f.brand.findData("Happy"))
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Happy Saffron", True))
    f._rename()
    assert f.brand.currentData() == "Happy Saffron" and f.selection() == {"7"}
    again = ProductFilter(store)                                     # remembered everywhere
    again.set_videos(videos)
    assert again.brand.findData("Happy Saffron") > 0
