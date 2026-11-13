"""Sign in with Bluesky: AT Protocol OAuth for a desktop app, so nobody makes an App Password.

The rules, from the atproto OAuth spec (atproto.com/specs/oauth), RFC 9449 (DPoP) and Bluesky's reference
authorization server (bluesky-social/atproto, packages/oauth/oauth-provider), checked November 2026:

- **Reelay is a public native client.** Its client_id is the https address of its client metadata file
  (CLIENT_ID_URL, on the Reelay website), with ``application_type: native``, ``token_endpoint_auth_method:
  none`` and ``dpop_bound_access_tokens: true``. No secret ships in the app, and no Reelay server is involved.
- **Redirect:** the metadata registers ``http://127.0.0.1/callback`` with no port; a loopback redirect then
  matches on any port (RFC 8252 7.3), so the app listens on a random free one. ``localhost`` is refused by
  Bluesky. The token request must repeat the exact redirect_uri (port included) the sign-in used.
- **Scopes:** ``atproto`` (required) plus ``transition:generic``, the App Password level of access: it can
  write posts, upload blobs and get the service token the video service needs. It can't change the account,
  its email or password, and can't read DMs.
- **Finding the account's server:** handle → DID (``https://<handle>/.well-known/atproto-did``, else the
  official ``com.atproto.identity.resolveHandle``), DID → DID document (plc.directory or did:web) → PDS (the
  ``#atproto_pds`` service) → ``/.well-known/oauth-protected-resource`` → the authorization server, whose
  ``/.well-known/oauth-authorization-server`` metadata is checked before anything is sent. The handle must be
  claimed back by the DID document (bidirectional). With no handle typed, sign-in starts at bsky.social.
- **The flow:** PAR (pushed authorization request, required) with PKCE S256 and a DPoP proof → the browser
  opens the authorization page with only client_id + request_uri → the redirect brings state, iss and code;
  iss must be the server we started with → token request with DPoP → the token's ``sub`` (the DID) must
  match the account we resolved, and that account's PDS must name this same authorization server.
- **DPoP:** a fresh ES256 key per sign-in, kept in the Keychain with the tokens; every request carries a new
  proof (typ dpop+jwt, jti, htm, htu without query, iat, the server's nonce, and ath for the PDS). Servers
  hand out nonces in a ``DPoP-Nonce`` header and reject a missing or stale one (400 ``use_dpop_nonce`` from
  the authorization server, 401 ``WWW-Authenticate: DPoP error="use_dpop_nonce"`` from the PDS); the request
  is retried once with the new nonce. Nonces are kept per server.
- **Tokens:** access tokens last minutes; refresh tokens are single-use and rotate, and for a public client
  the whole sign-in lasts at most about two weeks without use, so Reelay refreshes as needed and asks to
  sign in again once Bluesky says no. Disconnect revokes the tokens at the server's revocation_endpoint.

Nothing here talks to Bluesky until someone clicks "Sign in with Bluesky" or a post is due.
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
from Cryptodome.Hash import SHA256
from Cryptodome.PublicKey import ECC
from Cryptodome.Signature import DSS

from .handles import clean_handle
from .oauth import SignInError, pkce_pair

# Where the client metadata lives. It's the client_id, so it must stay exactly this address.
CLIENT_ID_URL = "https://reelay.app/oauth/bluesky-client-metadata.json"
REDIRECT_PATH = "/callback"
REGISTERED_REDIRECT_URI = "http://127.0.0.1/callback"          # no port: any loopback port matches
SCOPES = ("atproto", "transition:generic")
SCOPE = " ".join(SCOPES)
ENTRYWAY = "https://bsky.social"                               # where sign-in starts when no handle is typed
PLC_DIRECTORY = "https://plc.directory"
REFRESH_MARGIN = 60                                            # refresh this many seconds before expiry
HTTP_TIMEOUT = 20

EXPIRED = "Your Bluesky sign-in has expired. Sign in with Bluesky again in Accounts."


def client_metadata(client_id: str = CLIENT_ID_URL) -> dict:
    """The client metadata document the website hosts at `client_id` (tests compare the two)."""
    origin = _origin(client_id)
    return {
        "client_id": client_id,
        "client_name": "Reelay",
        "client_uri": origin,
        "tos_uri": f"{origin}/terms",
        "policy_uri": f"{origin}/privacy",
        "application_type": "native",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "redirect_uris": [REGISTERED_REDIRECT_URI],
        "scope": SCOPE,
        "token_endpoint_auth_method": "none",
        "dpop_bound_access_tokens": True,
    }


# ====================================================================== small helpers
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _json(resp) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _https_origin(url: str) -> bool:
    parts = urllib.parse.urlsplit(url or "")
    return parts.scheme == "https" and bool(parts.hostname) and parts.path in ("", "/") \
        and not parts.query and not parts.fragment and not parts.username


# ====================================================================== DPoP (RFC 9449)
class DpopKey:
    """An ES256 (P-256) key that signs DPoP proofs. One per sign-in; tokens are bound to it."""

    def __init__(self, key: ECC.EccKey | None = None):
        self.key = key or ECC.generate(curve="P-256")

    @property
    def public_jwk(self) -> dict:
        q = self.key.pointQ
        return {"kty": "EC", "crv": "P-256", "x": _b64(int(q.x).to_bytes(32, "big")),
                "y": _b64(int(q.y).to_bytes(32, "big"))}

    def to_jwk(self) -> dict:
        """The private key as a JWK, for the Keychain only."""
        return {**self.public_jwk, "d": _b64(int(self.key.d).to_bytes(32, "big"))}

    @classmethod
    def from_jwk(cls, jwk: dict | str) -> "DpopKey":
        data = json.loads(jwk) if isinstance(jwk, str) else jwk
        if data.get("kty") != "EC" or data.get("crv") != "P-256" or not data.get("d"):
            raise SignInError("The saved Bluesky sign-in key is unreadable. Sign in with Bluesky again.")
        return cls(ECC.construct(curve="P-256", d=int.from_bytes(_unb64(data["d"]), "big")))

    def proof(self, method: str, url: str, nonce: str | None = None, access_token: str | None = None) -> str:
        parts = urllib.parse.urlsplit(url)
        header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": self.public_jwk}
        claims = {"jti": secrets.token_urlsafe(16), "htm": method.upper(),
                  "htu": urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")),
                  "iat": int(time.time())}
        if nonce:
            claims["nonce"] = nonce
        if access_token:
            claims["ath"] = _b64(hashlib.sha256(access_token.encode()).digest())
        signing_input = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}." \
                        f"{_b64(json.dumps(claims, separators=(',', ':')).encode())}"
        signature = DSS.new(self.key, "fips-186-3").sign(SHA256.new(signing_input.encode()))   # raw r||s
        return f"{signing_input}.{_b64(signature)}"


def _nonce_error(resp) -> bool:
    if resp.status_code == 400:
        return _json(resp).get("error") == "use_dpop_nonce"
    if resp.status_code == 401:
        return "use_dpop_nonce" in (resp.headers.get("WWW-Authenticate") or "")
    return False


def dpop_request(http, key: DpopKey, nonces: dict, method: str, url: str, access_token: str | None = None,
                 **kwargs):
    """One request with a DPoP proof, retried once if the server asks for a (new) nonce."""
    origin = _origin(url)
    headers = dict(kwargs.pop("headers", None) or {})
    if access_token:
        headers["Authorization"] = f"DPoP {access_token}"
    kwargs.setdefault("timeout", HTTP_TIMEOUT)
    resp = None
    for _attempt in range(2):
        headers["DPoP"] = key.proof(method, url, nonces.get(origin), access_token)
        resp = http.request(method, url, headers=headers, **kwargs)
        fresh = resp.headers.get("DPoP-Nonce")
        retry = _nonce_error(resp) and fresh and fresh != nonces.get(origin)
        if fresh:
            nonces[origin] = fresh
        if not retry:
            break
    return resp


# ====================================================================== who is this, and where do they live
def resolve_handle(handle: str, http=requests) -> str:
    """handle → DID. The handle's own well-known file first, then Bluesky's official resolver
    (it covers handles set with a DNS TXT record)."""
    try:
        resp = http.get(f"https://{handle}/.well-known/atproto-did", timeout=HTTP_TIMEOUT)
        did = resp.text.strip() if resp.status_code == 200 else ""
        if did.startswith("did:"):
            return did
    except requests.RequestException:
        pass
    try:
        resp = http.get(f"{ENTRYWAY}/xrpc/com.atproto.identity.resolveHandle", params={"handle": handle},
                        timeout=HTTP_TIMEOUT)
    except requests.RequestException:
        raise SignInError("Couldn't reach Bluesky. Check your internet connection and try again.") from None
    did = str(_json(resp).get("did") or "")
    if resp.status_code != 200 or not did.startswith("did:"):
        raise SignInError(f"Bluesky doesn't know the handle @{handle}. Check the spelling and try again.")
    return did


def resolve_did(did: str, http=requests) -> dict:
    """DID → DID document (did:plc from plc.directory, did:web from the site itself)."""
    if did.startswith("did:plc:"):
        url = f"{PLC_DIRECTORY}/{did}"
    elif did.startswith("did:web:"):
        host = urllib.parse.unquote(did.removeprefix("did:web:"))
        if ":" in host or "/" in host:
            raise SignInError("That Bluesky account uses an address Reelay can't look up.")
        url = f"https://{host}/.well-known/did.json"
    else:
        raise SignInError("That Bluesky account uses an address Reelay can't look up.")
    try:
        resp = http.get(url, timeout=HTTP_TIMEOUT)
    except requests.RequestException:
        raise SignInError("Couldn't reach Bluesky's account directory. Please try again.") from None
    doc = _json(resp)
    if resp.status_code != 200 or doc.get("id") != did:
        raise SignInError("Couldn't look up that Bluesky account. Please try again.")
    return doc


def pds_of(doc: dict) -> str:
    for svc in doc.get("service") or []:
        if str(svc.get("id", "")).endswith("#atproto_pds") and svc.get("type") == "AtprotoPersonalDataServer":
            endpoint = str(svc.get("serviceEndpoint") or "").rstrip("/")
            if endpoint.startswith("https://"):
                return endpoint
    raise SignInError("That Bluesky account doesn't say which server holds it.")


def handle_of(doc: dict) -> str:
    for aka in doc.get("alsoKnownAs") or []:
        if str(aka).startswith("at://"):
            return str(aka)[5:].lower()
    return ""


def auth_server_for(pds: str, http=requests) -> str:
    try:
        resp = http.get(f"{pds}/.well-known/oauth-protected-resource", timeout=HTTP_TIMEOUT)
    except requests.RequestException:
        raise SignInError("Couldn't reach your Bluesky server. Please try again.") from None
    servers = _json(resp).get("authorization_servers") or []
    if resp.status_code != 200 or len(servers) != 1 or not _https_origin(servers[0]):
        raise SignInError("Your Bluesky server doesn't offer sign-in for apps yet. Use an App Password "
                          "under Advanced Setup instead.")
    return str(servers[0]).rstrip("/")


@dataclass
class ServerInfo:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    par_endpoint: str
    revocation_endpoint: str = ""


def auth_server_metadata(issuer: str, http=requests) -> ServerInfo:
    """The authorization server's metadata, checked against what the atproto spec requires."""
    try:
        resp = http.get(f"{issuer}/.well-known/oauth-authorization-server", timeout=HTTP_TIMEOUT)
    except requests.RequestException:
        raise SignInError("Couldn't reach Bluesky's sign-in server. Please try again.") from None
    m = _json(resp)
    ok = (resp.status_code == 200 and m.get("issuer") == issuer
          and m.get("require_pushed_authorization_requests") is True
          and m.get("authorization_response_iss_parameter_supported") is True
          and "S256" in (m.get("code_challenge_methods_supported") or [])
          and "ES256" in (m.get("dpop_signing_alg_values_supported") or [])
          and "none" in (m.get("token_endpoint_auth_methods_supported") or [])
          and m.get("client_id_metadata_document_supported") is True
          and all(s in (m.get("scopes_supported") or []) for s in SCOPES)
          and all(str(m.get(k) or "").startswith("https://") for k in
                  ("authorization_endpoint", "token_endpoint", "pushed_authorization_request_endpoint")))
    if not ok:
        raise SignInError("Bluesky's sign-in server isn't set up the way Reelay expects. Use an App Password "
                          "under Advanced Setup instead.")
    revoke = str(m.get("revocation_endpoint") or "")
    return ServerInfo(issuer, m["authorization_endpoint"], m["token_endpoint"],
                      m["pushed_authorization_request_endpoint"], revoke if revoke.startswith("https://") else "")


