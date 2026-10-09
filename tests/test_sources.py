"""Download sources: saved profile links, connected YouTube channels as sources,
display de-duplication, YouTube Data API listing, and the Download page."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from reelay.accounts import AccountService
from reelay.models import Account, VideoItem
from reelay.sources import (SourceError, SourceService, default_name, downloadable_by_default, iso_duration_seconds,
                            normalize_url, youtube_items)
from reelay.store import Store
from reelay.tokens import MemoryTokenStore
from reelay.youtube import OAuthClient, Tokens, YouTubeClient
from tests.conftest import FakeResponse, FakeSession

TIKTOK = "https://www.tiktok.com/@livingood.lifestyle"


@pytest.fixture
def svc(tmp_path):
    store = Store(tmp_path / "s.db")
    profile = store.create_profile("Lydia Livingood", tmp_path / "lib")
    service = SourceService(store, AccountService(store, MemoryTokenStore()))
    yield service, store, profile
    store.close()


# ------------------------------------------------------------------ saved sources
def test_save_rename_mine_remove(svc):
    service, store, profile = svc
    saved = service.add(profile.id, TIKTOK)
    assert (saved.name, saved.platform, saved.mine) == ("@livingood.lifestyle on TikTok", "tiktok", False)
    named = service.add(profile.id, "instagram.com/livingood.lifestyle/", name="Lydia's Instagram", mine=True)
    assert named.url == "https://instagram.com/livingood.lifestyle/" and named.platform == "instagram"
    service.rename(saved.id, "Lydia's TikTok")
    service.set_mine(saved.id, True)
    stored = {s.id: s for s in store.list_sources(profile.id)}
    assert stored[saved.id].name == "Lydia's TikTok" and stored[saved.id].mine
    service.remove(named.id)
    assert [s.id for s in store.list_sources(profile.id)] == [saved.id]


def test_validation(svc):
    service, _store, profile = svc
    for bad in ("", "https://vimeo.com/someone", "not a link"):
        with pytest.raises(SourceError):
            service.add(profile.id, bad)
    saved = service.add(profile.id, TIKTOK)
    with pytest.raises(SourceError):
        service.rename(saved.id, "   ")
    with pytest.raises(SourceError):
        service.rename(saved.id, "x" * 81)
    with pytest.raises(SourceError):
        service.remove(9999)


def test_sources_belong_to_their_profile_and_cascade(svc, tmp_path):
    service, store, profile = svc
    other = store.create_profile("Andrew", tmp_path / "other")
    service.add(profile.id, TIKTOK)
    service.add(other.id, "https://www.youtube.com/@andrew")
    assert [s.platform for s in store.list_sources(other.id)] == ["youtube"]
    store.delete_profile(other.id)
    assert store.list_sources(other.id) == [] and len(store.list_sources(profile.id)) == 1


def test_duplicates_are_merged_for_display_not_forbidden(svc):
    service, store, profile = svc
    service.add(profile.id, TIKTOK, name="Lydia's TikTok")
    service.add(profile.id, "tiktok.com/@LIVINGOOD.lifestyle/?lang=en", name="Copy", mine=True)
    service.add(profile.id, "https://www.youtube.com/@livingood")
    account = store.save_account(Account(profile_id=profile.id, platform="youtube", external_id="UC123",
                                         display_name="Lydia Livingood", handle="@livingood"))
    assert len(service.all_sources(profile.id)) == 4  # the API lists everything as stored
    shown = service.display_sources(profile.id)
    assert [(v.kind, v.name) for v in shown] == [("account", "Lydia Livingood"), ("saved", "Lydia's TikTok")]
    tiktok = shown[1]
    assert tiktok.duplicates == 1 and tiktok.mine  # merged; "mine" from the duplicate carries over
    assert shown[0].account.id == account.id and shown[0].duplicates == 1  # the saved @livingood link


def test_url_helpers():
    assert normalize_url("https://WWW.TikTok.com/@Lydia/") == normalize_url("tiktok.com/@lydia?x=1") \
        == "https://tiktok.com/@lydia"
    assert normalize_url("https://www.youtube.com/channel/UCAbC/") == "https://youtube.com/channel/UCAbC"
    assert default_name("https://www.youtube.com/@livingood") == "@livingood on YouTube"
    assert default_name("https://www.instagram.com/livingood.lifestyle/") == "@livingood.lifestyle on Instagram"
    assert iso_duration_seconds("PT1H2M3S") == 3723.0 and iso_duration_seconds("P0D") is None


# ------------------------------------------------------------------ YouTube Data API listing
def _video(vid, privacy="public", publish_at=None, duration="PT47S"):
    status = {"privacyStatus": privacy}
    if publish_at:
        status["publishAt"] = publish_at
    return {"id": vid, "snippet": {"title": f"Video {vid}", "description": "caption #growth",
                                   "publishedAt": "2026-10-02T15:00:00Z", "channelTitle": "Lydia Livingood",
                                   "tags": ["growth"], "thumbnails": {"high": {"url": f"https://i.ytimg.com/{vid}.jpg"}}},
            "contentDetails": {"duration": duration}, "statistics": {"viewCount": "477", "likeCount": "42"},
            "status": status}


def test_connected_channel_lists_every_video_via_the_data_api(fake_session):
    channel = {"items": [{"id": "UC1", "snippet": {"title": "Lydia"},
                          "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]}
    fake_session.on("GET", "/channels", FakeResponse(200, channel))
    fake_session.on("GET", "/playlistItems", FakeResponse(200, {
        "items": [{"contentDetails": {"videoId": f"v{n}"}} for n in range(50)], "nextPageToken": "P2"}))
    fake_session.on("GET", "/playlistItems", FakeResponse(200, {
        "items": [{"contentDetails": {"videoId": f"v{n}"}} for n in range(50, 53)]}))
    fake_session.on("GET", "/videos", FakeResponse(200, {"items": [_video(f"v{n}") for n in range(50)]}))
    fake_session.on("GET", "/videos", FakeResponse(200, {"items": [
        _video("v50", "private"), _video("v51", "private", publish_at="2026-11-01T16:00:00Z"),
        _video("v52", "unlisted", duration="PT1M3S")]}))
    client = YouTubeClient(OAuthClient("id", "s"), Tokens("a", "r", time.time() + 3600), session=fake_session)
    videos = client.list_channel_videos()
    assert [v["id"] for v in videos] == [f"v{n}" for n in range(53)]
    page2 = [c for c in fake_session.calls if "/playlistItems" in c["url"]][1]
    assert page2["params"]["pageToken"] == "P2"
    items = youtube_items(videos)
    first = items[0]
    assert (first.platform, first.url, first.upload_date, first.duration, first.view_count, first.like_count) == \
        ("YouTube", "https://www.youtube.com/watch?v=v0", "20261002", 47.0, 477, 42)
    assert first.thumbnail_url.endswith("v0.jpg") and first.uploader == "Lydia Livingood" and first.has_details
    assert [i.availability for i in items[50:]] == ["private", "scheduled", "unlisted"]
    assert [downloadable_by_default(i) for i in items[50:]] == [False, False, True]
    assert items[52].duration == 63.0


# ------------------------------------------------------------------ Download page
@pytest.fixture
def page_env(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox

    from reelay.gui.app import create_window
    from reelay.paths import db_path
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    store = Store(db_path())
    tokens = MemoryTokenStore()
    profile = store.create_profile("Lydia Livingood", tmp_path / "lib")
    store.set_active_profile(profile.id)
    window = create_window(QApplication.instance(), store, tokens)
    qtbot.addWidget(window)
    yield window, store, tokens, profile
    window.close()
    store.close()


def chips(page):
    return page._chips()


def test_one_click_refetch_uses_the_saved_url(page_env, qtbot, monkeypatch):
    window, store, _tokens, profile = page_env
    page = window.download
    SourceService(store, window.accounts).add(profile.id, TIKTOK, name="Lydia's TikTok", mine=True)
    qtbot.waitUntil(lambda: len(chips(page)) == 1, timeout=2000)  # appears via the change feed
    import reelay.gui.workers as workers
    requested = []

    def fake_list(url, opts, limit=None):
        requested.append(url)
        return "x", [VideoItem(platform="TikTok", video_id="1", url=f"{url}/video/1", upload_date="20261002",
                               title="t...", description="caption")]
    monkeypatch.setattr(workers, "list_videos", fake_list)
    chips(page)[0].fetch_btn.click()
    qtbot.waitUntil(lambda: page.model.rowCount() == 1, timeout=5000)
    assert requested == [TIKTOK] and page.url.text() == TIKTOK
    assert store.list_sources(profile.id)[0].last_fetched_at is not None
    assert store.get_profile(profile.id).last_source == TIKTOK


def test_connected_channel_is_a_source_without_a_link(page_env, qtbot, monkeypatch):
    window, store, tokens, profile = page_env
    page = window.download
    account = store.save_account(Account(profile_id=profile.id, platform="youtube", external_id="UC1",
                                         display_name="Lydia Livingood", handle="@livingood"))
    qtbot.waitUntil(lambda: len(chips(page)) == 1, timeout=2000)
    assert chips(page)[0].view.account.id == account.id

    class FakeChannel:
        def list_channel_videos(self):
            return [_video("pub"), _video("priv", "private")]
    monkeypatch.setattr(window.accounts, "client_for", lambda acc: FakeChannel())
    page.url.clear()
    chips(page)[0].fetch_btn.click()
    qtbot.waitUntil(lambda: page.model.rowCount() == 2, timeout=5000)
    rows = {i.video_id: i for i in page.model.items()}
    assert not rows["pub"].checked and not rows["priv"].checked      # nothing is preselected
    assert page.model._status_text(rows["priv"]) == "Private on YouTube"
    assert page.url.text() == ""                                      # no link was needed
    assert "Use browser sign-in" in page.status.text() and "Nothing is selected yet" in page.status.text()


def test_save_pasted_link_and_remove_with_duplicates(page_env, qtbot, monkeypatch):
    window, store, _tokens, profile = page_env
    page = window.download
    from reelay.gui import download_page
    monkeypatch.setattr(download_page.SourceDialog, "exec", lambda self: download_page.SourceDialog.Accepted)
    monkeypatch.setattr(download_page.SourceDialog, "name", lambda self: "Lydia's TikTok")
    monkeypatch.setattr(download_page.SourceDialog, "mine", lambda self: True)
    page.url.setText(TIKTOK)
    page.save_source_btn.click()
    SourceService(store, window.accounts).add(profile.id, TIKTOK + "/")  # same URL again
    qtbot.waitUntil(lambda: len(store.list_sources(profile.id)) == 2, timeout=2000)
    qtbot.waitUntil(lambda: len(chips(page)) == 1 and chips(page)[0].view.duplicates == 1, timeout=2000)
    assert chips(page)[0].view.name == "Lydia's TikTok" and chips(page)[0].view.mine
    page._remove_source(chips(page)[0].view)
    assert store.list_sources(profile.id) == []
    qtbot.waitUntil(lambda: len(chips(page)) == 0, timeout=2000)


def test_doors_are_explained(page_env):
    window, *_ = page_env
    page = window.download
    assert "pulls videos in" in page.doors_note.text() and "pushes videos out" in page.doors_note.text()
    assert "account you manage" in page.paste_label.text() and "permission to manage" in page.doors_note.text()
