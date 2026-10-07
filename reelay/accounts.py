"""Connected-account plumbing: OAuth client config, tokens, API clients.

Network work (sign-in, API calls) runs on background threads; anything that
writes the Store is done by the caller on the GUI thread.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Callable

from .models import Account
from .store import Store
from .tokens import TokenStore
from .youtube import OAuthClient, Tokens, YouTubeClient, YouTubeError, authorize, revoke

log = logging.getLogger("reelay")

YOUTUBE = "youtube"
GOOGLE_CLIENT_SETTING = "google_oauth_client"


class AccountService:
    def __init__(self, store: Store, tokens: TokenStore):
        self.store = store
        self.tokens = tokens

    # ---------------------------------------------------------------- OAuth client
    def oauth_client(self) -> OAuthClient | None:
        raw = self.store.get_setting(GOOGLE_CLIENT_SETTING)
        if not raw:
            return None
        try:
            return OAuthClient(**json.loads(raw))
        except (ValueError, TypeError):
            return None

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
        oauth = self.oauth_client()
        if oauth is None:
            raise YouTubeError("Add your Google API credentials in Accounts first.")
        tokens = self.load_tokens(account)
        if tokens is None:
            raise YouTubeError(f"Reconnect {account.display_name}: its sign-in is missing from the keychain.")
        key = account.token_key
        return YouTubeClient(oauth, tokens, on_tokens=lambda t: self.save_tokens(key, t))

    # ---------------------------------------------------------------- connect / disconnect
    @staticmethod
    def sign_in(oauth: OAuthClient, open_browser: Callable[[str], object],
                cancel: threading.Event | None = None) -> tuple[dict, Tokens]:
        """Background thread: browser sign-in, then read the channel."""
        tokens = authorize(oauth, open_browser=open_browser, cancel=cancel)
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
        """Remove the account; revoke and forget tokens unless another profile uses them."""
        shared = self.store.account_in_use_elsewhere(account)
        self.store.delete_account(account.id)
        if shared:
            return
        tokens = self.load_tokens(account)
        if tokens:
            threading.Thread(target=revoke, args=(tokens,), daemon=True).start()
        self.tokens.delete(account.token_key)