@dataclass
class Identity:
    did: str
    handle: str
    pds: str
    issuer: str


def find_account(typed: str, http=requests) -> Identity:
    """What someone typed (a handle, with or without @, a profile link, or a DID) → where they sign in."""
    text = (typed or "").strip()
    if text.lower().startswith("did:"):
        did, handle = text, ""
    else:
        handle = clean_handle(text, "bluesky").lower()
        if "." not in handle:
            raise SignInError("Type your full Bluesky handle, for example @yourname.bsky.social.")
        did = resolve_handle(handle, http)
    doc = resolve_did(did, http)
    if handle and handle_of(doc) != handle:
        raise SignInError(f"@{handle} doesn't point back to its Bluesky account. Check the handle and try again.")
    pds = pds_of(doc)
    return Identity(did, handle or handle_of(doc), pds, auth_server_for(pds, http))


# ====================================================================== a signed-in session
@dataclass
class BlueskySession:
    did: str
    handle: str
    pds: str
    issuer: str
    token_endpoint: str
    revocation_endpoint: str
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str
    dpop_key: dict                            # private JWK
    issued_at: float = 0.0
    nonces: dict = field(default_factory=dict)  # per server origin; not saved

    def to_secret(self) -> dict:
        """What the Keychain keeps for this account."""
        data = asdict(self)
        data.pop("nonces")
        return {"auth": "oauth", "via": "reelay", **data}

    @classmethod
    def from_secret(cls, secret: dict) -> "BlueskySession":
        try:
            key = secret["dpop_key"]
            return cls(did=secret["did"], handle=secret.get("handle", ""), pds=secret["pds"],
                       issuer=secret["issuer"], token_endpoint=secret["token_endpoint"],
                       revocation_endpoint=secret.get("revocation_endpoint", ""),
                       access_token=secret["access_token"], refresh_token=secret.get("refresh_token", ""),
                       expires_at=float(secret.get("expires_at") or 0), scope=secret.get("scope", ""),
                       dpop_key=json.loads(key) if isinstance(key, str) else dict(key),
                       issued_at=float(secret.get("issued_at") or 0))
        except (KeyError, TypeError, ValueError):
            raise SignInError("The saved Bluesky sign-in is incomplete. Sign in with Bluesky again.") from None

    def __repr__(self) -> str:   # never print tokens or the key
        return f"BlueskySession(did={self.did!r}, handle={self.handle!r}, expires_at={self.expires_at!r})"

    @property
    def key(self) -> DpopKey:
        return DpopKey.from_jwk(self.dpop_key)


