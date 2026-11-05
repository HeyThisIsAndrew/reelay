"""Date ordering and metadata handover when source listings are repeated."""

from dataclasses import replace
import threading

import pytest

from reelay.models import VideoItem
from tests.test_sources import page_env  # noqa: F401  (pytest fixture)


def item(vid, date="", platform="YouTube"):
    url = (f"https://www.tiktok.com/@sample/video/{vid}" if platform == "TikTok"
           else f"https://www.youtube.com/watch?v={vid}")
    return VideoItem(platform=platform, video_id=vid, url=url,
                     title=f"Video {vid}", upload_date=date, products=[])


def dates(page):
    return [page.model.item(page.proxy.mapToSource(page.proxy.index(r, 0)).row()).upload_date
            for r in range(page.proxy.rowCount())]


@pytest.mark.parametrize("sort,expected", [
    ("newest", ["20261104", "20261002", "20260901", ""]),
    ("oldest", ["20260901", "20261002", "20261104", ""]),
])
def test_late_dates_reorder_both_views_and_keep_unknown_dates_last(page_env, qtbot, sort, expected):
    window, store, _, _ = page_env
    page = window.download
    page.model.set_items([item("a"), item("b"), item("c", platform="TikTok"), item("unknown")])
    page.sort_box.setCurrentIndex(page.sort_box.findData(sort))
    page._apply_sort()
    for vid, date in [("a", "20261002"), ("b", "20261104"), ("c", "20260901")]:
        current = next(video for video in page.model.items() if video.video_id == vid)
        page._on_details(current.key, replace(current, upload_date=date))
    qtbot.waitUntil(lambda: dates(page) == expected, timeout=2000)
    page.pager.set_size(2)
    for view in (page.table, page.grid):
        assert view.model() is page.pager
        assert [page.model.item(page.pager.to_model_row(page.pager.index(r, 0))).upload_date
                for r in range(page.pager.rowCount())] == expected[:2]


def test_new_source_restarts_details_after_cancelled_worker_finishes(page_env, qtbot, monkeypatch):
    import reelay.gui.workers as workers
    window, store, _, _ = page_env
    page = window.download
    started, release = threading.Event(), threading.Event()
    calls = []

    def details(video, opts):
        calls.append(video.video_id)
        if len(calls) == 1:
            started.set()
            release.wait(5)
        return replace(video, upload_date="20261104")

    monkeypatch.setattr(workers, "load_details", details)
    try:
        page._on_fetched(["https://www.youtube.com/@first"], [item("first")], [])
        qtbot.waitUntil(started.is_set, timeout=2000)
        page._begin_fetch("Second source")
        page._on_fetched(["https://www.youtube.com/@second"], [item("second")], [])
        page._begin_fetch("Third source")
        page._on_fetched(["https://www.youtube.com/@third"], [item("third")], [])
        release.set()
        qtbot.waitUntil(lambda: all(page.model.find(item(vid).key).upload_date
                                   for vid in ("first", "second", "third")), timeout=2000)
        assert page.model.find(item("first").key) is not None
        assert "second" in calls
    finally:
        release.set()
        if page.details_worker:
            page.details_worker.wait(2000)
        qtbot.wait(30)


def test_new_source_restarts_product_lookup_after_previous_lookup_stops(page_env, qtbot, monkeypatch):
    window, store, _, _ = page_env
    page = window.download
    started, release = threading.Event(), threading.Event()
    calls = []

    def lookup(url, opts):
        calls.append(url)
        if len(calls) == 1:
            started.set()
            release.wait(5)
        return []

    monkeypatch.setattr("reelay.products.tiktok_products", lookup)
    first, second = item("first", "20261104", "TikTok"), item("second", "20261002", "TikTok")
    first.products = second.products = None
    try:
        page._on_fetched(["https://www.tiktok.com/@first"], [first], [])
        qtbot.waitUntil(started.is_set, timeout=2000)
        page._begin_fetch("Second source")
        page._on_fetched(["https://www.tiktok.com/@second"], [second], [])
        release.set()
        qtbot.waitUntil(lambda: page.model.find(second.key).products == [], timeout=2000)
        assert second.url in calls
    finally:
        release.set()
        if page.products_worker:
            page.products_worker.wait(2000)
        qtbot.wait(30)


def test_late_metadata_from_previous_profile_is_ignored(page_env, monkeypatch):
    from reelay.gui.workers import DetailsWorker
    window, store, _, profile = page_env
    page = window.download
    old = DetailsWorker([], {}, parent=page)
    old._listing_context = (0, profile.id)
    old.loaded.connect(page._on_details)
    page._stop_details()
    other = store.create_profile("Other", profile.output_folder + "-other")
    page.set_profile(other)
    current = item("shared", "20261002")
    current.title = "Other profile's title"
    page.model.set_items([current])
    old.loaded.emit(current.key, item("shared", "20261104"))
    assert page.model.find(current.key).title == "Other profile's title"
    assert store.load_videos(other.id) == []
