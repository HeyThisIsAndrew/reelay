"""Reelay's own (central) app credentials for "Sign in with …".

Once Reelay registers its apps with each platform, their public client IDs go
in DEFAULTS (or in environment variables, which win). Secrets never ship in
the desktop app except Google's: Google treats a desktop app's client secret
as non-confidential by design. Every other platform's secret lives on the
Reelay sign-in server (the "broker", see server/auth_broker.py), whose address
goes in REELAY_AUTH_BROKER_URL.

    REELAY_GOOGLE_CLIENT_ID / REELAY_GOOGLE_CLIENT_SECRET
    REELAY_TIKTOK_CLIENT_ID      (TikTok calls it the client key)
    REELAY_INSTAGRAM_CLIENT_ID / REELAY_THREADS_CLIENT_ID / REELAY_FACEBOOK_CLIENT_ID
    REELAY_AUTH_BROKER_URL       e.g. https://auth.reelay.app
    REELAY_BLUESKY_CLIENT_ID     the address of Reelay's Bluesky client metadata file

Bluesky needs no app review and no secret: its client ID is the https address of a small JSON file on the
Reelay website (bluesky_oauth.CLIENT_ID_URL). It stays empty, so "Sign in with Bluesky" stays hidden, until that
file is live; then set BLUESKY_CLIENT_ID below to bluesky_oauth.CLIENT_ID_URL.

Until a value is filled in, that platform keeps its current setup (your own
Google project, App Passwords, pasted tokens).

GOOGLE_CLIENT_ID ships as a placeholder until Reelay's Google app is approved: anything still containing
PLACEHOLDER counts as "not set up", so the app says so plainly instead of sending people to a broken
Google page. Replace it (or set REELAY_GOOGLE_CLIENT_ID) with the real client ID to switch it on.
"""

from __future__ import annotations

import os

# Filled in when Reelay's apps are registered and approved. Empty = not available yet.
PLACEHOLDER = "PLACEHOLDER"
DEFAULTS: dict[str, str] = {
    "GOOGLE_CLIENT_ID": f"REELAY_GOOGLE_CLIENT_ID_{PLACEHOLDER}.apps.googleusercontent.com",
    "GOOGLE_CLIENT_SECRET": "",
    "TIKTOK_CLIENT_ID": "",
    "INSTAGRAM_CLIENT_ID": "",
    "THREADS_CLIENT_ID": "",
    "FACEBOOK_CLIENT_ID": "",
    "BLUESKY_CLIENT_ID": "",      # "https://reelay.app/oauth/bluesky-client-metadata.json" once it's live
    "AUTH_BROKER_URL": "",
}


def value(name: str) -> str:
    """REELAY_<NAME> from the environment, else the built-in default."""
    return (os.environ.get(f"REELAY_{name}") or DEFAULTS.get(name, "")).strip()


def is_placeholder(text: str) -> bool:
    return PLACEHOLDER in (text or "")


def client_id(provider: str) -> str:
    """The provider's client ID, or "" while it's missing or still the placeholder."""
    cid = value(f"{provider.upper()}_CLIENT_ID")
    return "" if is_placeholder(cid) else cid


def pending_setup(provider: str) -> bool:
    """True while this build only has the placeholder for the provider (set up, but not finished)."""
    return is_placeholder(value(f"{provider.upper()}_CLIENT_ID"))


def broker_url() -> str:
    return value("AUTH_BROKER_URL").rstrip("/")
