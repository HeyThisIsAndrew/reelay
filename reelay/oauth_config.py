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

Until a value is filled in, that platform keeps its current setup (your own
Google project, App Passwords, pasted tokens).
"""

from __future__ import annotations

import os

# Filled in when Reelay's apps are registered and approved. Empty = not available yet.
DEFAULTS: dict[str, str] = {
    "GOOGLE_CLIENT_ID": "",
    "GOOGLE_CLIENT_SECRET": "",
    "TIKTOK_CLIENT_ID": "",
    "INSTAGRAM_CLIENT_ID": "",
    "THREADS_CLIENT_ID": "",
    "FACEBOOK_CLIENT_ID": "",
    "AUTH_BROKER_URL": "",
}


def value(name: str) -> str:
    """REELAY_<NAME> from the environment, else the built-in default."""
    return (os.environ.get(f"REELAY_{name}") or DEFAULTS.get(name, "")).strip()


def client_id(provider: str) -> str:
    return value(f"{provider.upper()}_CLIENT_ID")


def broker_url() -> str:
    return value("AUTH_BROKER_URL").rstrip("/")
