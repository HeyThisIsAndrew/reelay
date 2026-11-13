"""Sign in with Bluesky (AT Protocol OAuth) against a fake Bluesky: the handle lookup, PAR, the browser sign-in
on the loopback, the DPoP-bound token exchange with nonces, refresh, revoke, and posting a video with the
signed-in session. No real account or server is ever contacted: every request goes to FakeBluesky, which
checks each DPoP proof the way a real server does."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from Cryptodome.Hash import SHA256
from Cryptodome.PublicKey import ECC
from Cryptodome.Signature import DSS

from reelay import bluesky_oauth, oauth
from reelay.bluesky_oauth import CLIENT_ID_URL, DpopKey, client_metadata
from reelay.oauth import PROVIDERS, SignInError, availability
from reelay.publishers import BlueskyPublisher
from reelay.publishers.base import PermanentError
from tests.conftest import FakeResponse

HANDLE = "heythisisandrew.bsky.social"
DID = "did:plc:heythisisandrew0000000000"
PDS = "https://amanita.us-east.host.bsky.network"
ISSUER = "https://bsky.social"
BLOB = {"$type": "blob", "ref": {"$link": "bafkrei"}, "mimeType": "video/mp4", "size": 4096}

SITE_FILE = Path(os.environ.get("REELAY_SITE_DIR", Path(__file__).resolve().parents[2] / "hey-this-is-andrew"
                                / "sites" / "reelay")) / "public" / "oauth" / "bluesky-client-metadata.json"


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def decode_proof(proof: str) -> tuple[dict, dict]:
    """Check a DPoP proof's signature against the key in its own header (as servers do) → (header, claims)."""
    h, c, sig = proof.split(".")
    header, claims = json.loads(_unb64(h)), json.loads(_unb64(c))
    jwk = header["jwk"]
    assert "d" not in jwk, "a DPoP proof must never carry the private key"
    key = ECC.construct(curve="P-256", point_x=int.from_bytes(_unb64(jwk["x"]), "big"),
                        point_y=int.from_bytes(_unb64(jwk["y"]), "big"))
    DSS.new(key, "fips-186-3").verify(SHA256.new(f"{h}.{c}".encode()), _unb64(sig))   # raises if forged
    return header, claims


def thumbprint(jwk: dict) -> str:
    return hashlib.sha256(json.dumps({k: jwk[k] for k in ("crv", "kty", "x", "y")}, separators=(",", ":"),
                                     sort_keys=True).encode()).hexdigest()