def is_oauth_secret(secret: dict | None) -> bool:
    return bool(secret) and secret.get("auth") == "oauth"


def _tokens_from(data: dict, ident: Identity, server: ServerInfo, key: DpopKey) -> BlueskySession:
    if not data.get("access_token") or str(data.get("token_type", "")).lower() != "dpop":
        raise SignInError("Bluesky didn't return a usable sign-in. Please try again.")
    scope = str(data.get("scope") or "")
    if not all(s in scope.split() for s in SCOPES):
        raise SignInError("Bluesky didn't give Reelay permission to post. Sign in again and allow access.")
    if data.get("sub") != ident.did:
        raise SignInError("Bluesky signed in a different account than the one Reelay looked up. Please try again.")
    now = time.time()
    expires_in = float(data.get("expires_in") or 0)
    return BlueskySession(did=ident.did, handle=ident.handle, pds=ident.pds, issuer=server.issuer,
                          token_endpoint=server.token_endpoint, revocation_endpoint=server.revocation_endpoint,
                          access_token=data["access_token"], refresh_token=data.get("refresh_token") or "",
                          expires_at=now + expires_in if expires_in else 0.0, scope=scope,
                          dpop_key=key.to_jwk(), issued_at=now)


# ====================================================================== the browser sign-in
def sign_in(handle: str = "", open_browser: Callable[[str], object] = webbrowser.open,
            cancel: threading.Event | None = None, session=None, timeout: float = 300,
            client_id: str = CLIENT_ID_URL) -> BlueskySession:
    """Run "Sign in with Bluesky" and return the signed-in session. Raises SignInError.
    Blocking: call from a worker thread."""
    http = session or requests.Session()
    if handle.strip():
        ident = find_account(handle, http)
    else:
        ident = Identity("", "", "", ENTRYWAY)
    server = auth_server_metadata(ident.issuer, http)
    key, nonces = DpopKey(), {}
    verifier, challenge = pkce_pair("base64url")
    state = secrets.token_urlsafe(32)

    from .loopback import LoopbackError, LoopbackReceiver
    receiver = LoopbackReceiver(state, path=REDIRECT_PATH, label="Bluesky sign-in")
    redirect_uri = receiver.redirect_uri
    try:
        form = {"client_id": client_id, "response_type": "code", "code_challenge": challenge,
                "code_challenge_method": "S256", "state": state, "redirect_uri": redirect_uri, "scope": SCOPE}
        if ident.did:
            form["login_hint"] = ident.handle or ident.did
        try:
            resp = dpop_request(http, key, nonces, "POST", server.par_endpoint, data=form)
        except requests.RequestException:
            raise SignInError("Couldn't reach Bluesky's sign-in server. Please try again.") from None
        request_uri = _json(resp).get("request_uri")
        if resp.status_code not in (200, 201) or not request_uri:
            raise SignInError(f"Bluesky didn't start the sign-in: {_reason(resp)}")
        open_browser(f"{server.authorization_endpoint}?"
                     f"{urllib.parse.urlencode({'client_id': client_id, 'request_uri': request_uri})}")
        try:
            params = receiver.wait(timeout, cancel)
        except LoopbackError as exc:
            raise SignInError(str(exc)) from None
    finally:
        receiver.close()

    if "error" in params:
        raise SignInError("Bluesky sign-in was cancelled." if params["error"] == "access_denied"
                          else f"Bluesky sign-in failed: {params.get('error_description') or params['error']}")
    if params.get("iss") != server.issuer:
        raise SignInError("The sign-in came back from a different server than the one Reelay opened. "
                          "Please try again.")
    if not params.get("code"):
        raise SignInError("Bluesky didn't send a sign-in code back. Please try again.")
    try:
        resp = dpop_request(http, key, nonces, "POST", server.token_endpoint, data={
            "grant_type": "authorization_code", "code": params["code"], "code_verifier": verifier,
            "redirect_uri": redirect_uri, "client_id": client_id})
    except requests.RequestException:
        raise SignInError("Couldn't reach Bluesky to finish signing in. Please try again.") from None
    data = _json(resp)
    if resp.status_code != 200:
        raise SignInError(f"Couldn't finish Bluesky sign-in: {_reason(resp)}")

    sub = str(data.get("sub") or "")
    if not ident.did:
        # Started at bsky.social without a handle: check the account really lives behind this server.
        if not sub.startswith("did:"):
            raise SignInError("Bluesky didn't say which account signed in. Please try again.")
        doc = resolve_did(sub, http)
        pds = pds_of(doc)
        if auth_server_for(pds, http) != server.issuer:
            raise SignInError("That Bluesky account belongs to a different sign-in server. Please try again.")
        handle_found = handle_of(doc)
        try:
            verified = bool(handle_found) and resolve_handle(handle_found, http) == sub
        except SignInError:
            verified = False
        ident = Identity(sub, handle_found if verified else "", pds, server.issuer)
    out = _tokens_from(data, ident, server, key)
    out.nonces = nonces
    if not out.handle:
        out.handle = out.did
    return out


