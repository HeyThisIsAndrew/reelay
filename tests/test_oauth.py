"""One "Sign in with …" for every platform through Reelay's own apps, plus the
sign-in server (broker). Every platform and browser here is a fake."""

from __future__ import annotations

import hashlib
import threading
import urllib.parse
import urllib.request

import pytest

from reelay import oauth
from reelay.oauth import PROVIDERS, SignInError, TokenSet, authorize_url, availability, pkce_pair
from tests.conftest import FakeResponse, FakeSession


@pytest.fixture
def central(monkeypatch):
    """A build with Reelay's apps configured (env vars win over the built-in defaults)."""
    for key, val in {"GOOGLE_CLIENT_ID": "reelay-google.apps.googleusercontent.com",
                     "GOOGLE_CLIENT_SECRET": "desktop-secret", "TIKTOK_CLIENT_ID": "tt-key",
                     "INSTAGRAM_CLIENT_ID": "ig-app", "THREADS_CLIENT_ID": "th-app",
                     "FACEBOOK_CLIENT_ID": "fb-app", "AUTH_BROKER_URL": "https://auth.reelay.test/"}.items():
        monkeypatch.setenv(f"REELAY_{key}", val)


def browser_that_approves(extra: dict | None = None):
    """Stands in for the user: opens the sign-in URL and 'approves' by hitting the loopback redirect."""
    seen = {}

    def open_browser(url: str):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        seen.update(q, url=url)
        params = urllib.parse.urlencode({"code": "the-code", "state": q["state"], **(extra or {})})
        threading.Thread(target=lambda: urllib.request.urlopen(f"{q['redirect_uri']}?{params}", timeout=5).read(),
                         daemon=True).start()
    return open_browser, seen


def test_nothing_is_available_until_reelay_registers_its_apps(monkeypatch):
    for key in ("GOOGLE_CLIENT_ID", "TIKTOK_CLIENT_ID", "AUTH_BROKER_URL"):
        monkeypatch.delenv(f"REELAY_{key}", raising=False)
    ok, why = availability(PROVIDERS["tiktok"])
    assert not ok and "approved" in why
    assert not availability(PROVIDERS["bluesky"])[0] and "App Password" in availability(PROVIDERS["bluesky"])[1]
    with pytest.raises(SignInError):
        oauth.sign_in("tiktok", open_browser=lambda u: None)


def test_tiktok_uses_hex_pkce_client_key_and_comma_scopes(central):
    verifier, challenge = pkce_pair("hex")
    assert challenge == hashlib.sha256(verifier.encode()).hexdigest() and 43 <= len(verifier) <= 128
    url = authorize_url(PROVIDERS["tiktok"], "tt-key", "http://127.0.0.1:5555/callback/", "s", challenge)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert url.startswith("https://www.tiktok.com/v2/auth/authorize/")
    assert q["client_key"] == "tt-key" and q["scope"] == "user.info.basic,video.list,user.info.stats"
    assert q["code_challenge_method"] == "S256" and q["code_challenge"] == challenge


def test_google_signs_in_on_the_device(central):
    session = FakeSession()
    session.on("POST", "oauth2.googleapis.com/token",
               FakeResponse(200, {"access_token": "A", "refresh_token": "R", "expires_in": 3600, "scope": "s"}))
    open_browser, seen = browser_that_approves()
    tokens = oauth.sign_in("google", open_browser=open_browser, session=session)
    assert (tokens.access_token, tokens.refresh_token) == ("A", "R")
    assert "yt-analytics.readonly" in seen["scope"] and seen["redirect_uri"].startswith("http://127.0.0.1:")
    sent = session.calls[0]["data"]
    assert sent["client_secret"] == "desktop-secret" and sent["code_verifier"] and sent["code"] == "the-code"
    assert "A" not in repr(tokens)                                    # tokens never printed


def test_tiktok_exchanges_through_the_broker(central):
    session = FakeSession()
    session.on("POST", "auth.reelay.test/v1/oauth/tiktok/token",
               FakeResponse(200, {"access_token": "T", "refresh_token": "TR", "expires_in": 86400,
                                  "open_id": "user-1", "scope": "user.info.basic"}))
    open_browser, seen = browser_that_approves()
    tokens = oauth.sign_in("tiktok", open_browser=open_browser, session=session)
    body = session.calls[0]["json"]
    assert seen["redirect_uri"].endswith("/callback/") and body["redirect_uri"] == seen["redirect_uri"]
    assert body["code_verifier"] and "client_secret" not in body      # the secret stays on the server
    assert tokens.extra["open_id"] == "user-1"


def test_meta_redirects_to_the_broker_and_the_app_picks_it_up(central):
    session = FakeSession()
    session.on("GET", "/v1/oauth/session/", FakeResponse(202, {"status": "waiting"}))
    session.on("GET", "/v1/oauth/session/", FakeResponse(200, {"access_token": "IG", "expires_in": 5184000,
                                                               "user_id": "17841"}))
    opened = []
    tokens = oauth.sign_in("instagram", open_browser=opened.append, session=session, poll_interval=0)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(opened[0]).query))
    assert opened[0].startswith("https://www.instagram.com/oauth/authorize")
    assert q["redirect_uri"] == "https://auth.reelay.test/v1/oauth/instagram/callback"
    assert "instagram_business_manage_insights" in q["scope"]
    assert session.calls[0]["url"].endswith(q["state"]) and tokens.extra["user_id"] == "17841"