class FakeBluesky:
    """bsky.social (entryway + authorization server), plc.directory, the account's PDS and video.bsky.app."""

    def __init__(self, handle_points_back=True, sub=DID, scope="atproto transition:generic", iss=ISSUER,
                 expires_in=900):
        self.calls: list[dict] = []
        self.handle_points_back, self.sub, self.scope, self.iss, self.expires_in = \
            handle_points_back, sub, scope, iss, expires_in
        self.as_nonce, self.pds_nonce = "as-nonce-1", "pds-nonce-1"
        self.par: dict = {}
        self.codes: dict[str, dict] = {}
        self.access: dict[str, str] = {}          # access token → key thumbprint
        self.refresh_tokens: dict[str, str] = {}  # refresh token → key thumbprint (single use)
        self.revoked: list[str] = []
        self.issued = 0
        self.expire_next_access = False
        self.lock = threading.Lock()

    # ------------------------------------------------------------ requests.Session surface
    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def request(self, method, url, headers=None, params=None, data=None, json=None, timeout=None):
        headers = headers or {}
        with self.lock:
            self.calls.append({"method": method, "url": url, "headers": dict(headers), "params": params,
                               "data": data if isinstance(data, dict) else None, "json": json})
            return self._route(method, url, headers, params or {}, data, json)

    # ------------------------------------------------------------ DPoP checks
    def _proof(self, method, url, headers, nonce, access_token=None):
        header, claims = decode_proof(headers["DPoP"])
        assert header["typ"] == "dpop+jwt" and header["alg"] == "ES256"
        assert claims["htm"] == method and claims["htu"] == url.split("?")[0] and claims["jti"]
        assert abs(claims["iat"] - time.time()) < 60
        if access_token:
            assert claims["ath"] == base64.urlsafe_b64encode(
                hashlib.sha256(access_token.encode()).digest()).rstrip(b"=").decode()
        return thumbprint(header["jwk"]), claims.get("nonce") == nonce

    def _as_nonce_needed(self):
        return FakeResponse(400, {"error": "use_dpop_nonce", "error_description": "nonce"},
                            headers={"DPoP-Nonce": self.as_nonce})

    def _tokens(self, jkt):
        self.issued += 1
        access, refresh = f"at-{self.issued}", f"rt-{self.issued}"
        self.access[access] = jkt
        self.refresh_tokens[refresh] = jkt
        return FakeResponse(200, {"access_token": access, "token_type": "DPoP", "refresh_token": refresh,
                                  "expires_in": self.expires_in, "scope": self.scope, "sub": self.sub},
                            headers={"DPoP-Nonce": self.as_nonce})

    # ------------------------------------------------------------ routes
    def _route(self, method, url, headers, params, data, body):
        if url == f"https://{HANDLE}/.well-known/atproto-did":
            return FakeResponse(200, text=DID + "\n")
        if url == f"https://plc.directory/{DID}":
            aka = [f"at://{HANDLE}"] if self.handle_points_back else ["at://someone-else.bsky.social"]
            return FakeResponse(200, {"id": DID, "alsoKnownAs": aka, "service": [
                {"id": "#atproto_pds", "type": "AtprotoPersonalDataServer", "serviceEndpoint": PDS}]})
        if url == f"{PDS}/.well-known/oauth-protected-resource":
            return FakeResponse(200, {"resource": PDS, "authorization_servers": [ISSUER]})
        if url == f"{ISSUER}/.well-known/oauth-authorization-server":
            return FakeResponse(200, {
                "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/oauth/authorize",
                "token_endpoint": f"{ISSUER}/oauth/token", "revocation_endpoint": f"{ISSUER}/oauth/revoke",
                "pushed_authorization_request_endpoint": f"{ISSUER}/oauth/par",
                "require_pushed_authorization_requests": True, "authorization_response_iss_parameter_supported": True,
                "code_challenge_methods_supported": ["S256"], "dpop_signing_alg_values_supported": ["ES256"],
                "token_endpoint_auth_methods_supported": ["none", "private_key_jwt"],
                "client_id_metadata_document_supported": True,
                "scopes_supported": ["atproto", "transition:generic", "transition:chat.bsky", "transition:email"]})
        if url == f"{ISSUER}/oauth/par":
            jkt, fresh = self._proof(method, url, headers, self.as_nonce)
            if not fresh:
                return self._as_nonce_needed()
            assert data["client_id"] == CLIENT_ID_URL and data["response_type"] == "code"
            assert data["code_challenge_method"] == "S256" and data["scope"] == "atproto transition:generic"
            assert data["redirect_uri"].startswith("http://127.0.0.1:") and data["redirect_uri"].endswith("/callback")
            self.par = {**data, "jkt": jkt}
            return FakeResponse(201, {"request_uri": "urn:ietf:params:oauth:request_uri:req-1", "expires_in": 299},
                                headers={"DPoP-Nonce": self.as_nonce})
        if url == f"{ISSUER}/oauth/token":
            jkt, fresh = self._proof(method, url, headers, self.as_nonce)
            if not fresh:
                return self._as_nonce_needed()
            assert data["client_id"] == CLIENT_ID_URL
            if data["grant_type"] == "authorization_code":
                grant = self.codes.pop(data["code"])
                challenge = base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest()
                                                     ).rstrip(b"=").decode()
                assert challenge == grant["code_challenge"], "PKCE verifier doesn't match"
                assert data["redirect_uri"] == grant["redirect_uri"], "redirect_uri must match the sign-in exactly"
                assert jkt == grant["jkt"], "the token request must use the PAR's DPoP key"
                return self._tokens(jkt)
            if data["grant_type"] == "refresh_token":
                if self.refresh_tokens.pop(data["refresh_token"], None) != jkt:
                    return FakeResponse(400, {"error": "invalid_grant"}, headers={"DPoP-Nonce": self.as_nonce})
                return self._tokens(jkt)
        if url == f"{ISSUER}/oauth/revoke":
            jkt, fresh = self._proof(method, url, headers, self.as_nonce)
            if not fresh:
                return self._as_nonce_needed()
            self.revoked.append(data["token"])
            return FakeResponse(200, {}, headers={"DPoP-Nonce": self.as_nonce})
        if url.startswith(f"{PDS}/xrpc/"):
            return self._pds(method, url, headers, params, body)
        if url == "https://video.bsky.app/xrpc/app.bsky.video.uploadVideo":
            assert headers["Authorization"] == "Bearer svc-token"
            return FakeResponse(200, {"jobStatus": {"jobId": "job-1", "did": DID, "state": "JOB_STATE_CREATED"}})
        if url == "https://video.bsky.app/xrpc/app.bsky.video.getJobStatus":
            assert "Authorization" not in headers, "the PDS-bound OAuth token must not go to the video service"
            return FakeResponse(200, {"jobStatus": {"jobId": "job-1", "state": "JOB_STATE_COMPLETED", "blob": BLOB}})
        raise AssertionError(f"unexpected request: {method} {url}")

    def _pds(self, method, url, headers, params, body):
        scheme, _, token = headers.get("Authorization", "").partition(" ")
        assert scheme == "DPoP", "OAuth calls to the PDS use the DPoP scheme"
        jkt, fresh = self._proof(method, url, headers, self.pds_nonce, token)
        if not fresh:
            return FakeResponse(401, {"error": "use_dpop_nonce"}, headers={
                "WWW-Authenticate": 'DPoP error="use_dpop_nonce"', "DPoP-Nonce": self.pds_nonce})
        if self.expire_next_access or self.access.get(token) != jkt:
            self.expire_next_access = False
            return FakeResponse(401, {"error": "invalid_token"}, headers={
                "WWW-Authenticate": 'DPoP error="invalid_token", error_description="expired"',
                "DPoP-Nonce": self.pds_nonce})
        name = url.rsplit("/", 1)[1]
        if name == "com.atproto.server.getSession":
            return FakeResponse(200, {"did": DID, "handle": HANDLE}, headers={"DPoP-Nonce": self.pds_nonce})
        if name == "com.atproto.server.getServiceAuth":
            assert params["aud"] == "did:web:amanita.us-east.host.bsky.network"
            return FakeResponse(200, {"token": "svc-token"}, headers={"DPoP-Nonce": self.pds_nonce})
        if name == "com.atproto.repo.getRecord":
            return FakeResponse(400, {"error": "RecordNotFound"}, headers={"DPoP-Nonce": self.pds_nonce})
        if name == "com.atproto.repo.createRecord":
            assert body["repo"] == DID and body["record"]["embed"]["video"] == BLOB
            return FakeResponse(200, {"uri": f"at://{DID}/app.bsky.feed.post/{body['rkey']}", "cid": "c"},
                                headers={"DPoP-Nonce": self.pds_nonce})
        raise AssertionError(f"unexpected PDS call {name}")

    # ------------------------------------------------------------ the person in the browser
    def browser(self, approve=True, iss=None, state=None):
        """Opens the authorization page and approves (or denies), redirecting to the app's loopback."""
        seen = {}

        def open_browser(url: str):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            seen.update(q, url=url)
            assert url.startswith(f"{ISSUER}/oauth/authorize?") and set(q) == {"client_id", "request_uri"}
            redirect = self.par["redirect_uri"]
            if approve:
                code = f"code-{len(self.codes) + 1}"
                self.codes[code] = self.par
                back = {"code": code, "state": state or self.par["state"], "iss": iss or self.iss}
            else:
                back = {"error": "access_denied", "state": self.par["state"], "iss": self.iss}
            target = f"{redirect}?{urllib.parse.urlencode(back)}"
            threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=5).read(), daemon=True).start()
        return open_browser, seen


