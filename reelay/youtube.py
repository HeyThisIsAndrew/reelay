"""YouTube Data API v3: OAuth for desktop apps, channel info, scheduled uploads.

Implemented on plain `requests` (already a yt-dlp dependency) rather than
google-api-python-client, which keeps the app small and every HTTP call
easy to fake in tests.

OAuth uses Google's "installed app" flow: the system browser opens Google's
consent page, which redirects to a one-shot server on 127.0.0.1 inside the
app. PKCE protects the code exchange.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import requests

log = logging.getLogger("reelay")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
CHUNK_SIZE = 8 * 1024 * 1024  # must be a multiple of 256 KiB
MAX_RETRIES = 5


class YouTubeError(Exception):
    """An API call failed; the message is suitable to show the user."""

    def __init__(self, message: str, status: int | None = None, reason: str = ""):
        super().__init__(message)
        self.status, self.reason = status, reason


class ReauthRequired(YouTubeError):
    """The refresh token was revoked or expired; the user must reconnect."""


class UploadCancelled(Exception):
    pass


# ====================================================================== OAuth
@dataclass
class OAuthClient:
    client_id: str
    client_secret: str

    @classmethod
    def from_json(cls, data: dict | str | Path) -> OAuthClient:
        """Accept the client_secret_*.json file downloaded from Google Cloud."""
        if isinstance(data, (str, Path)) and Path(data).is_file():
            data = json.loads(Path(data).read_text(encoding="utf-8"))
        elif isinstance(data, str):
            data = json.loads(data)
        section = data.get("installed") or data.get("web") or data
        try:
            return cls(client_id=section["client_id"], client_secret=section.get("client_secret", ""))
        except KeyError:
            raise ValueError("That file doesn't look like a Google OAuth client (no client_id).") from None


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str = ""
    issuer: str = ""          # "reelay" (Reelay's Google app) | "own" (your Google project) | "" (older sign-ins)

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at - 60

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> Tokens:
        data = json.loads(text)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def __repr__(self) -> str:   # never print tokens
        return f"Tokens(scope={self.scope!r}, issuer={self.issuer!r})"

    @classmethod
    def from_response(cls, data: dict, previous_refresh: str = "") -> Tokens:
        return cls(access_token=data["access_token"],
                   refresh_token=data.get("refresh_token") or previous_refresh,
                   expires_at=time.time() + float(data.get("expires_in", 3600)),
                   scope=data.get("scope", ""))


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url(client: OAuthClient, redirect_uri: str, state: str, challenge: str,
                   scopes: list[str] | None = None) -> str:
    query = {
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes or SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent select_account",  # always return a refresh token; allow picking a channel
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(query)}"


def authorize(client: OAuthClient, open_browser: Callable[[str], object] = webbrowser.open,
              timeout: float = 300, cancel: threading.Event | None = None,
              session: requests.Session | None = None, scopes: list[str] | None = None) -> Tokens:
    """Run the full browser sign-in and return tokens."""
    from .loopback import LoopbackError, LoopbackReceiver
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    receiver = LoopbackReceiver(state, path="/", label="Google sign-in")   # only the real redirect counts
    open_browser(build_auth_url(client, receiver.redirect_uri, state, challenge, scopes))
    try:
        params = receiver.wait(timeout, cancel)
    except LoopbackError as exc:
        raise YouTubeError(str(exc)) from None
    if "error" in params:
        raise YouTubeError("Google sign-in was cancelled." if params["error"] == "access_denied"
                           else f"Google sign-in failed: {params['error']}")
    return exchange_code(client, params["code"], verifier, receiver.redirect_uri, session)


def exchange_code(client: OAuthClient, code: str, verifier: str, redirect_uri: str,
                  session: requests.Session | None = None) -> Tokens:
    resp = (session or requests).post(TOKEN_URL, data={
        "client_id": client.client_id, "client_secret": client.client_secret, "code": code,
        "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": redirect_uri,
    }, timeout=30)
    data = _json(resp)
    if resp.status_code != 200 or "access_token" not in data:
        raise YouTubeError(f"Couldn't finish sign-in: {data.get('error_description') or data.get('error') or resp.status_code}")
    if not data.get("refresh_token"):
        raise YouTubeError("Google didn't return a refresh token. Remove Reelay's access at "
                           "myaccount.google.com/permissions and connect again.")
    return Tokens.from_response(data)


def refresh_tokens(client: OAuthClient, tokens: Tokens, session: requests.Session | None = None) -> Tokens:
    resp = (session or requests).post(TOKEN_URL, data={
        "client_id": client.client_id, "client_secret": client.client_secret,
        "refresh_token": tokens.refresh_token, "grant_type": "refresh_token",
    }, timeout=30)
    data = _json(resp)
    if resp.status_code == 400 and data.get("error") in ("invalid_grant", "unauthorized_client"):
        raise ReauthRequired("YouTube access expired or was revoked. Reconnect this channel in Accounts. "
                             "(Google expires sign-ins weekly while your API project is in Testing mode.)")
    if resp.status_code != 200:
        raise YouTubeError(f"Couldn't refresh YouTube access: {data.get('error_description') or resp.status_code}",
                           resp.status_code)
    fresh = Tokens.from_response(data, previous_refresh=tokens.refresh_token)
    fresh.issuer = tokens.issuer
    return fresh


def revoke(tokens: Tokens, session: requests.Session | None = None) -> None:
    try:
        (session or requests).post(REVOKE_URL, data={"token": tokens.refresh_token}, timeout=15)
    except requests.RequestException as exc:  # revoking is best-effort
        log.info("token revoke failed: %s", exc)


def _json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _api_error(resp) -> YouTubeError:
    data = _json(resp).get("error", {})
    errors = data.get("errors") or [{}]
    reason = errors[0].get("reason", "") if isinstance(errors, list) else ""
    message = data.get("message") or f"HTTP {resp.status_code}"
    friendly = {
        "quotaExceeded": "YouTube's daily API quota is used up. Uploads will work again after midnight Pacific time.",
        "uploadLimitExceeded": "This channel has hit YouTube's upload limit for now. Try again later.",
        "invalidPublishAt": "The scheduled time must be in the future.",
        "forbidden": "YouTube refused this request for the connected channel.",
    }.get(reason)
    return YouTubeError(friendly or f"YouTube error: {html.unescape(message)}", resp.status_code, reason)


# ====================================================================== API client
class YouTubeClient:
    """Authenticated calls for one channel. Refreshes tokens automatically and
    reports new tokens through `on_tokens` so they can be persisted."""

    def __init__(self, client: OAuthClient, tokens: Tokens,
                 on_tokens: Callable[[Tokens], None] | None = None,
                 session: requests.Session | None = None):
        self.client, self.tokens = client, tokens
        self.on_tokens = on_tokens or (lambda _t: None)
        self.session = session or requests.Session()

    def _auth_headers(self) -> dict[str, str]:
        if self.tokens.expired:
            self._refresh()
        return {"Authorization": f"Bearer {self.tokens.access_token}"}

    def _refresh(self) -> None:
        self.tokens = refresh_tokens(self.client, self.tokens, self.session)
        self.on_tokens(self.tokens)

    def request(self, method: str, url: str, **kwargs):
        headers = {**kwargs.pop("headers", {}), **self._auth_headers()}
        resp = self.session.request(method, url, headers=headers, timeout=kwargs.pop("timeout", 60), **kwargs)
        if resp.status_code == 401:  # token revoked early or clock skew: refresh once
            self._refresh()
            headers.update(self._auth_headers())
            resp = self.session.request(method, url, headers=headers, timeout=60, **kwargs)
        return resp

    def _get(self, path: str, params: dict) -> dict:
        resp = self.request("GET", f"{API}/{path}", params=params)
        if resp.status_code != 200:
            raise _api_error(resp)
        return _json(resp)

    # ---------------------------------------------------------------- reads
    def my_channel(self) -> dict:
        data = self._get("channels", {"part": "snippet,contentDetails", "mine": "true"})
        items = data.get("items") or []
        if not items:
            raise YouTubeError("This Google account doesn't have a YouTube channel yet.")
        ch = items[0]
        snip = ch.get("snippet", {})
        thumbs = snip.get("thumbnails", {})
        return {
            "id": ch["id"],
            "title": snip.get("title", ""),
            "handle": snip.get("customUrl", ""),
            "thumbnail": (thumbs.get("medium") or thumbs.get("default") or {}).get("url", ""),
            "uploads_playlist": ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", ""),
        }

    def video_statuses(self, video_ids: list[str]) -> dict[str, dict]:
        """{id: {privacy, publish_at, upload_status, title, published_at}}"""
        out: dict[str, dict] = {}
        for i in range(0, len(video_ids), 50):
            data = self._get("videos", {"part": "snippet,status", "id": ",".join(video_ids[i:i + 50])})
            for v in data.get("items", []):
                st, sn = v.get("status", {}), v.get("snippet", {})
                out[v["id"]] = {
                    "privacy": st.get("privacyStatus", ""),
                    "publish_at": st.get("publishAt"),
                    "upload_status": st.get("uploadStatus", ""),
                    "title": sn.get("title", ""),
                    "published_at": sn.get("publishedAt"),
                    "thumbnail": (sn.get("thumbnails", {}).get("medium") or {}).get("url", ""),
                }
        return out

    def recent_uploads(self, limit: int = 50) -> list[dict]:
        """The channel's latest uploads incl. private/scheduled ones (owner only)."""
        playlist = self.my_channel()["uploads_playlist"]
        if not playlist:
            return []
        data = self._get("playlistItems", {"part": "contentDetails", "playlistId": playlist,
                                           "maxResults": min(limit, 50)})
        ids = [i["contentDetails"]["videoId"] for i in data.get("items", [])]
        statuses = self.video_statuses(ids)
        return [{"id": vid, **statuses[vid]} for vid in ids if vid in statuses]

    def list_channel_videos(self, limit: int | None = None) -> list[dict]:
        """Every video on the connected channel (newest first), including private,
        unlisted and scheduled ones the owner can see. Paginates the uploads
        playlist 50 at a time; costs about 2 quota units per 50 videos."""
        playlist = self.my_channel()["uploads_playlist"]
        if not playlist:
            return []
        ids: list[str] = []
        page_token = None
        while True:
            params = {"part": "contentDetails", "playlistId": playlist, "maxResults": 50}
            if page_token:
                params["pageToken"] = page_token
            data = self._get("playlistItems", params)
            ids += [i["contentDetails"]["videoId"] for i in data.get("items", [])]
            page_token = data.get("nextPageToken")
            if not page_token or (limit and len(ids) >= limit):
                break
        ids = ids[:limit] if limit else ids
        videos: dict[str, dict] = {}
        for i in range(0, len(ids), 50):
            data = self._get("videos", {"part": "snippet,contentDetails,statistics,status",
                                        "id": ",".join(ids[i:i + 50])})
            videos.update({v["id"]: v for v in data.get("items", [])})
        return [videos[vid] for vid in ids if vid in videos]

    # ---------------------------------------------------------------- upload
    def upload(self, path: Path, resource: dict,
               on_progress: Callable[[int, int], None] | None = None,
               cancel: threading.Event | None = None, chunk_size: int = CHUNK_SIZE) -> dict:
        """Resumable upload. Returns the created video resource."""
        path = Path(path)
        total = path.stat().st_size
        cancel = cancel or threading.Event()
        on_progress = on_progress or (lambda _s, _t: None)

        resp = self.request(
            "POST", UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
            json=resource, headers={"X-Upload-Content-Length": str(total),
                                    "X-Upload-Content-Type": "video/mp4"})
        if resp.status_code != 200 or "Location" not in resp.headers:
            raise _api_error(resp)
        session_url = resp.headers["Location"]

        offset, retries = 0, 0
        on_progress(0, total)
        with path.open("rb") as fh:
            while True:
                if cancel.is_set():
                    raise UploadCancelled()
                fh.seek(offset)
                chunk = fh.read(chunk_size)
                end = offset + len(chunk) - 1
                try:
                    resp = self.request("PUT", session_url, data=chunk, timeout=300, headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end}/{total}" if chunk else f"bytes */{total}",
                    })
                except requests.RequestException as exc:
                    resp, network_error = None, exc
                else:
                    network_error = None

                if resp is not None and resp.status_code in (200, 201):
                    on_progress(total, total)
                    return _json(resp)
                if resp is not None and resp.status_code == 308:  # chunk accepted, keep going
                    offset = _next_offset(resp.headers.get("Range"))
                    retries = 0
                    on_progress(offset, total)
                    continue
                if resp is not None and resp.status_code not in (500, 502, 503, 504):
                    raise _api_error(resp)

                retries += 1
                if retries > MAX_RETRIES:
                    raise YouTubeError(f"Upload kept failing ({network_error or resp.status_code}). Try again later.")
                if cancel.wait(min(2 ** retries, 32)):
                    raise UploadCancelled()
                offset = self._query_offset(session_url, total)

    def _query_offset(self, session_url: str, total: int) -> int:
        """Ask the server how much it has after an interrupted chunk."""
        resp = self.request("PUT", session_url, data=b"", headers={"Content-Range": f"bytes */{total}"})
        if resp.status_code == 308:
            return _next_offset(resp.headers.get("Range"))
        return 0


def _next_offset(range_header: str | None) -> int:
    """'bytes=0-1048575' -> 1048576; missing header -> 0."""
    if not range_header or "-" not in range_header:
        return 0
    return int(range_header.rsplit("-", 1)[1]) + 1


def watch_url(video_id: str) -> str:
    return f"https://youtube.com/shorts/{video_id}"
