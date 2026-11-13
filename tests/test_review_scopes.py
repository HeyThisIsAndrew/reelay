"""The permissions Reelay asks for are exactly the ones its website justifies to reviewers.

Google, TikTok and Meta each reject an app whose consent screen asks for a permission the submission doesn't
explain, or one no shipped feature uses. The website's table (sites/reelay/src/data/permissions.json in the
website repository) is what reviewers read, so the two must never drift."""

import json
import os
from pathlib import Path

import pytest

from reelay.oauth import PROVIDERS, scopes_for
from reelay.youtube import SCOPES

# What each sign-in asks for, by platform. Changing this means changing the website's table and its demo videos.
REVIEW_SCOPES = {
    "google": {"https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"},
    "tiktok": {"user.info.basic"},
    "instagram": {"instagram_business_basic"},
    "threads": {"threads_basic", "threads_content_publish"},
    "facebook": {"pages_show_list", "pages_read_engagement", "pages_manage_posts"},
}

SITE_TABLE = Path(os.environ.get("REELAY_SITE_DIR", Path(__file__).resolve().parents[2] / "hey-this-is-andrew"
                                 / "sites" / "reelay")) / "src" / "data" / "permissions.json"


def requested(provider: str) -> set[str]:
    p = PROVIDERS[provider]
    return set(filter(None, scopes_for(p).split(p.scope_sep)))


@pytest.mark.parametrize("provider", sorted(REVIEW_SCOPES))
def test_sign_in_asks_only_for_the_reviewed_permissions(provider):
    assert requested(provider) == REVIEW_SCOPES[provider]


def test_your_own_google_project_asks_for_the_same_youtube_permissions():
    assert set(SCOPES) == REVIEW_SCOPES["google"]


@pytest.mark.skipif(not SITE_TABLE.exists(), reason="the website repository isn't next to this one")
def test_the_website_table_lists_exactly_what_the_app_asks_for():
    table = json.loads(SITE_TABLE.read_text())
    site = {p["key"]: {s["name"] for s in p["scopes"]} for p in table["platforms"]}
    assert site == REVIEW_SCOPES
    unused = [s["name"] for p in table["platforms"] for s in p["scopes"] if not s.get("used", True)]
    assert unused == [], "the website lists a permission no shipped feature uses"