@pytest.fixture
def bluesky_on(monkeypatch):
    """A build with Sign in with Bluesky switched on (the metadata file is live)."""
    monkeypatch.setenv("REELAY_BLUESKY_CLIENT_ID", CLIENT_ID_URL)
    with bluesky_oauth._LOCKS_GUARD:
        bluesky_oauth._LATEST.clear()


def signed_in_secret(world: FakeBluesky, handle: str = "@" + HANDLE) -> dict:
    """What the Keychain holds after Sign in with Bluesky."""
    open_browser, _ = world.browser()
    tokens = oauth.sign_in("bluesky", open_browser=open_browser, session=world, timeout=10, login_hint=handle)
    [found] = oauth.identity("bluesky", tokens)
    return {"access_token": tokens.access_token, "refresh_token": tokens.refresh_token,
            "expires_at": tokens.expires_at, "via": "reelay", **found["secret"]}


# ====================================================================== the client metadata
def test_client_metadata_follows_the_native_client_rules():
    meta = client_metadata()
    assert meta["client_id"] == CLIENT_ID_URL and CLIENT_ID_URL.startswith("https://")
    assert urllib.parse.urlsplit(CLIENT_ID_URL).port is None
    assert meta["client_uri"] == "https://reelay.app"          # same origin as the client_id
    assert meta["application_type"] == "native" and meta["token_endpoint_auth_method"] == "none"
    assert meta["dpop_bound_access_tokens"] is True
    assert meta["redirect_uris"] == ["http://127.0.0.1/callback"]     # loopback IP, no port, never "localhost"
    assert set(meta["grant_types"]) == {"authorization_code", "refresh_token"} and meta["response_types"] == ["code"]
    assert meta["scope"].split() == ["atproto", "transition:generic"] == list(PROVIDERS["bluesky"].scopes)
    assert "jwks" not in meta and "jwks_uri" not in meta                  # a public client has no keys


