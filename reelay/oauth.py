"""One "Sign in with …" for every platform, using Reelay's own apps.

Each platform's rules (checked against their docs, October 2026):

- Google / YouTube: browser sign-in with a loopback redirect
  (http://127.0.0.1:<port>/) and PKCE. Google treats a desktop app's client
  secret as non-confidential, so the code is exchanged right here.
- TikTok (Login Kit for Desktop): loopback redirect (registered as
  http://127.0.0.1:*/callback/) and PKCE with a HEX-encoded SHA-256 challenge.
  The token request needs the client secret, so the exchange goes through
  the Reelay sign-in server (the broker).
- Instagram, Threads, Facebook (Meta): the code exchange needs the app secret
  and must happen server-side, and redirect URIs must be registered https
  addresses. So the platform redirects to the broker, which exchanges the code
  (and swaps it for a long-lived token); the app picks the result up with the
  one-time `state` it generated.
- Bluesky: atproto OAuth (PAR + DPoP, public client) is planned; App Passwords
  keep working until then.

Nothing here talks to a platform until someone clicks a sign-in button.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import asdict, dataclass, field
from typing import Callable

import requests

from . import oauth_config


class SignInError(Exception):
    """Plain-language reason a sign-in didn't finish (never contains a token)."""


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    platforms: tuple[str, ...]
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    analytics_scopes: tuple[str, ...] = ()
    scope_sep: str = " "
    pkce: str | None = "base64url"           # "base64url" | "hex" (TikTok) | None
    redirect: str = "loopback"               # "loopback" (back to the app) | "broker" (via the Reelay server)
    exchange: str = "broker"                 # "device" (Google only) | "broker"
    client_id_param: str = "client_id"
    loopback_path: str = "/"
    extra_params: tuple[tuple[str, str], ...] = ()
    status: str = "ready"                    # "ready" | "planned"
    note: str = ""


PROVIDERS: dict[str, Provider] = {p.key: p for p in (
    Provider("google", "Google", ("youtube",), "https://accounts.google.com/o/oauth2/v2/auth",
             "https://oauth2.googleapis.com/token",
             ("https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"),
             ("https://www.googleapis.com/auth/yt-analytics.readonly",),
             exchange="device", extra_params=(("access_type", "offline"), ("prompt", "consent select_account"))),
    Provider("tiktok", "TikTok", ("tiktok",), "https://www.tiktok.com/v2/auth/authorize/",
             "https://open.tiktokapis.com/v2/oauth/token/", ("user.info.basic", "video.list"),
             ("user.info.stats",), scope_sep=",", pkce="hex", client_id_param="client_key",
             loopback_path="/callback/"),
    Provider("instagram", "Instagram", ("instagram",), "https://www.instagram.com/oauth/authorize",
             "https://api.instagram.com/oauth/access_token",
             ("instagram_business_basic", "instagram_business_content_publish"),
             ("instagram_business_manage_insights",), scope_sep=",", pkce=None, redirect="broker"),
    Provider("threads", "Threads", ("threads",), "https://threads.com/oauth/authorize",
             "https://graph.threads.com/oauth/access_token", ("threads_basic", "threads_content_publish"),
             ("threads_manage_insights",), scope_sep=",", pkce=None, redirect="broker"),
    Provider("facebook", "Facebook", ("facebook",), "https://www.facebook.com/v23.0/dialog/oauth",
             "https://graph.facebook.com/v23.0/oauth/access_token",
             ("pages_show_list", "pages_manage_posts", "pages_read_engagement"), ("read_insights",),
             scope_sep=",", pkce=None, redirect="broker"),
    Provider("bluesky", "Bluesky", ("bluesky",), "", "", (), status="planned",
             note="Bluesky's own sign-in (atproto OAuth) is planned; connect with an App Password for now."),
)}

PLATFORM_PROVIDER = {platform: p.key for p in PROVIDERS.values() for platform in p.platforms}


def provider_for(platform: str) -> Provider | None:
    key = PLATFORM_PROVIDER.get(platform)
    return PROVIDERS.get(key) if key else None


def availability(provider: Provider) -> tuple[bool, str]:
    """(can sign in now, why not)."""
    if provider.status != "ready":
        return False, provider.note
    if not oauth_config.client_id(provider.key):
        return False, f"One-click {provider.label} sign-in arrives once Reelay's {provider.label} app is approved."
    if provider.exchange == "device" and not oauth_config.value(f"{provider.key.upper()}_CLIENT_SECRET"):
        return False, f"Reelay's {provider.label} sign-in isn't set up in this build."
    if (provider.exchange == "broker" or provider.redirect == "broker") and not oauth_config.broker_url():
        return False, f"One-click {provider.label} sign-in needs the Reelay sign-in server, which isn't set up yet."
    return True, ""


