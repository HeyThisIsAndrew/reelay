"""People type handles the way each platform shows them: with an @, with spaces, or as a whole profile link.

Every field where a handle, Page ID or token is typed has to accept those forms."""

import pytest

from reelay.handles import clean_credentials, clean_handle, clean_text, handle_from_link


@pytest.mark.parametrize("typed", ["@thisisandrew.bsky.social", "thisisandrew.bsky.social",
                                   "  @thisisandrew.bsky.social  ", "＠thisisandrew.bsky.social",
                                   "@@thisisandrew.bsky.social", "@ThisIsAndrew.bsky.social",
                                   "​@thisisandrew.bsky.social​",
                                   "https://bsky.app/profile/thisisandrew.bsky.social",
                                   "bsky.app/profile/@thisisandrew.bsky.social/"])
def test_bluesky_handle_every_way_people_type_it(typed):
    assert clean_handle(typed, "bluesky") == "thisisandrew.bsky.social"


def test_bluesky_short_name_gets_the_default_domain():
    assert clean_handle("@thisisandrew", "bluesky") == "thisisandrew.bsky.social"


def test_bluesky_custom_domain_and_email_are_left_alone():
    assert clean_handle("@andrew.example.com", "bluesky") == "andrew.example.com"
    assert clean_handle("me@example.com", "bluesky") == "me@example.com"     # Bluesky signs in by email too


@pytest.mark.parametrize("platform,typed,expected", [
    ("tiktok", "@livingood.lifestyle", "livingood.lifestyle"),
    ("tiktok", "https://www.tiktok.com/@livingood.lifestyle?lang=en", "livingood.lifestyle"),
    ("instagram", "@hey_thisisandrew", "hey_thisisandrew"),
    ("instagram", "https://www.instagram.com/hey_thisisandrew/", "hey_thisisandrew"),
    ("threads", "https://www.threads.net/@hey_thisisandrew", "hey_thisisandrew"),
    ("threads", "https://www.threads.com/@hey_thisisandrew", "hey_thisisandrew"),
    ("youtube", "@HeyThisIsAndrew", "HeyThisIsAndrew"),
    ("youtube", "youtube.com/@HeyThisIsAndrew/videos", "HeyThisIsAndrew"),
])
def test_other_platforms_accept_the_at_and_the_profile_link(platform, typed, expected):
    assert clean_handle(typed, platform) == expected


def test_handle_from_link_ignores_other_sites_and_post_links():
    assert handle_from_link("https://example.com/@someone", "tiktok") == ""
    assert handle_from_link("https://www.instagram.com/reel/ABC123/", "instagram") == ""


def test_clean_text_strips_quotes_and_invisible_characters():
    assert clean_text('  "abc"​ ') == "abc"
    assert clean_text("﻿abc\n") == "abc"


def test_bluesky_credentials_accept_the_at_and_a_spaced_app_password():
    secret = clean_credentials("bluesky", {"handle": "@thisisandrew.bsky.social",
                                           "app_password": " abcd efgh ijkl mnop "})
    assert secret == {"handle": "thisisandrew.bsky.social", "app_password": "abcd-efgh-ijkl-mnop"}


def test_bluesky_app_password_with_dashes_is_kept():
    assert clean_credentials("bluesky", {"handle": "a.bsky.social",
                                         "app_password": "abcd-efgh-ijkl-mnop"})["app_password"] == "abcd-efgh-ijkl-mnop"


@pytest.mark.parametrize("typed", ["Bearer EAAB123", "access_token=EAAB123", '"EAAB123"', " EAAB123\n"])
def test_tokens_accept_what_gets_copied_with_them(typed):
    assert clean_credentials("threads", {"access_token": typed}) == {"access_token": "EAAB123"}


@pytest.mark.parametrize("typed", ["@123456789", "123456789", "https://www.facebook.com/profile.php?id=123456789",
                                   "https://www.facebook.com/123456789/"])
def test_facebook_page_id_accepts_the_at_and_the_page_link(typed):
    assert clean_credentials("facebook", {"page_id": typed, "page_access_token": "t"})["page_id"] == "123456789"


def test_facebook_page_name_link_becomes_the_name():
    # A vanity link isn't an ID, but Facebook's API looks Pages up by their username too.
    assert clean_credentials("facebook", {"page_id": "https://www.facebook.com/HeyThisIsAndrew",
                                          "page_access_token": "t"})["page_id"] == "HeyThisIsAndrew"


def test_unknown_fields_are_only_trimmed():
    assert clean_credentials("bluesky", {"service": " https://bsky.social "})["service"] == "https://bsky.social"


def test_bluesky_login_strips_the_at_even_for_details_saved_elsewhere():
    from reelay.publishers.bluesky import BlueskyPublisher

    sent = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"accessJwt": "j", "did": "did:plc:x", "handle": "thisisandrew.bsky.social"}

    class Session:
        def request(self, method, url, json=None, **kw):
            sent.update(json or {})
            return Resp()

        post = None

    pub = BlueskyPublisher(session=Session())
    pub.login("@thisisandrew.bsky.social", "abcd efgh ijkl mnop")
    assert sent["identifier"] == "thisisandrew.bsky.social"
    assert sent["password"] == "abcd-efgh-ijkl-mnop"


def test_typed_username_finds_the_saved_sources_with_it():
    from reelay.sources import SourceView, is_bare_handle, sources_for_handle

    views = [SourceView("saved", "TikTok", "tiktok", True, url="https://www.tiktok.com/@hey_thisisandrew"),
             SourceView("saved", "Instagram", "instagram", True, url="https://www.instagram.com/hey_thisisandrew/"),
             SourceView("saved", "YouTube", "youtube", True, url="https://www.youtube.com/@HeyThisIsAndrew")]
    assert is_bare_handle("@hey_thisisandrew") and is_bare_handle("hey_thisisandrew")
    assert not is_bare_handle("https://www.tiktok.com/@x") and not is_bare_handle("tiktok.com/@x")
    assert [v.platform for v in sources_for_handle(views, "@hey_thisisandrew")] == ["tiktok", "instagram"]
    assert [v.platform for v in sources_for_handle(views, "@heythisisandrew")] == ["youtube"]
    assert sources_for_handle(views, "@someone_else") == []


def test_source_usernames_accept_the_at_and_invisible_characters():
    from reelay.sources import resolve_source_input

    assert resolve_source_input("tiktok", " ​@livingood.lifestyle ") == "https://www.tiktok.com/@livingood.lifestyle"
    assert resolve_source_input("instagram", "＠hey_thisisandrew") == "https://www.instagram.com/hey_thisisandrew/"