@pytest.mark.skipif(not SITE_FILE.exists(), reason="no website checkout with the metadata file next to this one")
def test_the_website_hosts_exactly_this_metadata():
    assert json.loads(SITE_FILE.read_text()) == client_metadata()


def test_sign_in_with_bluesky_stays_off_until_the_metadata_is_live(monkeypatch):
    monkeypatch.delenv("REELAY_BLUESKY_CLIENT_ID", raising=False)
    ok, why = availability(PROVIDERS["bluesky"])
    assert not ok and "App Password" in why and "Advanced Setup" in why
    monkeypatch.setenv("REELAY_BLUESKY_CLIENT_ID", CLIENT_ID_URL)
    assert availability(PROVIDERS["bluesky"]) == (True, "")             # no broker, no secret needed


# ====================================================================== DPoP
def test_dpop_proofs_are_signed_es256_jwts_bound_to_one_key():
    key = DpopKey()
    proof = key.proof("post", "https://pds.example/xrpc/x?a=1#f", nonce="n1", access_token="tok")
    header, claims = decode_proof(proof)
    assert header["typ"] == "dpop+jwt" and header["alg"] == "ES256" and header["jwk"]["crv"] == "P-256"
    assert claims["htm"] == "POST" and claims["htu"] == "https://pds.example/xrpc/x" and claims["nonce"] == "n1"
    assert claims["ath"] == base64.urlsafe_b64encode(hashlib.sha256(b"tok").digest()).rstrip(b"=").decode()
    assert decode_proof(key.proof("GET", "https://x"))[1]["jti"] != claims["jti"]   # a fresh proof every time
    again = DpopKey.from_jwk(json.dumps(key.to_jwk()))                    # what the Keychain keeps
    assert again.public_jwk == key.public_jwk
    with pytest.raises(SignInError):
        DpopKey.from_jwk({"kty": "EC", "crv": "P-256"})


# ====================================================================== signing in
def test_sign_in_with_a_handle_typed_with_an_at(bluesky_on, tmp_path):
    from reelay.accounts import AccountService
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    world = FakeBluesky()
    open_browser, seen = world.browser()
    tokens = oauth.sign_in("bluesky", open_browser=open_browser, session=world, timeout=10,
                           login_hint=" @HeyThisIsAndrew.bsky.social ")
    assert seen["client_id"] == CLIENT_ID_URL and seen["request_uri"].startswith("urn:ietf:params:oauth:request_uri:")
    assert world.par["login_hint"] == HANDLE
    pars = [c for c in world.calls if c["url"].endswith("/oauth/par")]
    assert len(pars) == 2                                    # first without a nonce, retried with the server's
    token_call = next(c for c in world.calls if c["url"].endswith("/oauth/token"))
    assert token_call["data"]["redirect_uri"] == world.par["redirect_uri"]
    assert tokens.access_token == "at-1" and tokens.refresh_token == "rt-1"
    assert "at-1" not in repr(tokens)

    [found] = oauth.identity("bluesky", tokens)
    assert found["external_id"] == DID and found["handle"] == "@" + HANDLE and found["display_name"] == HANDLE
    store = Store(tmp_path / "s.db")
    svc = AccountService(store, MemoryTokenStore())
    profile = store.create_profile("Hey, This Is Andrew", tmp_path / "lib")
    account = svc.connect_signed_in(profile.id, "bluesky", tokens, found)
    secret = svc.credentials(account)
    assert account.token_key == f"bluesky:{DID}" and secret["auth"] == "oauth"
    assert secret["pds"] == PDS and secret["issuer"] == ISSUER and secret["dpop_key"]["d"]
    assert "app_password" not in secret
    store.close()


