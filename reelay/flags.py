"""Feature flags for automatic posting.

Reelay is a content ticket system first: every card carries ready-to-paste
copy for each platform. Automatic posting to Bluesky, Threads and Facebook is
built and tested but OFF by default. YouTube posting is always available.

Each flag is read from, in order:
1. an environment variable (wins when set): REELAY_PUBLISH_BLUESKY=1,
   REELAY_PUBLISH_THREADS=1, REELAY_PUBLISH_FACEBOOK=1 (0 forces off), then
2. the setting "publish.<platform>" (Settings → Automatic posting), then
3. the default: off.

Flags are read once when Reelay starts (see Services), so the publishers the
dispatcher has and the platforms scheduling accepts always agree.
"""

from __future__ import annotations

import logging
import os

from .models import Platform

log = logging.getLogger("reelay")

FLAG_PLATFORMS = (Platform.BLUESKY, Platform.THREADS, Platform.FACEBOOK)
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def env_var(platform: str) -> str:
    return f"REELAY_PUBLISH_{platform.upper()}"


def setting_key(platform: str) -> str:
    return f"publish.{platform}"


def flag_source(platform: str, store=None) -> tuple[bool, str]:
    """(enabled, where the value came from: "always" | "env" | "setting" | "default")."""
    if platform == Platform.YOUTUBE:
        return True, "always"
    if platform not in FLAG_PLATFORMS:
        return False, "default"
    raw = os.environ.get(env_var(platform))
    if raw is not None:
        value = raw.strip().lower()
        if value in _TRUE:
            return True, "env"
        if value in _FALSE:
            return False, "env"
        log.warning("Ignoring %s=%r (use 1 or 0)", env_var(platform), raw)
    if store is not None and store.get_setting(setting_key(platform)) == "1":
        return True, "setting"
    return False, "default"


def publishing_enabled(platform: str, store=None) -> bool:
    return flag_source(platform, store)[0]


def enabled_platforms(store=None) -> frozenset[str]:
    """Platforms Reelay may post to automatically (YouTube always included)."""
    return frozenset(p for p in Platform.ALL if publishing_enabled(p, store))
