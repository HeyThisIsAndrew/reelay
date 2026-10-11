import base64
import hashlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from reelay import youtube as yt
from tests.conftest import FakeResponse

CLIENT = yt.OAuthClient("cid.apps.googleusercontent.com", "secret")


def fresh_tokens(**kw):
    return yt.Tokens(access_token=kw.get("access", "A1"), refresh_token="R1",
                     expires_at=kw.get("expires_at", time.time() + 3600))


# ------------------------------------------------------------------ OAuth
def test_client_from_downloaded_json_formats(tmp_path):
    f = tmp_path / "client_secret.json"
    f.write_text('{"installed": {"client_id": "abc", "client_secret": "xyz"}}')
    assert yt.OAuthClient.from_json(f) == yt.OAuthClient("abc", "xyz")
    assert yt.OAuthClient.from_json({"web": {"client_id": "w", "client_secret": "s"}}).client_id == "w"
    with pytest.raises(ValueError):
        yt.OAuthClient.from_json({"nope": 1})


def test_auth_url_requests_offline_access_with_pkce():
    verifier, challenge = yt.pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    url = yt.build_auth_url(CLIENT, "http://127.0.0.1:5000/", "st", challenge)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert q["access_type"] == "offline" and q["code_challenge_method"] == "S256"
    assert q["code_challenge"] == challenge and q["state"] == "st"
    assert "youtube.upload" in q["scope"] and "youtube.readonly" in q["scope"]


def _browser(params_for):
    """Simulates the user's browser: follows the redirect with given params."""
    def open_browser(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        target = q["redirect_uri"] + "?" + urllib.parse.urlencode(params_for(q))

        def visit():
            try:
                urllib.request.urlopen(target, timeout=5).read()
            except urllib.error.HTTPError:      # a forged redirect gets an error page, like in a real browser
                pass
        threading.Thread(target=visit, daemon=True).start()
    return open_browser


def test_authorize_full_loopback_flow(fake_session):
    seen = {}

    def token_endpoint(_m, _u, kw):
        seen.update(kw["data"])
        return FakeResponse(200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3599})

    fake_session.on("POST", "oauth2.googleapis.com/token", token_endpoint)
    tokens = yt.authorize(CLIENT, open_browser=_browser(lambda q: {"code": "C0DE", "state": q["state"]}),
                          timeout=10, session=fake_session)
    assert (tokens.access_token, tokens.refresh_token) == ("AT", "RT")
    assert seen["code"] == "C0DE" and seen["grant_type"] == "authorization_code"
    assert seen["redirect_uri"].startswith("http://127.0.0.1:") and len(seen["code_verifier"]) >= 43


def test_authorize_rejects_state_mismatch_and_denial(fake_session):
    # A forged redirect is turned away and the real sign-in keeps waiting (here: until it times out),
    # so another program can't end or hijack it.
    with pytest.raises(yt.YouTubeError, match="Timed out"):
        yt.authorize(CLIENT, open_browser=_browser(lambda q: {"code": "x", "state": "forged"}),
                     timeout=1.5, session=fake_session)
    with pytest.raises(yt.YouTubeError, match="cancelled"):
        yt.authorize(CLIENT, open_browser=_browser(lambda q: {"error": "access_denied", "state": q["state"]}),
                     timeout=10, session=fake_session)


def test_authorize_can_be_cancelled():
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(yt.YouTubeError, match="cancelled"):
        yt.authorize(CLIENT, open_browser=lambda _url: None, timeout=10, cancel=cancel)


def test_missing_refresh_token_is_explained(fake_session):
    fake_session.on("POST", "token", FakeResponse(200, {"access_token": "AT", "expires_in": 10}))
    with pytest.raises(yt.YouTubeError, match="refresh token"):
        yt.exchange_code(CLIENT, "c", "v", "http://127.0.0.1:1/", fake_session)


def test_revoked_refresh_token_requires_reconnect(fake_session):
    fake_session.on("POST", "token", FakeResponse(400, {"error": "invalid_grant"}))
    with pytest.raises(yt.ReauthRequired):
        yt.refresh_tokens(CLIENT, fresh_tokens(), fake_session)


# ------------------------------------------------------------------ API client
CHANNEL = {"items": [{"id": "UC123", "snippet": {"title": "Lydia", "customUrl": "@lydia",
                                                 "thumbnails": {"default": {"url": "http://t"}}},
                      "contentDetails": {"relatedPlaylists": {"uploads": "UU123"}}}]}


def test_expired_token_is_refreshed_and_persisted(fake_session):
    saved = []
    fake_session.on("POST", "token", FakeResponse(200, {"access_token": "NEW", "expires_in": 3600}))
    fake_session.on("GET", "/channels", FakeResponse(200, CHANNEL))
    client = yt.YouTubeClient(CLIENT, fresh_tokens(expires_at=0), on_tokens=saved.append, session=fake_session)
    ch = client.my_channel()
    assert ch == {"id": "UC123", "title": "Lydia", "handle": "@lydia", "thumbnail": "http://t",
                  "uploads_playlist": "UU123"}
    assert saved[0].access_token == "NEW" and saved[0].refresh_token == "R1"
    assert fake_session.calls[-1]["headers"]["Authorization"] == "Bearer NEW"