def test_sign_in_without_a_handle_starts_at_bsky_social_and_checks_the_account(bluesky_on):
    world = FakeBluesky()
    open_browser, _ = world.browser()
    tokens = oauth.sign_in("bluesky", open_browser=open_browser, session=world, timeout=10)
    assert "login_hint" not in world.par
    assert tokens.extra["did"] == DID and tokens.extra["handle"] == HANDLE and tokens.extra["pds"] == PDS
    urls = [c["url"] for c in world.calls]
    token_at = urls.index(f"{ISSUER}/oauth/token")
    assert f"https://plc.directory/{DID}" in urls[token_at:]   # the account was looked up after Bluesky named it


@pytest.mark.parametrize("world, browser_kw, message", [
    (FakeBluesky(iss="https://evil.example"), {}, "different server"),
    (FakeBluesky(sub="did:plc:someoneelse"), {}, "different account"),
    (FakeBluesky(scope="atproto"), {}, "permission to post"),
    (FakeBluesky(), {"approve": False}, "cancelled"),
])
def test_sign_in_refuses_anything_that_doesnt_add_up(bluesky_on, world, browser_kw, message):
    open_browser, _ = world.browser(**browser_kw)
    with pytest.raises(SignInError, match=message):
        oauth.sign_in("bluesky", open_browser=open_browser, session=world, timeout=10, login_hint=HANDLE)


def test_a_handle_its_account_doesnt_claim_is_refused(bluesky_on):
    world = FakeBluesky(handle_points_back=False)
    with pytest.raises(SignInError, match="doesn't point back"):
        oauth.sign_in("bluesky", open_browser=lambda _u: None, session=world, timeout=10, login_hint=HANDLE)
    assert not any("/oauth/par" in c["url"] for c in world.calls)        # nothing was sent to sign in


def test_an_auth_server_missing_required_features_is_refused(bluesky_on):
    world = FakeBluesky()
    real = world._route

    def route(method, url, headers, params, data, body):
        resp = real(method, url, headers, params, data, body)
        if url.endswith("/.well-known/oauth-authorization-server"):
            resp._json = {**resp._json, "require_pushed_authorization_requests": False}
        return resp
    world._route = route
    with pytest.raises(SignInError, match="App Password"):
        oauth.sign_in("bluesky", open_browser=lambda _u: None, session=world, timeout=10, login_hint=HANDLE)


# ====================================================================== posting with the sign-in
def make_job(tmp_path, credentials, saved_secrets):
    from tests.test_publishers import make_job as base_job
    job, _ = base_job(tmp_path, "bluesky", credentials)
    job.save_credentials = saved_secrets.append
    return job


def test_publisher_posts_a_video_with_the_oauth_sign_in(bluesky_on, tmp_path):
    world = FakeBluesky()
    secret = signed_in_secret(world)
    saved: list[dict] = []
    world.calls.clear()
    result = BlueskyPublisher(world, sleep=lambda _s: None).publish(make_job(tmp_path, secret, saved))
    assert result.remote_url.startswith(f"https://bsky.app/profile/{HANDLE}/post/")
    assert result.remote_id.startswith(f"at://{DID}/app.bsky.feed.post/")
    urls = [c["url"] for c in world.calls]
    assert not any("createSession" in u for u in urls)                    # no App Password involved
    first_pds = [c for c in world.calls if c["url"].startswith(PDS)][:2]
    assert first_pds[0]["url"] == first_pds[1]["url"]                     # retried once with the PDS nonce
    assert saved == []                                                    # nothing refreshed, nothing to save
    for call in world.calls:
        assert "at-1" not in call["url"] and "rt-1" not in json.dumps(call.get("params") or {})


def test_an_expired_access_token_refreshes_and_the_new_tokens_are_saved(bluesky_on, tmp_path):
    world = FakeBluesky()
    secret = {**signed_in_secret(world), "expires_at": time.time() - 5}     # access token already expired
    saved: list[dict] = []
    BlueskyPublisher(world, sleep=lambda _s: None).publish(make_job(tmp_path, secret, saved))
    assert [s["access_token"] for s in saved] == ["at-2"] and saved[0]["refresh_token"] == "rt-2"
    assert saved[0]["dpop_key"] == secret["dpop_key"]                     # same key: tokens are bound to it
    assert "rt-1" not in world.refresh_tokens                             # the old refresh token was used up

    # Another publish in this process with the stale Keychain copy uses the new tokens, not the spent ones.
    again = tmp_path / "again"
    again.mkdir()
    saved2: list[dict] = []
    BlueskyPublisher(world, sleep=lambda _s: None).publish(make_job(again, secret, saved2))
    assert saved2 == []