def _reason(resp) -> str:
    data = _json(resp)
    return str(data.get("error_description") or data.get("error") or f"HTTP {resp.status_code}")


# ====================================================================== refresh, revoke, requests
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_LATEST: dict[str, dict] = {}     # did → newest secret this process has seen (refresh tokens are single-use)


def _lock(did: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(did, threading.Lock())


def remember(secret: dict) -> None:
    with _LOCKS_GUARD:
        old = _LATEST.get(secret.get("did", ""))
        if not old or float(secret.get("issued_at") or 0) >= float(old.get("issued_at") or 0):
            _LATEST[secret["did"]] = dict(secret)


def forget(did: str) -> None:
    with _LOCKS_GUARD:
        _LATEST.pop(did, None)


def newest(secret: dict) -> dict:
    """The newer of `secret` and what this process last refreshed for the same account."""
    with _LOCKS_GUARD:
        cached = _LATEST.get(secret.get("did", ""))
    if cached and float(cached.get("issued_at") or 0) > float(secret.get("issued_at") or 0):
        return dict(cached)
    return secret


def refresh(sess: BlueskySession, http=None, client_id: str = CLIENT_ID_URL) -> BlueskySession:
    """New tokens for a session (the refresh token rotates). Raises SignInError(EXPIRED) once Bluesky says no."""
    http = http or requests.Session()
    if not sess.refresh_token:
        raise SignInError(EXPIRED)
    key = sess.key
    resp = dpop_request(http, key, sess.nonces, "POST", sess.token_endpoint, data={
        "grant_type": "refresh_token", "refresh_token": sess.refresh_token, "client_id": client_id})
    data = _json(resp)
    if resp.status_code != 200:
        if resp.status_code in (400, 401):
            raise SignInError(EXPIRED)
        raise SignInError(f"Bluesky couldn't refresh the sign-in: {_reason(resp)}")
    ident = Identity(sess.did, sess.handle, sess.pds, sess.issuer)
    server = ServerInfo(sess.issuer, "", sess.token_endpoint, "", sess.revocation_endpoint)
    fresh = _tokens_from(data, ident, server, key)
    fresh.refresh_token = fresh.refresh_token or sess.refresh_token
    fresh.nonces = sess.nonces
    return fresh


class OAuthConnection:
    """Calls to the account's PDS with its DPoP-bound token: refreshes when needed, retries on a new nonce,
    and hands every rotated token to `save` so the Keychain always has the newest one."""

    def __init__(self, secret: dict, http=None, save: Callable[[dict], None] | None = None,
                 client_id: str = CLIENT_ID_URL):
        self.http = http or requests.Session()
        self.save = save
        self.client_id = client_id
        self.session = BlueskySession.from_secret(newest(secret))
        self._key = self.session.key

    @property
    def did(self) -> str:
        return self.session.did

    @property
    def pds(self) -> str:
        return self.session.pds.rstrip("/")

    @property
    def handle(self) -> str:
        return self.session.handle

    def secrets(self) -> tuple[str, ...]:
        return (self.session.access_token, self.session.refresh_token)

    def _refresh(self, stale_token: str) -> None:
        with _lock(self.did):
            cached = newest(self.session.to_secret())
            if cached.get("access_token") != stale_token:      # someone else in this process just refreshed
                nonces = self.session.nonces
                self.session = BlueskySession.from_secret(cached)
                self.session.nonces = nonces
            else:
                self.session = refresh(self.session, self.http, self.client_id)
                secret = self.session.to_secret()
                remember(secret)
                if self.save:
                    self.save(secret)
            self._key = self.session.key

    def request(self, method: str, url: str, **kwargs):
        """Raises SignInError when the sign-in has expired, requests.RequestException on network trouble."""
        if self.session.expires_at and self.session.expires_at - REFRESH_MARGIN < time.time():
            self._refresh(self.session.access_token)
        token = self.session.access_token
        resp = dpop_request(self.http, self._key, self.session.nonces, method, url, access_token=token, **kwargs)
        if resp.status_code == 401 and "invalid_token" in (resp.headers.get("WWW-Authenticate") or "") \
                or resp.status_code == 400 and _json(resp).get("error") in ("ExpiredToken", "InvalidToken"):
            self._refresh(token)
            resp = dpop_request(self.http, self._key, self.session.nonces, method, url,
                                access_token=self.session.access_token, **kwargs)
        return resp


def revoke(secret: dict, http=None, client_id: str = CLIENT_ID_URL) -> bool:
    """Best effort, on Disconnect: ask Bluesky to end Reelay's access. Never raises."""
    try:
        sess = BlueskySession.from_secret(newest(secret))
        forget(sess.did)
        if not sess.revocation_endpoint:
            return False
        http = http or requests.Session()
        key, ok = sess.key, True
        for token in filter(None, (sess.refresh_token, sess.access_token)):
            resp = dpop_request(http, key, sess.nonces, "POST", sess.revocation_endpoint,
                                data={"token": token, "client_id": client_id})
            ok = ok and resp.status_code == 200
        return ok
    except Exception:  # noqa: BLE001 (disconnecting must never fail because Bluesky didn't answer)
        return False
