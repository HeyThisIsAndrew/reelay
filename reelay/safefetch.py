"""Download a file a platform points at (a cover image, a caption track) without it reaching this computer
or the local network, even through DNS tricks or redirects.

The usual check-then-download leaves a gap: the name is looked up once to check it and again to download,
so a name that switches to a private address in between (DNS rebinding) gets through, and a redirect is
followed without any check at all. Here the name is looked up ONCE per hop, every address it returns must be
public, and the connection goes to that checked address (TLS still verifies the certificate for the name).
Each redirect is a new hop, checked the same way, up to a small limit.
"""

from __future__ import annotations

import http.client
import socket
import ssl
from typing import Callable
from urllib.parse import urljoin, urlsplit

from .urlsafety import _public, asset_url_problem

MAX_BYTES = 25 * 1024 * 1024            # covers and caption files are far smaller
MAX_REDIRECTS = 3
REDIRECTS = (301, 302, 303, 307, 308)


class FetchRefused(Exception):
    """The file wasn't fetched (a refused address or redirect, an error status, too big)."""


def _default_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


def _connect(found, timeout) -> socket.socket:
    """A socket connected to exactly the checked address (no second lookup)."""
    family, socktype, proto, _canon, sockaddr = found
    sock = socket.socket(family, socktype, proto)
    try:
        sock.settimeout(timeout)
        sock.connect(sockaddr)
    except BaseException:
        sock.close()
        raise
    return sock


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, found, timeout):
        super().__init__(host, port, timeout=timeout)
        self._found = found

    def connect(self):
        self.sock = _connect(self._found, self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, found, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._found = found

    def connect(self):
        sock = _connect(self._found, self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)   # the name's certificate


def fetch_public(url: str, headers: dict | None = None, timeout: float = 15, max_bytes: int = MAX_BYTES,
                 max_redirects: int = MAX_REDIRECTS, context: ssl.SSLContext | None = None,
                 allowed: Callable[[str], bool] = _public) -> bytes:
    """The body of `url`. Raises FetchRefused. ``allowed`` decides which addresses may be connected to
    (public ones only; tests pass their own)."""
    context = context or _default_context()
    for _hop in range(max_redirects + 1):
        problem = asset_url_problem(url, resolve=False)            # scheme, user name, numeric local hosts
        if problem:
            raise FetchRefused(problem)
        parts = urlsplit(url)
        https = parts.scheme.lower() == "https"
        host = parts.hostname
        port = parts.port or (443 if https else 80)
        try:
            found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError, OSError):
            raise FetchRefused("That link's address can't be found.") from None
        if not found or not all(allowed(info[4][0]) for info in found):
            raise FetchRefused("Links to this computer or the local network can't be fetched.")
        conn = (_PinnedHTTPS(host, port, found[0], timeout, context) if https
                else _PinnedHTTP(host, port, found[0], timeout))
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        try:
            conn.request("GET", target, headers={"Accept": "*/*", **(headers or {})})
            resp = conn.getresponse()
            if resp.status in REDIRECTS:
                location = resp.getheader("Location")
                if not location:
                    raise FetchRefused(f"A redirect (HTTP {resp.status}) without a destination.")
                url = urljoin(url, location)
                continue
            if resp.status != 200:
                raise FetchRefused(f"HTTP {resp.status}")
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise FetchRefused("The file is larger than expected.")
            return data
        except (OSError, http.client.HTTPException) as exc:
            raise FetchRefused(f"Couldn't download it ({type(exc).__name__}).") from None
        finally:
            conn.close()
    raise FetchRefused("Too many redirects.")
