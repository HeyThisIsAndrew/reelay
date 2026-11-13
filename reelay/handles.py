"""Clean up what people type into handle, Page ID and token fields.

Platforms show handles with an @ ("@thisisandrew.bsky.social"), so that's how people type them. They also paste
whole profile links, copy a token with "Bearer " in front of it, or pick up a stray space or invisible character
from a web page. None of that should make a sign-in fail. Every field that takes a handle, Page ID or token
goes through here first, in the setup windows and again in the publishers, so details saved another way (the
agent API, an older version) get the same treatment.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_AT_SIGNS = "@＠"                                    # the fullwidth ＠ comes from some keyboards and web pages
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"))
_QUOTES = "\"'“”‘’`<>"
_APP_PASSWORD = re.compile(r"^[a-z0-9]{4}(?:-[a-z0-9]{4}){3}$")

# (hosts, the path pattern that holds the handle)
_PROFILE_LINKS: dict[str, tuple[tuple[str, ...], re.Pattern]] = {
    "bluesky": (("bsky.app",), re.compile(r"^/profile/@?([^/?#]+)")),
    "tiktok": (("tiktok.com",), re.compile(r"^/@([^/?#]+)")),
    "instagram": (("instagram.com",), re.compile(r"^/@?([A-Za-z0-9_.]+)/?$")),
    "threads": (("threads.net", "threads.com"), re.compile(r"^/@([^/?#]+)")),
    "youtube": (("youtube.com",), re.compile(r"^/@([^/?#]+)")),
}
_INSTAGRAM_NOT_A_PROFILE = {"reel", "reels", "p", "tv", "stories", "explore", "accounts", "direct"}

# Credential fields that hold a handle, a Page ID, or a pasted token.
HANDLE_FIELDS = {"handle", "username"}
TOKEN_FIELDS = {"access_token", "page_access_token", "refresh_token"}


def clean_text(text: str | None) -> str:
    """Trim whitespace, invisible characters and the quotes that come along with a copy."""
    out = (text or "").translate(_INVISIBLE).strip()
    while len(out) >= 2 and out[0] in _QUOTES and out[-1] in _QUOTES:
        out = out[1:-1].strip()
    return out.strip(_QUOTES).strip()


def _looks_like_link(text: str) -> bool:
    return "://" in text or "/" in text


def _host(url: str) -> str:
    parsed = urlparse(url if "://" in url else "https://" + url)
    host = (parsed.hostname or "").lower()
    return host[4:] if host.startswith("www.") else host.removeprefix("m.")


def handle_from_link(url: str, platform: str) -> str:
    """The handle in a profile link on that platform, or "" if it isn't one."""
    known = _PROFILE_LINKS.get(platform)
    if not known:
        return ""
    hosts, pattern = known
    host = _host(url)
    if not any(host == h or host.endswith("." + h) for h in hosts):
        return ""
    path = urlparse(url if "://" in url else "https://" + url).path
    m = pattern.match(path)
    if not m:
        return ""
    handle = m.group(1)
    if platform == "instagram" and handle.lower() in _INSTAGRAM_NOT_A_PROFILE:
        return ""
    return handle


def clean_handle(text: str | None, platform: str | None = None) -> str:
    """'@name', ' ＠name ', a profile link → 'name'. Bluesky handles are lowercased and a bare name gets
    '.bsky.social', which is what Bluesky's own sign-in does with it."""
    out = clean_text(text)
    if platform and _looks_like_link(out):
        out = handle_from_link(out, platform) or out
    out = out.lstrip(_AT_SIGNS).strip().rstrip("/")
    if platform == "bluesky" and out:
        out = out.lower()
        if "." not in out and "@" not in out:
            out += ".bsky.social"
    return out


def clean_app_password(text: str | None) -> str:
    """Bluesky App Passwords are four groups of four ('abcd-efgh-ijkl-mnop'). Copies sometimes arrive with
    spaces between the groups or none at all; anything else is passed through as typed."""
    out = clean_text(text)
    joined = re.sub(r"[\s\-–—]+", "-", out.lower())
    if _APP_PASSWORD.match(joined):
        return joined
    if re.fullmatch(r"[a-z0-9]{16}", joined):
        return "-".join(joined[i:i + 4] for i in range(0, 16, 4))
    return out


def clean_token(text: str | None) -> str:
    """A pasted access token, without 'Bearer ', 'access_token=' or the line breaks a copy can add."""
    out = re.sub(r"^(?:Bearer|OAuth)\s+", "", clean_text(text), flags=re.IGNORECASE)
    out = re.sub(r"\s+", "", out)
    if "access_token=" in out:
        out = out.split("access_token=", 1)[1].split("&", 1)[0]
    return clean_text(out)


def clean_page_id(text: str | None) -> str:
    """A Facebook Page ID typed as '@123', '123' or pasted as the Page's link → '123' (or the Page's
    username, which Facebook's API also accepts)."""
    out = clean_text(text)
    if _looks_like_link(out) and "facebook.com" in _host(out):
        parsed = urlparse(out if "://" in out else "https://" + out)
        page = parse_qs(parsed.query).get("id", [""])[0]
        if not page:
            parts = [p for p in parsed.path.split("/") if p]
            if parts and parts[0] in ("pages", "pg") and len(parts) > 1:
                parts = parts[1:]
                digits = [p for p in parts if p.isdigit()]
                page = digits[0] if digits else parts[0]
            else:
                page = parts[0] if parts else ""
        out = page
    return out.lstrip(_AT_SIGNS).strip().rstrip("/")


def clean_credentials(platform: str, secret: dict) -> dict:
    """Every field of a setup form, cleaned the way its kind of field needs."""
    out = {}
    for key, value in secret.items():
        if not isinstance(value, str):
            out[key] = value
        elif key in HANDLE_FIELDS:
            out[key] = clean_handle(value, platform)
        elif key == "app_password":
            out[key] = clean_app_password(value)
        elif key in TOKEN_FIELDS:
            out[key] = clean_token(value)
        elif key == "page_id":
            out[key] = clean_page_id(value)
        else:
            out[key] = clean_text(value)
    return out
