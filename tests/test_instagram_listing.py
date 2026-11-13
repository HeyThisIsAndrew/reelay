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
