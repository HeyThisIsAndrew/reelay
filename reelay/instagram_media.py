"""List a creator's own Instagram videos through Meta's official Instagram API.

yt-dlp's Instagram profile reader relies on Instagram's private web endpoints and is marked broken. The
supported way to list "all of my posts" is the Instagram API with Instagram Login: ``GET /me/media`` on
graph.instagram.com, for a Professional (Creator or Business) account, with a token that has
``instagram_business_basic``. A creator gets that token from a Meta developer app they own (no App Review is
needed for their own account), pastes it under Accounts → Instagram → Advanced Setup, and their Instagram
source then lists every Reel and video, newest first. Each item's link is the post's permalink, which Reelay
downloads like any pasted reel link.

Only graph.instagram.com is contacted, only with the creator's own token, and only when they list the source.
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlparse

import requests

from .models import VideoItem

GRAPH = "https://graph.instagram.com"
API_VERSION = "v21.0"
MEDIA_FIELDS = "id,caption,media_type,media_product_type,permalink,timestamp,thumbnail_url,username"
PAGE_SIZE = 50
MAX_PAGES = 100                      # 5,000 posts: a guard against a paging loop, not a real limit
TIMEOUT = 30
_SHORTCODE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:reels?|p|tv)/([\w-]+)", re.IGNORECASE)


class InstagramApiError(Exception):
    """A plain-language reason (never contains the token)."""
    plain = True


def _error(resp, token: str) -> InstagramApiError:
    try:
        err = resp.json().get("error", {})
    except ValueError:
        err = {}
    code, message = err.get("code"), str(err.get("message", "")).replace(token, "[token]")
    if resp.status_code in (400, 401) and (code == 190 or "token" in message.lower()):
        return InstagramApiError("The Instagram token has expired or was revoked. Make a new one in your Meta app "
                                 "and connect again under Accounts → Instagram → Advanced Setup.")
    if resp.status_code == 429 or code in (4, 17, 32, 613):
        return InstagramApiError("Instagram is limiting requests right now. Wait a few minutes and try again.")
    return InstagramApiError(f"Instagram's API said no ({resp.status_code}){': ' + message if message else ''}.")


def _get(session, url: str, token: str, params: dict | None = None) -> dict:
    if urlparse(url).hostname != "graph.instagram.com":          # paging links must stay on Meta's API
        raise InstagramApiError("Instagram returned a paging link Reelay won't follow.")
    try:
        resp = session.get(url, params={**(params or {}), "access_token": token}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise InstagramApiError(f"Couldn't reach Instagram's API: {type(exc).__name__}.") from None
    if resp.status_code != 200:
        raise _error(resp, token)
    return resp.json()


def verify(credentials: dict, session=None) -> dict:
    """Check a pasted token: whose account it is (TokenSetupDialog's `verify`)."""
    token = (credentials.get("access_token") or "").strip()
    if not token:
        raise InstagramApiError("Paste the Instagram access token first.")
    data = _get(session or requests.Session(), f"{GRAPH}/{API_VERSION}/me", token,
                {"fields": "user_id,username,account_type"})
    if str(data.get("account_type", "")).upper() not in ("", "BUSINESS", "MEDIA_CREATOR"):
        raise InstagramApiError("That's a personal Instagram account. Switch it to a Professional (Creator or "
                                "Business) account in Instagram's settings: Instagram's API only lists those.")
    username = data.get("username") or ""
    return {"external_id": str(data.get("user_id") or data.get("id")), "display_name": username or "Instagram",
            "handle": f"@{username}" if username else ""}


def shortcode(permalink: str) -> str:
    m = _SHORTCODE.search(permalink or "")
    return m.group(1) if m else ""


def to_item(media: dict) -> VideoItem | None:
    """One API media object → a Library item (videos and Reels only; photos and carousels are skipped)."""
    if media.get("media_type") != "VIDEO":
        return None
    code = shortcode(media.get("permalink", ""))
    if not code:
        return None
    caption = (media.get("caption") or "").strip()
    first = caption.splitlines()[0].strip() if caption else ""
    date = ""
    if media.get("timestamp"):
        try:
            date = datetime.strptime(media["timestamp"][:10], "%Y-%m-%d").strftime("%Y%m%d")
        except ValueError:
            date = ""
    return VideoItem(platform="Instagram", video_id=code, url=media["permalink"], title=first[:100],
                     description=caption, upload_date=date, uploader=media.get("username") or "",
                     thumbnail_url=media.get("thumbnail_url") or "")


def list_media(token: str, session=None, limit: int | None = None) -> tuple[str, list[VideoItem]]:
    """(username, the account's videos and Reels, newest first), following every page."""
    session = session or requests.Session()
    url, params = f"{GRAPH}/{API_VERSION}/me/media", {"fields": MEDIA_FIELDS, "limit": PAGE_SIZE}
    items: list[VideoItem] = []
    username = ""
    for _page in range(MAX_PAGES):
        data = _get(session, url, token, params)
        for media in data.get("data", []):
            username = username or media.get("username") or ""
            item = to_item(media)
            if item is not None:
                items.append(item)
        if limit and len(items) >= limit:
            return username, items[:limit]
        url = (data.get("paging") or {}).get("next")
        if not url:
            break
        params = None                                # the next link carries every parameter but the token
    return username, items
