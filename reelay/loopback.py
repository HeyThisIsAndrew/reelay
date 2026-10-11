"""Catching a sign-in redirect on this computer (the OAuth "loopback" redirect).

A browser sign-in ends with the platform (or the Reelay sign-in server) sending the browser to
``http://127.0.0.1:<port><path>?state=…&code=…``. ``LoopbackReceiver`` listens for exactly that, once:

- **127.0.0.1 only**, on a random free port chosen by the operating system.
- **Only the real redirect counts:** the path must match, the ``state`` must be the one this sign-in
  generated (compared in constant time), and the Host header must be this address, so a stray or hostile
  request from another program or web page (DNS rebinding) can't end the sign-in early or slip in its own
  code. Anything else gets an error page and the receiver keeps waiting.
- **Gone right after:** once the real redirect arrives the server stops listening, and ``wait`` always
  closes it, so a port is never left open.
- **Nothing sensitive echoed or kept:** the page shown in the browser never repeats the code; responses are
  ``no-store`` with ``no-referrer`` so the code can't leak through the browser cache or a Referer header.
"""

from __future__ import annotations

import html
import http.server
import secrets
import threading
import time
import urllib.parse

DONE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Reelay</title>
<style>body{{font-family:-apple-system,system-ui,sans-serif;background:#1f1e1d;color:#ecebe7;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0}}main{{text-align:center;max-width:420px}}
h1{{font-size:22px;margin:0 0 8px}}p{{color:#a9a8a2;line-height:1.5}}</style></head>
<body><main><h1>{title}</h1><p>{body}</p></main></body></html>"""


class LoopbackError(Exception):
    """Plain-language reason no redirect arrived (timed out or cancelled)."""


class LoopbackReceiver:
    """One-shot receiver for a sign-in redirect. Use as a context manager, or call ``close()``."""

    def __init__(self, state: str | None = None, path: str = "/callback", label: str = "") -> None:
        """``state`` can be set later with ``expect()`` (when it has to include the port, which is only known
        once the server is listening). Until then every request is turned away."""
        self.state: str | None = None
        if state is not None:
            self.expect(state)
        self.path = path if path.startswith("/") else "/" + path    # exactly as registered (TikTok: /callback/)
        self.label = label or "the sign-in"
        self.result: dict[str, str] = {}
        self.rejected = 0                       # requests that weren't the real redirect
        self._done = threading.Event()
        self._closed = False
        receiver = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                receiver._handle(self)

            def do_POST(self) -> None:  # noqa: N802
                self._reply_plain(405)

            def _reply_plain(self, code: int) -> None:
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args) -> None:   # never log URLs: they carry the code
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, name="reelay-loopback", daemon=True)
        self._thread.start()

    def expect(self, state: str) -> None:
        if not state or len(state) < 16:
            raise ValueError("A sign-in needs an unguessable state value.")
        self.state = state

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.path}"

    # ------------------------------------------------------------ requests
    def _handle(self, req) -> None:
        url = urllib.parse.urlsplit(req.path)
        host = (req.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            return self._page(req, 400, "Not this address", "This page only answers Reelay's own sign-in.")
        if url.path != self.path:                        # favicon.ico, probes…
            self.rejected += 1
            return self._page(req, 404, "Nothing here", "This page only answers Reelay's own sign-in.")
        params = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))
        if self._done.is_set() or self.state is None or \
                not secrets.compare_digest(params.get("state", "").encode(), self.state.encode()):
            self.rejected += 1
            return self._page(req, 400, "This sign-in link has expired",
                              "Go back to Reelay and click Connect again.")
        self.result = params
        ok = "error" not in params
        self._page(req, 200, "You're connected" if ok else "Sign-in didn't finish",
                   "You can close this tab and go back to Reelay." if ok else
                   html.escape(params.get("error_description") or params.get("error") or "No code was returned."))
        self._done.set()
        threading.Thread(target=self._shutdown, daemon=True).start()   # stop listening right away

    @staticmethod
    def _page(req, code: int, title: str, body: str) -> None:
        data = DONE_PAGE.format(title=html.escape(title), body=body).encode()
        req.send_response(code)
        req.send_header("Content-Type", "text/html; charset=utf-8")
        req.send_header("Content-Length", str(len(data)))
        req.send_header("Cache-Control", "no-store")
        req.send_header("Referrer-Policy", "no-referrer")
        req.send_header("X-Content-Type-Options", "nosniff")
        req.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        req.end_headers()
        req.wfile.write(data)

    # ------------------------------------------------------------ waiting
    def wait(self, timeout: float = 300, cancel: threading.Event | None = None) -> dict[str, str]:
        """Block until the real redirect arrives; returns its query parameters (``code`` or ``error``…).
        Raises LoopbackError on timeout or cancel. Always closes the server."""
        deadline = time.monotonic() + timeout
        try:
            while not self._done.wait(0.2):
                if cancel is not None and cancel.is_set():
                    raise LoopbackError("Sign-in was cancelled.")
                if time.monotonic() > deadline:
                    raise LoopbackError(f"Timed out waiting for {self.label}. Please try again.")
            return dict(self.result)
        finally:
            self.close()

    def _shutdown(self) -> None:
        if not self._closed:
            self.server.shutdown()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self) -> "LoopbackReceiver":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