def test_cancel_stops_a_waiting_sign_in(central):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(SignInError, match="cancelled"):
        oauth.sign_in("threads", open_browser=lambda u: None, cancel=cancel, session=FakeSession(), poll_interval=0)


def test_identity_lookups():
    s = FakeSession()
    s.on("GET", "open.tiktokapis.com/v2/user/info", FakeResponse(200, {"data": {"user": {
        "open_id": "o1", "display_name": "Lydia", "username": "livingood.lifestyle"}}}))
    s.on("GET", "graph.facebook.com", FakeResponse(200, {"data": [
        {"id": "p1", "name": "Livin'Good", "access_token": "PT"}, {"id": "p2", "name": "No token"}]}))
    [tt] = oauth.identity("tiktok", TokenSet("T"), s)
    assert (tt["external_id"], tt["handle"]) == ("o1", "@livingood.lifestyle")
    [page] = oauth.identity("facebook", TokenSet("U"), s)               # Pages without a token are skipped
    assert page["secret"] == {"page_id": "p1", "page_access_token": "PT"}


# ------------------------------------------------------------------ the sign-in server (broker)
class _Client:
    """Talks to the broker over real local HTTP."""

    def __init__(self, url):
        import requests
        self.url, self.http = url, requests

    def get(self, path, **kw):
        return self.http.get(self.url + path, timeout=10, **kw)

    def post(self, path, **kw):
        return self.http.post(self.url + path, timeout=10, **kw)


@pytest.fixture
def broker():
    from reelay.api import ApiServer
    from server.auth_broker import create_broker
    servers = []

    def start(upstream: FakeSession):
        config = {"public_url": "https://auth.reelay.test", "tiktok": {"id": "tt-key", "secret": "tt-secret"},
                  "instagram": {"id": "ig", "secret": "ig-secret"}, "threads": {}, "facebook": {}}
        server = ApiServer(create_broker(config, session=upstream), host="127.0.0.1", port=0)
        assert server.start()
        servers.append(server)
        return _Client(server.url)
    yield start
    for server in servers:
        server.stop()


def test_broker_exchanges_tiktok_codes_with_the_secret(broker):
    up = FakeSession()
    up.on("POST", "open.tiktokapis.com/v2/oauth/token/", FakeResponse(200, {"access_token": "T", "open_id": "o"}))
    r = broker(up).post("/v1/oauth/tiktok/token", json={"code": "c", "code_verifier": "v",
                                                        "redirect_uri": "http://127.0.0.1:9/callback/"})
    assert r.status_code == 200 and r.json()["access_token"] == "T"
    sent = up.calls[0]["data"]
    assert sent["client_secret"] == "tt-secret" and sent["code_verifier"] == "v"


def test_broker_holds_meta_results_once(broker):
    up = FakeSession()
    up.on("POST", "api.instagram.com/oauth/access_token", FakeResponse(200, {"access_token": "short", "user_id": 5}))
    up.on("GET", "graph.instagram.com/access_token", FakeResponse(200, {"access_token": "long", "expires_in": 9}))
    client = broker(up)
    assert client.get("/v1/oauth/session/abc").status_code == 202      # still waiting
    page = client.get("/v1/oauth/instagram/callback", params={"code": "c#_", "state": "abc"})
    assert page.status_code == 200 and "connected" in page.text
    assert up.calls[0]["data"]["code"] == "c"                           # Instagram's trailing #_ is stripped
    first = client.get("/v1/oauth/session/abc")
    assert first.json() == {"access_token": "long", "expires_in": 9, "user_id": 5}
    assert client.get("/v1/oauth/session/abc").status_code == 202      # handed out once, then forgotten
    assert client.post("/v1/oauth/threads/token", json={"code": "c", "redirect_uri": "x"}).status_code == 404


# ------------------------------------------------------------------ desktop
def test_central_google_is_used_when_no_own_project(central, tmp_path):
    from reelay.accounts import AccountService
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    from reelay.youtube import OAuthClient
    store = Store(tmp_path / "s.db")
    svc = AccountService(store, MemoryTokenStore())
    assert svc.google_client_source() == "reelay" and svc.oauth_client().client_id.startswith("reelay-google")
    svc.set_oauth_client(OAuthClient("mine.apps.googleusercontent.com", "s"))      # your own project wins
    assert svc.google_client_source() == "own" and svc.oauth_client().client_id.startswith("mine")
    store.close()


def test_accounts_page_offers_sign_in_buttons(central, qtbot, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from reelay.gui.app import create_window
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    store = Store(db_path())
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    page = window.accounts_page
    window.go("accounts")
    assert page.connected_box.isVisibleTo(page) and not page.setup_box.isVisibleTo(page)   # no setup needed
    assert page.creds_label.text() == "Using Reelay's Google sign-in"
    tiktok = page.platform_cards["tiktok"]
    assert tiktok.signin.isVisibleTo(tiktok) and tiktok.signin.text() == "Sign in with TikTok"
    threads = page.platform_cards["threads"]
    assert threads.signin.available and threads.connect_btn.text() == "Use Your Own App Instead…"
    found = [{"external_id": "o1", "display_name": "Lydia", "handle": "@lydia", "secret": {}}]
    tiktok.signin._finish(TokenSet("T", "R"), found)
    [acc] = store.list_accounts(window.profile.id, "tiktok")
    assert acc.display_name == "Lydia" and window.accounts.has_credentials(acc)
    window.close()
    store.close()
