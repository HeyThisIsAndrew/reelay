"""Privacy & Cloud AI Opt-In Gate (#12).

Reelay is local-first by default. Cloud AI features (such as the Repurpose Engine
powered by Meta Muse Spark via the Meta Model API) are strictly opt-in:

1. Default state is OFF (False) for every new user and profile.
2. Before any transcript, title, description, or notes leave this Mac for
   https://api.meta.ai/v1, the creator must explicitly opt in.
3. Reelay uses the standard non-contributor tier ('muse-spark-1.3'), never a
   contributor model, so creator content is not used to train Meta's models.
4. Opt-in state is persisted locally in the Reelay settings table under
   'privacy.cloud_ai_opt_in' (or controlled via REELAY_CLOUD_AI_OPT_IN in the
   environment) and can be toggled at any time.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("reelay.privacy")

CLOUD_AI_SETTING_KEY = "privacy.cloud_ai_opt_in"
CLOUD_AI_ENV_VAR = "REELAY_CLOUD_AI_OPT_IN"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

DISCLOSURE_TEXT = (
    "Optional Cloud AI (Meta Model API):\n"
    "- What is sent: Only the selected ticket's title, description, notes, and transcript "
    "when you explicitly request platform copy.\n"
    "- Where it goes: Meta via the official Meta Model API (https://api.meta.ai/v1) using "
    "the standard non-contributor tier (muse-spark-1.3).\n"
    "- Privacy & training: Standard non-contributor tier requests are not used to improve "
    "Meta's models.\n"
    "- Control: Nothing runs automatically in the background, and nothing is saved to a "
    "ticket or published without your review and approval."
)


class PrivacyOptInRequiredError(Exception):
    """Raised when a cloud AI call is attempted before the creator has opted in."""


def is_cloud_ai_enabled(store: Any | None = None) -> bool:
    """Return True only if the creator has explicitly opted in to cloud AI features.

    Defaults to False. Checks:
    1. REELAY_CLOUD_AI_OPT_IN environment variable if set ('1'/'true' vs '0'/'false').
    2. Local Reelay Store setting 'privacy.cloud_ai_opt_in'.
    """
    raw_env = os.environ.get(CLOUD_AI_ENV_VAR)
    if raw_env is not None:
        val = raw_env.strip().lower()
        if val in _TRUE:
            return True
        if val in _FALSE:
            return False
        log.warning("Ignoring invalid %s=%r (expected 1 or 0)", CLOUD_AI_ENV_VAR, raw_env)

    if store is None:
        return False

    if hasattr(store, "get_setting"):
        raw_setting = str(store.get_setting(CLOUD_AI_SETTING_KEY, "0") or "0")
        return raw_setting.strip().lower() in _TRUE

    if hasattr(store, "get"):
        raw_val = store.get(CLOUD_AI_SETTING_KEY)
        if raw_val is not None:
            return str(raw_val).strip().lower() in _TRUE

    return False


def set_cloud_ai_enabled(store: Any, enabled: bool) -> None:
    """Persist the creator's cloud AI opt-in preference ('1' or '0') in the local store."""
    if store is None:
        raise ValueError("A valid store is required to persist cloud AI opt-in state.")

    serialized = "1" if enabled else "0"
    if hasattr(store, "set_setting"):
        store.set_setting(CLOUD_AI_SETTING_KEY, serialized)
    elif hasattr(store, "set"):
        store.set(CLOUD_AI_SETTING_KEY, serialized)
    elif isinstance(store, dict):
        store[CLOUD_AI_SETTING_KEY] = serialized
    else:
        raise TypeError(f"Unsupported store type for privacy setting: {type(store).__name__}")


def require_cloud_ai_opt_in(store: Any | None = None, *, opt_in: bool | None = None) -> None:
    """Verify that cloud AI is enabled before contacting the Meta Model API.

    Raises:
        PrivacyOptInRequiredError: If cloud AI is disabled.
    """
    if opt_in is False:
        raise PrivacyOptInRequiredError(
            "Cloud AI features are disabled. Opt in via Settings before using Meta Model API."
        )
    if opt_in is True:
        return
    if not is_cloud_ai_enabled(store):
        raise PrivacyOptInRequiredError(
            "Cloud AI features are disabled. Opt in via Settings before using Meta Model API."
        )