def test_401_triggers_single_refresh_and_retry(fake_session):
    fake_session.on("GET", "/channels", FakeResponse(401, {}))
    fake_session.on("POST", "token", FakeResponse(200, {"access_token": "A2", "expires_in": 3600}))
    fake_session.on("GET", "/channels", FakeResponse(200, CHANNEL))
    assert yt.YouTubeClient(CLIENT, fresh_tokens(), session=fake_session).my_channel()["id"] == "UC123"


def test_account_without_channel(fake_session):
    fake_session.on("GET", "/channels", FakeResponse(200, {"items": []}))
    with pytest.raises(yt.YouTubeError, match="doesn't have a YouTube channel"):
        yt.YouTubeClient(CLIENT, fresh_tokens(), session=fake_session).my_channel()


def test_recent_uploads_include_schedule(fake_session):
    fake_session.on("GET", "/channels", FakeResponse(200, CHANNEL))
    fake_session.on("GET", "/playlistItems", FakeResponse(200, {"items": [{"contentDetails": {"videoId": "v1"}}]}))
    fake_session.on("GET", "/videos", FakeResponse(200, {"items": [{
        "id": "v1", "snippet": {"title": "T", "publishedAt": "2026-10-01T00:00:00Z"},
        "status": {"privacyStatus": "private", "publishAt": "2026-10-20T16:00:00Z", "uploadStatus": "processed"}}]}))
    ups = yt.YouTubeClient(CLIENT, fresh_tokens(), session=fake_session).recent_uploads()
    assert ups[0]["id"] == "v1" and ups[0]["publish_at"] == "2026-10-20T16:00:00Z"


# ------------------------------------------------------------------ uploads
def _video(tmp_path, size=600 * 1024):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"\0" * size)
    return f


def test_resumable_upload_sends_chunks_with_ranges(tmp_path, fake_session):
    f = _video(tmp_path)
    chunk = 256 * 1024
    fake_session.on("POST", "upload/youtube", FakeResponse(200, {}, {"Location": "https://up/session1"}))

    def put(_m, _u, kw):
        start, end = kw["headers"]["Content-Range"].split()[1].split("/")[0].split("-")
        if int(end) + 1 < f.stat().st_size:
            return FakeResponse(308, None, {"Range": f"bytes=0-{end}"})
        return FakeResponse(200, {"id": "NEWVID", "status": {"privacyStatus": "private"}})

    fake_session.on("PUT", "up/session1", put, repeat=True)
    progress = []
    resource = {"snippet": {"title": "t"}, "status": {"privacyStatus": "private"}}
    result = yt.YouTubeClient(CLIENT, fresh_tokens(), session=fake_session).upload(
        f, resource, on_progress=lambda s, t: progress.append(s), chunk_size=chunk)
    assert result["id"] == "NEWVID"
    post = fake_session.calls[0]
    assert post["json"] == resource and post["headers"]["X-Upload-Content-Length"] == str(600 * 1024)
    ranges = [c["headers"]["Content-Range"] for c in fake_session.calls if c["method"] == "PUT"]
    assert ranges == ["bytes 0-262143/614400", "bytes 262144-524287/614400", "bytes 524288-614399/614400"]
    assert progress[0] == 0 and progress[-1] == 600 * 1024


def test_upload_resumes_after_server_error(tmp_path, fake_session, monkeypatch):
    f = _video(tmp_path, 512 * 1024)
    fake_session.on("POST", "upload", FakeResponse(200, {}, {"Location": "https://up/s"}))
    fake_session.on("PUT", "up/s", FakeResponse(308, None, {"Range": "bytes=0-262143"}))
    fake_session.on("PUT", "up/s", FakeResponse(503, {}))
    fake_session.on("PUT", "up/s", FakeResponse(308, None, {"Range": "bytes=0-262143"}))  # status query
    fake_session.on("PUT", "up/s", FakeResponse(201, {"id": "OK"}))
    monkeypatch.setattr(threading.Event, "wait", lambda self, t=None: False)  # no real backoff sleep
    out = yt.YouTubeClient(CLIENT, fresh_tokens(), session=fake_session).upload(f, {}, chunk_size=256 * 1024)
    assert out["id"] == "OK"
    assert fake_session.calls[3]["headers"]["Content-Range"] == "bytes */524288"
    assert fake_session.calls[4]["headers"]["Content-Range"] == "bytes 262144-524287/524288"


def test_quota_error_is_friendly(tmp_path, fake_session):
    f = _video(tmp_path, 1024)
    fake_session.on("POST", "upload", FakeResponse(403, {"error": {"message": "x", "errors": [{"reason": "quotaExceeded"}]}}))
    with pytest.raises(yt.YouTubeError, match="quota"):
        yt.YouTubeClient(CLIENT, fresh_tokens(), session=fake_session).upload(f, {})


def test_upload_cancel(tmp_path, fake_session):
    f = _video(tmp_path, 1024)
    fake_session.on("POST", "upload", FakeResponse(200, {}, {"Location": "https://up/s"}))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(yt.UploadCancelled):
        yt.YouTubeClient(CLIENT, fresh_tokens(), session=fake_session).upload(f, {}, cancel=cancel)


@pytest.mark.parametrize("header,expected", [(None, 0), ("bytes=0-99", 100), ("bytes=0-262143", 262144)])
def test_next_offset(header, expected):
    assert yt._next_offset(header) == expected