# ====================================================================== tokens
@dataclass
class TokenSet:
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0                  # unix time; 0 = unknown / long-lived
    scope: str = ""
    extra: dict = field(default_factory=dict)   # e.g. open_id, user_id

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> "TokenSet":
        return cls(**json.loads(text))

    @classmethod
    def from_response(cls, data: dict) -> "TokenSet":
        if not data.get("access_token"):
            raise SignInError("The platform didn't return an access token.")
        expires = data.get("expires_at") or (time.time() + float(data["expires_in"]) if data.get("expires_in") else 0)
        known = {"access_token", "refresh_token", "expires_in", "expires_at", "scope", "token_type"}
        return cls(access_token=data["access_token"], refresh_token=data.get("refresh_token") or "",
                   expires_at=float(expires or 0), scope=str(data.get("scope") or ""),
                   extra={k: v for k, v in data.items() if k not in known and isinstance(v, (str, int, float))})

    def __repr__(self) -> str:   # never print tokens
        return f"TokenSet(scope={self.scope!r}, expires_at={self.expires_at!r})"


def pkce_pair(kind: str | None) -> tuple[str, str]:
    """(verifier, challenge). TikTok wants the SHA-256 as hex; everyone else base64url."""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode()).digest()
    if kind == "hex":
        return verifier, digest.hex()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def scopes_for(provider: Provider, analytics: bool = True) -> str:
    return provider.scope_sep.join(provider.scopes + (provider.analytics_scopes if analytics else ()))


def authorize_url(provider: Provider, client_id: str, redirect_uri: str, state: str, challenge: str | None,
                  analytics: bool = True) -> str:
    query = {provider.client_id_param: client_id, "redirect_uri": redirect_uri, "response_type": "code",
             "scope": scopes_for(provider, analytics), "state": state, **dict(provider.extra_params)}
    if provider.pkce and challenge:
        query.update(code_challenge=challenge, code_challenge_method="S256")
    return f"{provider.authorize_url}?{urllib.parse.urlencode(query)}"


def _json(resp) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _error(data: dict, fallback) -> str:
    err = data.get("error")
    if isinstance(err, dict):
        err = err.get("message") or err.get("code")
    return str(data.get("error_description") or data.get("detail") or err or fallback)


# ====================================================================== the flow
def sign_in(provider_key: str, open_browser: Callable[[str], object] = webbrowser.open,
            cancel: threading.Event | None = None, session=None, timeout: float = 300,
            poll_interval: float = 1.5, analytics: bool = True) -> TokenSet:
    """Run the browser sign-in for one platform with Reelay's app and return
    its tokens. Raises SignInError. Blocking: call from a worker thread."""
    provider = PROVIDERS[provider_key]
    ok, why = availability(provider)
    if not ok:
        raise SignInError(why)
    http = session or requests
    client_id = oauth_config.client_id(provider.key)
    broker = oauth_config.broker_url()
    verifier, challenge = pkce_pair(provider.pkce) if provider.pkce else ("", None)
    state = secrets.token_urlsafe(32)

    if provider.redirect == "broker":
        redirect_uri = f"{broker}/v1/oauth/{provider.key}/callback"
        open_browser(authorize_url(provider, client_id, redirect_uri, state, challenge, analytics))
        return _poll_broker(http, broker, state, cancel, timeout, poll_interval)

    from .youtube import LoopbackReceiver, YouTubeError
    receiver = LoopbackReceiver()
    redirect_uri = f"http://127.0.0.1:{receiver.port}{provider.loopback_path}"
    open_browser(authorize_url(provider, client_id, redirect_uri, state, challenge, analytics))
    try:
        params = receiver.wait(timeout, cancel)
    except YouTubeError as exc:
        raise SignInError(str(exc).replace("Google", provider.label)) from None
    if params.get("state") != state:
        raise SignInError("The sign-in response didn't match this request. Please try again.")
    if "error" in params:
        raise SignInError(f"{provider.label} sign-in was cancelled." if params["error"] in ("access_denied",)
                          else f"{provider.label} sign-in failed: {params.get('error_description') or params['error']}")
    code = urllib.parse.unquote(params.get("code", ""))
    if provider.exchange == "device":
        resp = http.post(provider.token_url, data={
            "client_id": client_id, "client_secret": oauth_config.value(f"{provider.key.upper()}_CLIENT_SECRET"),
            "code": code, "code_verifier": verifier, "grant_type": "authorization_code",
            "redirect_uri": redirect_uri}, timeout=30)
    else:
        resp = http.post(f"{broker}/v1/oauth/{provider.key}/token", json={
            "code": code, "code_verifier": verifier, "redirect_uri": redirect_uri}, timeout=30)
    data = _json(resp)
    if resp.status_code != 200:
        raise SignInError(f"Couldn't finish {provider.label} sign-in: {_error(data, resp.status_code)}")
    return TokenSet.from_response(data)