def test_a_rejected_token_mid_post_refreshes_once(bluesky_on, tmp_path):
    world = FakeBluesky()
    secret = signed_in_secret(world)
    world.expire_next_access = True                                       # the PDS says invalid_token once
    saved: list[dict] = []
    BlueskyPublisher(world, sleep=lambda _s: None).publish(make_job(tmp_path, secret, saved))
    assert [s["access_token"] for s in saved] == ["at-2"]


def test_an_ended_sign_in_asks_to_sign_in_again(bluesky_on, tmp_path):
    world = FakeBluesky()
    secret = {**signed_in_secret(world), "expires_at": time.time() - 5}
    world.refresh_tokens.clear()                                          # Bluesky ended the session
    with pytest.raises(PermanentError, match="Sign in with Bluesky again") as err:
        BlueskyPublisher(world).publish(make_job(tmp_path, secret, []))
    assert "rt-1" not in str(err.value) and "at-1" not in str(err.value)


def test_check_in_accounts_works_with_the_sign_in(bluesky_on):
    world = FakeBluesky()
    secret = signed_in_secret(world)
    assert BlueskyPublisher(world).verify(secret) == {"external_id": DID, "display_name": HANDLE,
                                                      "handle": "@" + HANDLE}


def test_app_password_accounts_keep_working(bluesky_on, tmp_path):
    """Accounts connected before Sign in with Bluesky (or under Advanced Setup) still log in with createSession."""
    from tests.test_publishers import BSKY_CREDS, bluesky_session
    s = bluesky_session()
    job = make_job(tmp_path, BSKY_CREDS, [])
    BlueskyPublisher(s, sleep=lambda _s: None).publish(job)
    assert "createSession" in s.calls[0]["url"]
    assert not any("DPoP" in (c.get("headers") or {}) for c in s.calls)


# ====================================================================== disconnect
def test_disconnect_revokes_the_bluesky_sign_in(bluesky_on, tmp_path):
    world = FakeBluesky()
    secret = signed_in_secret(world)
    assert oauth.revoke_access("bluesky", secret, world) is True
    assert world.revoked == ["rt-1", "at-1"]
    assert oauth.revoke_access("bluesky", {"handle": HANDLE, "app_password": "abcd-efgh-ijkl-mnop"}, world) is False


def test_accounts_keep_an_oauth_bluesky_secret_and_disconnect_deletes_it(bluesky_on, tmp_path, monkeypatch):
    from reelay.accounts import AccountService
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    world = FakeBluesky()
    secret = signed_in_secret(world)
    revoked = threading.Event()
    monkeypatch.setattr(oauth, "revoke_access", lambda platform, s, session=None: revoked.set())
    store = Store(tmp_path / "s.db")
    svc = AccountService(store, MemoryTokenStore())
    profile = store.create_profile("Be Unconventional", tmp_path / "lib")
    account = svc.connect_platform(profile.id, "bluesky", {"external_id": DID, "display_name": HANDLE}, secret)
    assert svc.has_credentials(account)
    svc.save_credentials(account, {**secret, "access_token": "at-9"})
    assert svc.credentials(account)["access_token"] == "at-9"
    svc.disconnect(account)
    assert revoked.wait(5) and svc.tokens.get(account.token_key) is None
    with pytest.raises(ValueError):                                       # an incomplete sign-in isn't saved
        svc.connect_platform(profile.id, "bluesky", {"external_id": DID}, {"auth": "oauth", "did": DID})
    store.close()


# ====================================================================== the Accounts page
def test_accounts_page_offers_sign_in_with_bluesky(bluesky_on, qtbot):
    from PySide6.QtWidgets import QApplication

    from reelay.gui.app import create_window
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    store = Store(db_path())
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    window.go("accounts")
    card = window.accounts_page.platform_cards["bluesky"]
    assert card.signin.available and card.signin.text() == "Sign in with Bluesky"
    assert card.signin.isVisibleTo(card) and card.connect_btn.text() == "Use an App Password Instead…"
    world = FakeBluesky()
    open_browser, _ = world.browser()
    tokens = oauth.sign_in("bluesky", open_browser=open_browser, session=world, timeout=10, login_hint=HANDLE)
    card.signin._finish(tokens, oauth.identity("bluesky", tokens))
    [acc] = store.list_accounts(window.profile.id, "bluesky")
    assert acc.handle == "@" + HANDLE and window.accounts.credentials(acc)["auth"] == "oauth"
    window.close()
    store.close()
