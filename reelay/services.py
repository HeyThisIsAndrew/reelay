"""Everything behind the UI, wired once: accounts, upload queue, pipeline,
dispatcher and the agent API server. Used by the desktop app and by the
headless server (`python -m reelay.api`)."""

from __future__ import annotations

import logging
import os

from .accounts import AccountService
from .flags import enabled_platforms
from .media import MediaLinks
from .pipeline import Pipeline
from .publishers import default_publishers
from .scheduler import Dispatcher
from .store import Store
from .tokens import TokenStore
from .uploads import UploadManager

log = logging.getLogger("reelay")


class Services:
    def __init__(self, store: Store, tokens: TokenStore, qt_parent=None, publishers: dict | None = None):
        self.store = store
        self.tokens = tokens
        self.accounts = AccountService(store, tokens)
        self.uploads = UploadManager(qt_parent)
        self.media = MediaLinks()
        self.enabled = enabled_platforms(store)   # read once: dispatcher and scheduling always agree
        if publishers is None:
            publishers = {p: pub for p, pub in default_publishers().items() if p in self.enabled}
        self.pipeline = Pipeline(store, self.accounts, self.uploads, media=self.media, enabled=self.enabled)
        self.dispatcher = Dispatcher(store, self.pipeline, publishers)
        off = sorted(set(default_publishers()) - set(publishers))
        if off:
            log.info("Automatic posting is off for: %s (copy-paste only).", ", ".join(off))
        self.pipeline.on_scheduled = self.dispatcher.kick
        self.api = None
        self.recovery: list = []        # interrupted sends found at start-up (the window asks about them)
        self._agent_token: str | None = None

    # ---------------------------------------------------------------- agent token
    def agent_token(self) -> str:
        if self._agent_token is None:
            self._agent_token = self.accounts.agent_token()
        return self._agent_token

    def rotate_agent_token(self) -> str:
        if os.environ.get("REELAY_API_TOKEN"):
            raise RuntimeError("The token comes from REELAY_API_TOKEN; change that variable instead.")
        self._agent_token = self.accounts.rotate_agent_token()
        return self._agent_token

    # ---------------------------------------------------------------- lifecycle
    def start_background(self, api: bool = True) -> None:
        """Start the dispatcher and (unless REELAY_API_DISABLED=1) the agent API."""
        try:
            self.recovery = self.dispatcher.reconcile()   # before anything is sent: hold what needs a person
        except Exception:  # noqa: BLE001 — never block start-up on bookkeeping
            log.exception("Start-up reconciliation failed")
            self.recovery = []
        self.dispatcher.start()
        if api and os.environ.get("REELAY_API_DISABLED") != "1":
            from .api import ApiServer, create_app
            self.agent_token()  # create it now so the first request doesn't hit the keychain
            self.api = ApiServer(create_app(self.store, self.pipeline, self.accounts, self.agent_token, self.media))
            self.api.start()

    def stop(self) -> None:
        if self.api is not None:
            self.api.stop()
            self.api = None
        self.dispatcher.stop()
        self.uploads.shutdown()