def _poll_broker(http, broker: str, state: str, cancel, timeout: float, interval: float) -> TokenSet:
    """Meta redirects to the Reelay server; collect the result with our one-time state."""
    deadline = time.monotonic() + timeout
    while True:
        if cancel is not None and cancel.is_set():
            raise SignInError("Sign-in was cancelled.")
        if time.monotonic() > deadline:
            raise SignInError("Timed out waiting for sign-in. Please try again.")
        try:
            resp = http.get(f"{broker}/v1/oauth/session/{state}", timeout=15)
        except requests.RequestException:
            resp = None
        if resp is not None and resp.status_code == 200:
            data = _json(resp)
            if data.get("error"):
                raise SignInError(f"Sign-in failed: {_error(data, 'unknown error')}")
            return TokenSet.from_response(data)
        if resp is not None and resp.status_code not in (202, 404):
            raise SignInError(f"The Reelay sign-in server answered {resp.status_code}.")
        time.sleep(interval)


def refresh(provider_key: str, tokens: TokenSet, session=None) -> TokenSet:
    """Fresh tokens for a sign-in (through the broker, except Google)."""
    provider = PROVIDERS[provider_key]
    http = session or requests
    if provider.exchange == "device":
        resp = http.post(provider.token_url, data={
            "client_id": oauth_config.client_id(provider.key),
            "client_secret": oauth_config.value(f"{provider.key.upper()}_CLIENT_SECRET"),
            "refresh_token": tokens.refresh_token, "grant_type": "refresh_token"}, timeout=30)
    else:
        resp = http.post(f"{oauth_config.broker_url()}/v1/oauth/{provider.key}/refresh",
                         json={"refresh_token": tokens.refresh_token, "access_token": tokens.access_token},
                         timeout=30)
    data = _json(resp)
    if resp.status_code != 200:
        raise SignInError(f"{provider.label} access expired. Sign in again in Accounts.")
    fresh = TokenSet.from_response(data)
    fresh.refresh_token = fresh.refresh_token or tokens.refresh_token
    fresh.extra = {**tokens.extra, **fresh.extra}
    return fresh


# ====================================================================== who signed in
def identity(provider_key: str, tokens: TokenSet, session=None) -> list[dict]:
    """The account(s) the sign-in gives access to, as
    [{external_id, display_name, handle, secret}] (Facebook: one per Page)."""
    http = session or requests
    t = tokens.access_token
    if provider_key == "tiktok":
        resp = http.get("https://open.tiktokapis.com/v2/user/info/",
                        params={"fields": "open_id,display_name,username,avatar_url"},
                        headers={"Authorization": f"Bearer {t}"}, timeout=20)
        user = (_json(resp).get("data") or {}).get("user") or {}
        uid = user.get("open_id") or tokens.extra.get("open_id")
        return [{"external_id": uid, "display_name": user.get("display_name") or user.get("username") or uid,
                 "handle": f"@{user['username']}" if user.get("username") else "", "secret": {}}] if uid else []
    if provider_key == "instagram":
        resp = http.get("https://graph.instagram.com/me", params={"fields": "user_id,username", "access_token": t},
                        timeout=20)
        me = _json(resp)
        uid = str(me.get("user_id") or me.get("id") or "")
        return [{"external_id": uid, "display_name": me.get("username") or uid,
                 "handle": f"@{me['username']}" if me.get("username") else "", "secret": {"user_id": uid}}] if uid else []
    if provider_key == "threads":
        resp = http.get("https://graph.threads.com/v1.0/me", params={"fields": "id,username", "access_token": t},
                        timeout=20)
        me = _json(resp)
        uid = str(me.get("id") or "")
        return [{"external_id": uid, "display_name": me.get("username") or uid,
                 "handle": f"@{me['username']}" if me.get("username") else "", "secret": {"user_id": uid}}] if uid else []
    if provider_key == "facebook":
        resp = http.get("https://graph.facebook.com/v23.0/me/accounts",
                        params={"fields": "id,name,access_token", "access_token": t}, timeout=20)
        pages = _json(resp).get("data") or []
        return [{"external_id": str(p["id"]), "display_name": p.get("name") or str(p["id"]), "handle": "",
                 "secret": {"page_id": str(p["id"]), "page_access_token": p.get("access_token", "")}}
                for p in pages if p.get("id") and p.get("access_token")]
    raise SignInError(f"Reelay can't look up {provider_key} accounts yet.")
