"""One-time, expiring public links to local videos (for Threads).

Threads downloads the video from a URL rather than accepting an upload. Reelay
serves each video at /media/<random token> on the agent API server; through
your Cloudflare Tunnel that becomes a public HTTPS link. Links are
unguessable (32 random bytes), point at exactly one file, expire after an
hour, and live only in memory.

A link is only made for a real video file (``issue`` refuses anything else), and
it stays tied to that exact file: if the path is later swapped for another file
or a symlink to one, the link stops working instead of serving it.
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
        self._links: dict[str, tuple[Path, float, tuple[int, int]]] = {}   # token -> (file, expiry, identity)

    def issue(self, path: Path) -> str:
        """A token for one video file. Raises ValueError for anything that isn't a real video file."""
        from .urlsafety import VIDEO_TYPES, file_identity, looks_like_video
        try:
            real = Path(path).resolve(strict=True)
        except (OSError, RuntimeError):
            raise ValueError("The video file is missing.") from None
        identity = file_identity(real)
        if not real.is_file() or identity is None or real.suffix.lower() not in VIDEO_TYPES \
                or not looks_like_video(real):
            raise ValueError("Only video files can be shared through a media link.")
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = self.clock()
            self._links = {t: v for t, v in self._links.items() if v[1] > now}
            self._links[token] = (real, now + self.ttl, identity)
        return token

    def resolve(self, token: str) -> Path | None:
        """The file a live token points at, or None (expired, unknown, or the file was swapped)."""
        from .urlsafety import file_identity
        with self._lock:
            entry = self._links.get(token)
            if entry is None or entry[1] <= self.clock():
                self._links.pop(token, None)
                return None
            path, _expires, identity = entry
        try:
            same = (path.resolve(strict=True) == path and path.is_file()
                    and file_identity(path) == identity)
        except (OSError, RuntimeError):
            same = False
        return path if same else None

    def url(self, token: str, base: str) -> str:
        return f"{base.rstrip('/')}/media/{token}"
