"""Instagram profile listing: yt-dlp marks its profile reader broken, so Reelay says so plainly before trying,
and never shows yt-dlp's "please report this issue" text (the owner hit it on 2026-11-12)."""

from __future__ import annotations

import pytest

from reelay import fetcher
from reelay.ytdl import friendly_error

RAW = ("ERROR: [instagram:user] hey_thisisandrew: Unable to extract data; please report this issue on "
       "https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the appropriate issue template. Confirm you are "
       "on the latest version using yt-dlp -U")


def test_a_profile_link_is_refused_with_what_to_do_instead(monkeypatch):
    monkeypatch.setattr(fetcher, "instagram_profile_listing_works", lambda: False)
    monkeypatch.setattr(fetcher.yt_dlp, "YoutubeDL", lambda *a, **k: pytest.fail("no network request"))
    with pytest.raises(fetcher.ListingUnavailable) as exc:
        fetcher.list_videos("https://www.instagram.com/hey_thisisandrew/", {})
    message = str(exc.value)
    assert "paste the links" in message and "Use browser sign-in" in message
    assert friendly_error(exc.value) == message                    # shown as written
    assert "—" not in message


def test_post_and_reel_links_are_still_tried(monkeypatch):
    monkeypatch.setattr(fetcher, "instagram_profile_listing_works", lambda: False)
    for url in ("https://www.instagram.com/reel/C1abc/", "https://www.instagram.com/p/C1abc/",
                "https://www.instagram.com/hey_thisisandrew/reel/C1abc/"):
        assert fetcher.listing_problem(url) is None
    assert fetcher.listing_problem("https://www.tiktok.com/@hey_thisisandrew") is None


def test_profiles_are_tried_again_once_yt_dlp_fixes_its_reader(monkeypatch):
    monkeypatch.setattr(fetcher, "instagram_profile_listing_works", lambda: True)
    assert fetcher.listing_problem("https://www.instagram.com/hey_thisisandrew/") is None


def test_yt_dlp_report_this_issue_text_never_reaches_the_creator():
    text = friendly_error(Exception(RAW), "https://www.instagram.com/hey_thisisandrew/")
    assert text.startswith("Instagram changed something") and "github" not in text and "yt-dlp -U" not in text


# ------------------------------------------------------------------ the official API: the creator's own posts
from reelay import instagram_media  # noqa: E402
from tests.conftest import FakeResponse, FakeSession  # noqa: E402

TOKEN = "IGQ-secret-token"


def _media(n, kind="VIDEO", product="REELS"):
    return {"id": str(n), "media_type": kind, "media_product_type": product, "username": "hey_thisisandrew",
            "permalink": f"https://www.instagram.com/reel/SC{n}/", "timestamp": f"2026-11-0{n}T15:00:00+0000",
            "caption": f"Reel {n} first line\nmore #sonyalpha", "thumbnail_url": f"https://cdn/x{n}.jpg"}


def test_every_page_of_the_creators_own_videos_is_listed():
    s = FakeSession()
    s.on("GET", "/me/media", FakeResponse(200, {"data": [_media(3), _media(2, "IMAGE", "FEED")],
                                                 "paging": {"next": "https://graph.instagram.com/v21.0/me/media?after=X"}}))
    s.on("GET", "after=X", FakeResponse(200, {"data": [_media(1)]}))
    name, items = instagram_media.list_media(TOKEN, session=s)
    assert name == "hey_thisisandrew"
    assert [(i.video_id, i.url, i.upload_date, i.title) for i in items] == [
        ("SC3", "https://www.instagram.com/reel/SC3/", "20261103", "Reel 3 first line"),
        ("SC1", "https://www.instagram.com/reel/SC1/", "20261101", "Reel 1 first line")]   # the photo is skipped
    assert all(i.key.startswith("Instagram_") for i in items)


def test_paging_never_leaves_metas_api_and_errors_never_show_the_token():
    s = FakeSession()
    s.on("GET", "/me/media", FakeResponse(200, {"data": [], "paging": {"next": "https://evil.example/steal"}}))
    with pytest.raises(instagram_media.InstagramApiError):
        instagram_media.list_media(TOKEN, session=s)
    expired = FakeSession()
    expired.on("GET", "/me/media", FakeResponse(400, {"error": {"code": 190, "message": f"bad {TOKEN}"}}))
    with pytest.raises(instagram_media.InstagramApiError) as exc:
        instagram_media.list_media(TOKEN, session=expired)
    assert TOKEN not in str(exc.value) and "expired" in str(exc.value)


def test_verify_names_the_account_and_refuses_personal_accounts():
    ok = FakeSession()
    ok.on("GET", "/me", FakeResponse(200, {"user_id": "1789", "username": "hey_thisisandrew",
                                            "account_type": "MEDIA_CREATOR"}))
    assert instagram_media.verify({"access_token": TOKEN}, session=ok) == {
        "external_id": "1789", "display_name": "hey_thisisandrew", "handle": "@hey_thisisandrew"}
    personal = FakeSession()
    personal.on("GET", "/me", FakeResponse(200, {"user_id": "1", "username": "x", "account_type": "PERSONAL"}))
    with pytest.raises(instagram_media.InstagramApiError) as exc:
        instagram_media.verify({"access_token": TOKEN}, session=personal)
    assert "Professional" in str(exc.value)


def test_the_library_lists_your_own_instagram_through_the_api(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from reelay.gui.app import create_window
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore

    store = Store(db_path())
    profile = store.create_profile("Hey This Is Andrew", tmp_path / "lib")
    store.set_active_profile(profile.id)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    window.accounts.connect_platform(profile.id, "instagram", {"external_id": "1789", "display_name":
                                     "hey_thisisandrew", "handle": "@hey_thisisandrew"}, {"access_token": TOKEN})
    page = window.download
    asked = []

    def fake_list(token, session=None, limit=None):
        asked.append(token)
        return "hey_thisisandrew", [instagram_media.to_item(_media(1))]
    monkeypatch.setattr(instagram_media, "list_media", fake_list)

    assert page._instagram_api_account("https://www.instagram.com/someone_else/") is None   # never for others
    assert page._instagram_api_account("https://www.instagram.com/reel/SC1/") is None        # a post: yt-dlp
    page._fetch_urls(["https://www.instagram.com/hey_thisisandrew/"])
    qtbot.waitUntil(lambda: any(i.video_id == "SC1" for i in page.model.items()), timeout=5000)
    assert asked == [TOKEN]
    row = store.get_video(store.video_row_id(profile.id, "Instagram", "SC1"))
    assert row.ticket.authorization == "Own content"
    window.close()
    store.close()
