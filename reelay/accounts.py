"""Connected-account plumbing: OAuth client config, credentials, API clients.

Every secret lives in the OS keychain through a TokenStore (macOS Keychain,
Windows Credential Manager), under service "Reelay":

    agent_api_token            bearer token for the agent API
    youtube:<channel id>       Google OAuth tokens (JSON)
    bluesky:<did>              {"handle", "app_password", "service"}
    threads:<user id>          {"access_token", "user_id"}
    facebook:<page id>         {"page_id", "page_access_token"}

The database only records which accounts exist and when they were last
verified. Secrets are never returned by the API or written to logs.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from typing import Callable

from .models import Account
from .store import Store
from .tokens import TokenStore
from .youtube import OAuthClient, Tokens, YouTubeClient, YouTubeError, authorize, revoke

log = logging.getLogger("reelay")

YOUTUBE = "youtube"
GOOGLE_CLIENT_SETTING = "google_oauth_client"
AGENT_TOKEN_KEY = "agent_api_token"

# Secret fields each platform keeps in the keychain (all required).
CREDENTIAL_FIELDS: dict[str, tuple[str, ...]] = {
    "bluesky": ("handle", "app_password"),
    "threads": ("access_token",),
    "facebook": ("page_id", "page_access_token"),
    "tiktok": ("access_token",),        # signed in with Reelay's TikTok app (analytics; posting stays by hand)
    "instagram": ("access_token",),     # signed in with Reelay's Instagram app
}


class AccountService:
    def __init__(self, store: Store, tokens: TokenStore):
        self.store = store
        self.tokens = tokens

    # ---------------------------------------------------------------- OAuth client
    def own_oauth_client(self) -> OAuthClient | None:
        """The Google project the person set up themselves (Set Up YouTube…)."""
        raw = self.store.get_setting(GOOGLE_CLIENT_SETTING)
        if not raw:
            return None
        try:
            return OAuthClient(**json.loads(raw))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def central_oauth_client() -> OAuthClient | None:
        """Reelay's own Google app ("Sign in with Google" with no setup), if this build has one."""
        from . import oauth_config
        cid, secret = oauth_config.client_id("google"), oauth_config.value("GOOGLE_CLIENT_SECRET")
        return OAuthClient(cid, secret) if cid and secret else None

    def oauth_client(self) -> OAuthClient | None:
        """The Google app to sign in with: your own project if you set one up,
        otherwise Reelay's."""
        return self.own_oauth_client() or self.central_oauth_client()

    @staticmethod
    def central_pending() -> bool:
        """Reelay's Google sign-in is set up in this build but not finished (the client ID is a placeholder)."""
        from . import oauth_config
        return oauth_config.pending_setup("google")

    def client_for_issuer(self, issuer: str) -> OAuthClient | None:
        """The Google app that can refresh tokens it issued: Reelay's or your own (older sign-ins: either)."""
        if issuer == "reelay":
            return self.central_oauth_client()
        if issuer == "own":
            return self.own_oauth_client()
        return self.oauth_client()

    def needs_reconnect(self, account: Account) -> str:
        """Why a YouTube channel must be signed in again, or "" if it's fine."""
        if account.platform != YOUTUBE:
            return ""
        tokens = self.load_tokens(account)
        if tokens is None:
            return "Signed out: sign in again"
        if self.client_for_issuer(tokens.issuer) is None:
            return ("Connected with your own Google project, which was removed: sign in again"
                    if tokens.issuer == "own" else "Reelay's Google sign-in isn't available: sign in again")
        return ""

    def google_client_source(self) -> str | None:
        """"own" (your Google project), "reelay" (Reelay's app) or None (needs setup)."""
        if self.own_oauth_client():
            return "own"
        return "reelay" if self.central_oauth_client() else None

    def set_oauth_client(self, client: OAuthClient | None) -> None:
        self.store.set_setting(GOOGLE_CLIENT_SETTING,
                               json.dumps({"client_id": client.client_id, "client_secret": client.client_secret})
                               if client else "")

    # ---------------------------------------------------------------- tokens
    def load_tokens(self, account: Account) -> Tokens | None:
        raw = self.tokens.get(account.token_key)
        return Tokens.from_json(raw) if raw else None

    def save_tokens(self, account_key: str, tokens: Tokens) -> None:
        self.tokens.set(account_key, tokens.to_json())

    def client_for(self, account: Account) -> YouTubeClient:
        """Thread-safe to *use*; build it on the GUI thread."""
        tokens = self.load_tokens(account)
        if tokens is None:
            raise YouTubeError(f"Reconnect {account.display_name}: its sign-in is missing from the keychain.")
        oauth = self.client_for_issuer(tokens.issuer)     # tokens only refresh with the app that issued them
        if oauth is None:
            raise YouTubeError(f"Reconnect {account.display_name} in Accounts: the Google app it was connected "
                               "with isn't set up any more.")
        key = account.token_key
        return YouTubeClient(oauth, tokens, on_tokens=lambda t: self.save_tokens(key, t))

    # ---------------------------------------------------------------- connect / disconnect
    @staticmethod
    def sign_in(oauth: OAuthClient, open_browser: Callable[[str], object],
                cancel: threading.Event | None = None) -> tuple[dict, Tokens]:
        """Background thread: browser sign-in, then read the channel. Reelay's own app asks for the
        reviewed YouTube permissions (oauth.PROVIDERS), plus any analytics ones once those are added."""
        from .oauth import PROVIDERS
        central = AccountService.central_oauth_client()
        scopes = list(PROVIDERS["google"].scopes + PROVIDERS["google"].analytics_scopes) \
            if central and oauth == central else None
        tokens = authorize(oauth, open_browser=open_browser, cancel=cancel, scopes=scopes)
        tokens.issuer = "reelay" if central and oauth == central else "own"
        channel = YouTubeClient(oauth, tokens).my_channel()
        return channel, tokens

    def finish_connect(self, profile_id: int, channel: dict, tokens: Tokens) -> Account:
        """GUI thread: store the account and its tokens."""
        account = Account(profile_id=profile_id, platform=YOUTUBE, external_id=channel["id"],
                          display_name=channel["title"] or channel["id"], handle=channel.get("handle", ""),
                          avatar_url=channel.get("thumbnail", ""))
        self.save_tokens(account.token_key, tokens)
        return self.store.save_account(account)

    def disconnect(self, account: Account) -> None:
        """Remove the account; revoke and forget its secret unless another profile uses it."""
        shared = self.store.account_in_use_elsewhere(account)
        self.store.delete_account(account.id)
        if shared:
            return
        if account.platform == YOUTUBE:
            tokens = self.load_tokens(account)
            if tokens:
                threading.Thread(target=revoke, args=(tokens,), daemon=True).start()
        else:
            secret = self._secret(account)
            if secret.get("via") == "reelay" and secret.get("access_token"):   # signed in with Reelay's app
                from .oauth import revoke_access
                threading.Thread(target=revoke_access, args=(account.platform, secret), daemon=True).start()
        self.tokens.delete(account.token_key)

    def _secret(self, account: Account) -> dict:
        try:
            data = json.loads(self.tokens.get(account.token_key) or "{}")
        except Exception:  # noqa: BLE001 — an unreadable keychain entry just isn't revoked
            return {}
        return data if isinstance(data, dict) else {}

    # ---------------------------------------------------------------- other platforms
    def credentials(self, account: Account) -> dict | None:
        """The account's secret fields, or None if missing / unreadable."""
        raw = self.tokens.get(account.token_key)
        if not raw:
            return None
        if account.platform == YOUTUBE:
            return {"oauth": True}
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        fields = CREDENTIAL_FIELDS.get(account.platform, ())
        return data if all(data.get(f) for f in fields) else None

    def has_credentials(self, account: Account) -> bool:
        try:
            return self.credentials(account) is not None
        except Exception:  # noqa: BLE001 — a locked/unavailable keychain reads as "not present"
            log.warning("Couldn't read the keychain entry for %s", account.token_key)
            return False

    def connect_platform(self, profile_id: int, platform: str, identity: dict, secret: dict) -> Account:
        """Store a verified Bluesky / Threads / Facebook account and its secret.

        `identity` comes from the publisher's live check: external_id,
        display_name, handle."""
        missing = [f for f in CREDENTIAL_FIELDS[platform] if not secret.get(f)]
        if missing:
            raise ValueError(f"Missing {', '.join(missing)}.")
        account = Account(profile_id=profile_id, platform=platform, external_id=identity["external_id"],
                          display_name=identity.get("display_name") or identity["external_id"],
                          handle=identity.get("handle", ""))
        self.tokens.set(account.token_key, json.dumps(secret))
        account = self.store.save_account(account)
        self.store.mark_verified(account.id)
        return account

    def connect_signed_in(self, profile_id: int, platform: str, tokens, found: dict) -> Account:
        """Store an account attached with Reelay's "Sign in with …" (TikTok,
        Instagram, Threads, Facebook). `found` is one entry from oauth.identity()."""
        secret = {"access_token": tokens.access_token, "refresh_token": tokens.refresh_token,
                  "expires_at": tokens.expires_at, "via": "reelay", **found.get("secret", {})}
        identity = {k: found[k] for k in ("external_id", "display_name", "handle") if k in found}
        return self.connect_platform(profile_id, platform, identity, secret)

    def account_for(self, profile_id: int, platform: str, account_id: int | None = None) -> Account | None:
        """The account a post should use on a platform: the given one, else the profile's first."""
        if account_id is not None:
            account = self.store.get_account(account_id)
            if account and account.platform == platform and account.profile_id == profile_id:
                return account
        accounts = self.store.list_accounts(profile_id, platform)
        return accounts[0] if accounts else None

    # ---------------------------------------------------------------- agent API token
    def agent_token(self) -> str:
        """REELAY_API_TOKEN if set; else the keychain value, generated on first use."""
        env = os.environ.get("REELAY_API_TOKEN")
        if env:
            return env
        token = self.tokens.get(AGENT_TOKEN_KEY)
        if not token:
            token = secrets.token_urlsafe(32)
            self.tokens.set(AGENT_TOKEN_KEY, token)
            log.info("Generated a new agent API token and stored it in the system keychain "
                     "(service \"Reelay\", account \"%s\").", AGENT_TOKEN_KEY)
        return token

    def rotate_agent_token(self) -> str:
        token = secrets.token_urlsafe(32)
        self.tokens.set(AGENT_TOKEN_KEY, token)
        log.info("Rotated the agent API token (keychain account \"%s\").", AGENT_TOKEN_KEY)
        return token
