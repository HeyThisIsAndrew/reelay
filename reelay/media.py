"""One-time, expiring public links to local videos (for Threads).

Threads downloads the video from a URL rather than accepting an upload. Reelay
serves each video at /media/<random token> on the agent API server; through
your Cloudflare Tunnel that becomes a public HTTPS link. Links are
unguessable (32 random bytes), point at exactly one file, expire after an
hour, and live only in memory.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path

DEFAULT_TTL_SECONDS = 3600


def public_base_url(store=None) -> str:
    """REELAY_PUBLIC_URL, else the Settings value; '' when not configured."""
    url = os.environ.get("REELAY_PUBLIC_URL") or (store.get_setting("public_url") if store else "")
    return url.rstrip("/")


class MediaLinks:
    def __init__(self, ttl: float = DEFAULT_TTL_SECONDS, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._lock = threading.Lock()
        self._links: dict[str, tuple[Path, float]] = {}

    def issue(self, path: Path) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = self.clock()
            self._links = {t: v for t, v in self._links.items() if v[1] > now}
            self._links[token] = (Path(path), now + self.ttl)
        return token

    def resolve(self, token: str) -> Path | None:
        with self._lock:
            entry = self._links.get(token)
            if entry is None or entry[1] <= self.clock():
                self._links.pop(token, None)
                return None
            return entry[0]

    def url(self, token: str, base: str) -> str:
        return f"{base.rstrip('/')}/media/{token}"
