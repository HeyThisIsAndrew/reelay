"""Agent API, end to end over real HTTP (uvicorn on a random loopback port)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
import requests

from reelay.api import ApiServer, configured_address, create_app
from reelay.media import MediaLinks
from reelay.models import DispatchStatus, Platform, PostStatus, Stage
from tests.fakes import SECRETS, approve_and_run, connect_platform, disable_publishing, enable_publishing, make_world

TOKEN = SECRETS["agent"]


@pytest.fixture
def api(tmp_path, monkeypatch):
    disable_publishing(monkeypatch)  # the shipped default: only YouTube posts automatically
    world = make_world(tmp_path, monkeypatch, ready=False)   # suggested drafts only, nothing saved
    media = MediaLinks()
    world.pipeline.media = media
    server = ApiServer(create_app(world.store, world.pipeline, world.accounts, lambda: TOKEN, media),
                       host="127.0.0.1", port=0)
    assert server.start()
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {TOKEN}"

    class Api:
        base = server.url

        def get(self, path, **kw):
            return session.get(self.base + path, timeout=10, **kw)

        def patch(self, path, **kw):
            return session.patch(self.base + path, timeout=10, **kw)

        def post(self, path, **kw):
            return session.post(self.base + path, timeout=10, **kw)

    a = Api()
    a.world, a.media, a.server = world, media, server
    yield a
    server.stop()
    world.store.close()


def future(days=2):
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0)


# ------------------------------------------------------------------ auth & basics
def test_every_v1_route_needs_the_bearer_token(api):
    for method, path in (("get", "/v1/board?profile=Lydia%20Livingood"), ("get", "/v1/platforms?profile=x"),
                         ("patch", f"/v1/videos/{api.world.rows[0]}"), ("post", f"/v1/videos/{api.world.rows[0]}/schedule")):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}):
            r = requests.request(method, api.base + path, headers=headers, json={}, timeout=10)
            assert r.status_code == 401, (method, path, headers)
            assert r.json()["detail"] == "Missing or invalid bearer token." and r.json()["code"] == "unauthorized"
            assert r.headers["WWW-Authenticate"] == "Bearer"


def test_openapi_is_public_and_31(api):
    r = requests.get(api.base + "/openapi.json", timeout=10)
    assert r.status_code == 200 and r.json()["openapi"].startswith("3.1")
    assert requests.get(api.base + "/docs", timeout=10).status_code == 404  # no CDN-backed docs UI


def test_no_cors_headers(api):
    r = api.get("/v1/board?profile=Lydia%20Livingood", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_binds_loopback_by_default(monkeypatch):
    monkeypatch.delenv("REELAY_HOST", raising=False)
    monkeypatch.delenv("REELAY_PORT", raising=False)
    assert configured_address() == ("127.0.0.1", 8471)
    monkeypatch.setenv("REELAY_HOST", "0.0.0.0")
    monkeypatch.setenv("REELAY_PORT", "9000")
    monkeypatch.delenv("REELAY_ALLOW_NETWORK", raising=False)
    assert configured_address() == ("127.0.0.1", 9000)       # the network needs an explicit opt-in
    monkeypatch.setenv("REELAY_ALLOW_NETWORK", "1")
    assert configured_address() == ("0.0.0.0", 9000)
    monkeypatch.setenv("REELAY_HOST", "::1")
    monkeypatch.delenv("REELAY_ALLOW_NETWORK")
    assert configured_address() == ("::1", 9000)             # any loopback address is fine


def test_requests_must_be_addressed_to_reelay(api, monkeypatch):
    """SEC-10: a web page that points its own domain at 127.0.0.1 (DNS rebinding) sends that domain as
    Host; those requests are refused before any route runs, even with a valid token."""
    ok = requests.get(f"{api.base}/v1/board", params={"profile": "Lydia Livingood"},
                      headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10)
    assert ok.status_code == 200
    for host in ("evil.example", "evil.example:8471", "127.0.0.1.evil.example", "", "192.168.1.5:8471"):
        r = requests.get(f"{api.base}/v1/board", params={"profile": "Lydia Livingood"},
                         headers={"Authorization": f"Bearer {TOKEN}", "Host": host}, timeout=10)
        assert r.status_code == 400 and r.json()["code"] == "bad_host", host
    for host in ("localhost", "localhost:8471", "127.0.0.1:1234", "[::1]:8471"):
        r = requests.get(f"{api.base}/openapi.json", headers={"Host": host}, timeout=10)
        assert r.status_code == 200, host
    api.world.store.set_setting("public_url", "https://reelay-andrew.trycloudflare.com")   # the tunnel
    r = requests.get(f"{api.base}/openapi.json", headers={"Host": "reelay-andrew.trycloudflare.com"}, timeout=10)
    assert r.status_code == 200
    monkeypatch.setenv("REELAY_ALLOWED_HOSTS", "studio-mac.local")
    assert requests.get(f"{api.base}/openapi.json", headers={"Host": "studio-mac.local:8471"},
                        timeout=10).status_code == 200


def test_on_the_network_a_phone_can_use_this_macs_address(api):
    api.server.app.state.bind_host = "0.0.0.0"           # as with REELAY_HOST=0.0.0.0 + REELAY_ALLOW_NETWORK=1
    try:
        assert requests.get(f"{api.base}/openapi.json", headers={"Host": "192.168.1.5:8471"},
                            timeout=10).status_code == 200
        assert requests.get(f"{api.base}/openapi.json", headers={"Host": "evil.example"},
                            timeout=10).status_code == 400
    finally:
        api.server.app.state.bind_host = "127.0.0.1"


def test_media_links_also_need_the_right_host(api):
    path = api.world.folder / "video0.mp4"
    token = api.media.issue(path)
    assert requests.get(f"{api.base}/media/{token}", headers={"Host": "evil.example"}, timeout=10).status_code == 400
    assert requests.get(f"{api.base}/media/{token}", timeout=10).status_code == 200
    assert requests.post(f"{api.base}/media/{token}", timeout=10).status_code == 405        # GET/HEAD only


# ------------------------------------------------------------------ GET /v1/board
def test_board_shape_and_source_data(api):
    r = api.get("/v1/board", params={"profile": "lydia livingood"})  # names are case-insensitive
    assert r.status_code == 200
    body = r.json()
    assert body["profile"]["name"] == "Lydia Livingood" and body["total"] == 3  # not-downloaded video excluded
    video = body["videos"][0]
    assert set(video) >= {"id", "stage", "title", "description", "tags", "target_platforms", "overrides",
                          "youtube_channel", "visibility", "scheduled_at", "file_path", "publications", "source"}
    assert video["stage"] == "backlog" and video["target_platforms"] == ["youtube"]
    assert video["draft_saved"] is False and video["title"].startswith("Caption number")
    assert video["youtube_channel"]["name"] == "Lydia Livingood" and video["file_available"] is True
    assert video["overrides"] == {"tiktok_text": None, "instagram_text": None, "bluesky_text": None,
                                  "threads_text": None, "facebook_text": None}
    assert video["hashtags"] is None
    assert video["source"]["stats"]["views"] == 1000 and "letting go" in video["source"]["caption"]
    assert video["source"]["uploaded_on"] == "2026-10-10" and video["publications"] == []


def test_board_pagination_and_stage_filter(api):
    page1 = api.get("/v1/board", params={"profile": "Lydia Livingood", "limit": 2}).json()
    assert len(page1["videos"]) == 2 and page1["next_offset"] == 2
    page2 = api.get("/v1/board", params={"profile": "Lydia Livingood", "limit": 2, "offset": 2}).json()
    assert len(page2["videos"]) == 1 and page2["next_offset"] is None
    assert {v["id"] for v in page1["videos"]}.isdisjoint({v["id"] for v in page2["videos"]})
    api.world.pipeline.move(api.world.rows[1], Stage.STAGING)
    staging = api.get("/v1/board", params={"profile": "Lydia Livingood", "stage": "staging"}).json()
    assert [v["id"] for v in staging["videos"]] == [api.world.rows[1]] and staging["total"] == 1
    assert api.get("/v1/board", params={"profile": "Lydia Livingood", "stage": "nope"}).status_code == 422
    assert api.get("/v1/board", params={"profile": "Lydia Livingood", "limit": 500}).status_code == 422


def test_board_profile_errors_are_400(api):
    missing = api.get("/v1/board")
    assert missing.status_code == 400 and "profile" in missing.json()["detail"]
    unknown = api.get("/v1/board", params={"profile": "Nobody"})
    assert unknown.status_code == 400 and "Lydia Livingood" in unknown.json()["detail"]


# ------------------------------------------------------------------ PATCH /v1/videos/{id}
def test_patch_applies_and_redraws(api):
    seen = []
    api.world.store.subscribe(seen.append)
    vid = api.world.rows[0]
    r = api.patch(f"/v1/videos/{vid}", json={"title": "Letting go of who you were", "tags": ["growth", "#healing"],
                                             "target_platforms": ["youtube", "bluesky"],
                                             "bluesky_text": "Letting go 🦋 #growth"})
    assert r.status_code == 200, r.text
    video = r.json()
    assert video["title"] == "Letting go of who you were" and video["tags"] == ["growth", "healing"]
    assert video["target_platforms"] == ["youtube", "bluesky"] and video["draft_saved"] is True
    assert video["overrides"]["bluesky_text"] == "Letting go 🦋 #growth"
    reread = api.get("/v1/board", params={"profile": "Lydia Livingood"}).json()["videos"]
    assert next(v for v in reread if v["id"] == vid)["title"] == "Letting go of who you were"
    assert any(c.entity == "post" and c.origin == "api" for c in seen)  # the GUI would redraw
    cleared = api.patch(f"/v1/videos/{vid}", json={"bluesky_text": None}).json()
    assert cleared["overrides"]["bluesky_text"] is None


def test_patch_rejects_other_fields_and_bad_types(api):
    vid = api.world.rows[0]
    for body in ({"stage": "published"}, {"title": "ok", "file_path": "/etc/passwd"}, {"title": None},
                 {"title": ""}, {"tags": "not-a-list"}, {"target_platforms": ["myspace"]}, {"target_platforms": []},
                 {}):
        r = api.patch(f"/v1/videos/{vid}", json=body)
        assert r.status_code == 422, body
        assert isinstance(r.json()["detail"], str)
    assert api.world.store.get_post(vid) is None  # nothing was written


def test_patch_is_atomic_and_never_truncates(api):
    vid = api.world.rows[0]
    api.patch(f"/v1/videos/{vid}", json={"title": "Original"})
    too_long = "x" * 301
    r = api.patch(f"/v1/videos/{vid}", json={"title": "Changed", "bluesky_text": too_long})
    assert r.status_code == 422 and any("300" in p for p in r.json()["problems"] or [r.json()["detail"]])
    post = api.world.store.get_post(vid)
    assert post.title == "Original" and post.bluesky_text is None
    emoji_heavy = "🌊" * 126  # 126 × 4 UTF-8 bytes = 504 by Threads' count
    assert api.patch(f"/v1/videos/{vid}", json={"threads_text": emoji_heavy}).status_code == 422
    assert api.patch(f"/v1/videos/{vid}", json={"threads_text": "🌊" * 125}).status_code == 200
    assert api.patch(f"/v1/videos/{vid}", json={"bluesky_text": "😀" * 300}).status_code == 200  # 300 graphemes
    assert api.patch(f"/v1/videos/{vid}", json={"bluesky_text": "😀" * 301}).status_code == 422
    families = api.patch(f"/v1/videos/{vid}", json={"bluesky_text": "👩‍👩‍👧" * 200})  # 200 graphemes, 3,600 bytes
    assert families.status_code == 422 and "bytes" in str(families.json())
    assert api.patch(f"/v1/videos/{vid}", json={"title": "a<b"}).status_code == 422


def test_patch_unknown_video_and_locked(api):
    assert api.patch("/v1/videos/99999", json={"title": "x"}).status_code == 404
    vid = api.world.rows[0]
    r = api.post(f"/v1/videos/{vid}/schedule", json={"scheduled_at": future().isoformat()})
    assert approve_and_run(api.world.store, api.world.pipeline, r).status == "executed"
    r = api.patch(f"/v1/videos/{vid}", json={"title": "too late"})
    assert r.status_code == 409 and r.json()["code"] == "locked"


# ------------------------------------------------------------------ POST /v1/videos/{id}/schedule
def test_schedule_youtube_goes_through_the_upload_queue(api):
    vid, when = api.world.rows[0], future()
    r = api.post(f"/v1/videos/{vid}/schedule", json={"scheduled_at": when.isoformat()})
    assert r.status_code == 202, r.text
    assert r.json()["operation"] == "schedule" and r.json()["target_ids"] == [vid]
    assert api.world.uploads.jobs == [] and api.world.store.dispatches_for(vid) == []   # nothing until approved
    done = approve_and_run(api.world.store, api.world.pipeline, r)
    assert done.status == "executed" and done.result["dispatches"][0]["platform"] == "youtube"
    assert api.world.store.get_video(vid).stage == "scheduled"
    assert api.world.store.get_post(vid).publish_at.startswith(when.strftime("%Y-%m-%dT%H:%M:%S"))
    status = api.get(f"/v1/approvals/{done.id}").json()
    assert status["status"] == "executed" and status["decided_by"] == "test person"
    assert len(api.world.uploads.jobs) == 1  # one upload queue, never a parallel uploader
    resource = api.world.uploads.jobs[0][3]
    assert resource["status"]["privacyStatus"] == "private" and resource["status"]["publishAt"]
    again = api.post(f"/v1/videos/{vid}/schedule", json={})
    assert again.status_code == 409 and again.json()["code"] == "already_scheduled"
    api.world.uploads.finish(vid, True, {"id": "YTabc"})
    assert api.world.store.get_post(vid).status == PostStatus.SCHEDULED


def test_schedule_validation(api):
    vid = api.world.rows[0]
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert api.post(f"/v1/videos/{vid}/schedule", json={"scheduled_at": past}).status_code == 422
    naive = future().replace(tzinfo=None).isoformat()
    assert api.post(f"/v1/videos/{vid}/schedule", json={"scheduled_at": naive}).status_code == 422
    soon = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
    r = api.post(f"/v1/videos/{vid}/schedule", json={"scheduled_at": soon})
    assert r.status_code == 422 and "10 minutes" in str(r.json())
    assert api.post(f"/v1/videos/{vid}/schedule", json={"platforms": ["tiktok"]}).status_code == 422
    assert api.post(f"/v1/videos/{vid}/schedule", json={"surprise": 1}).status_code == 422
    assert api.post("/v1/videos/99999/schedule", json={}).status_code == 404
    missing_file = api.world.rows[3]  # fetched but never downloaded
    r = api.post(f"/v1/videos/{missing_file}/schedule", json={})
    assert r.status_code == 422 and "missing" in str(r.json()).lower()
    assert api.world.uploads.jobs == []


def test_schedule_other_platforms_need_accounts_then_follow_due_times(api, monkeypatch):
    enable_publishing(monkeypatch, Platform.BLUESKY, Platform.FACEBOOK)
    vid, when = api.world.rows[1], future(3)
    api.patch(f"/v1/videos/{vid}", json={"bluesky_text": "Letting go 🦋", "facebook_text": "Letting go."})
    r = api.post(f"/v1/videos/{vid}/schedule", json={"scheduled_at": when.isoformat(), "platforms": ["bluesky"]})
    assert r.status_code == 422 and "Connect a Bluesky account" in str(r.json())
    connect_platform(api.world.accounts, api.world.profile.id, Platform.BLUESKY)
    connect_platform(api.world.accounts, api.world.profile.id, Platform.FACEBOOK)
    r = api.post(f"/v1/videos/{vid}/schedule",
                 json={"scheduled_at": when.isoformat(), "platforms": ["bluesky", "facebook"]})
    assert approve_and_run(api.world.store, api.world.pipeline, r).status == "executed"
    jobs = {d.platform: d for d in api.world.store.dispatches_for(vid)}
    assert set(jobs) == {"bluesky", "facebook"} and not api.world.uploads.jobs
    assert jobs["bluesky"].status == "pending" and jobs["bluesky"].due_at.startswith(when.strftime("%Y-%m-%dT%H:%M"))
    assert datetime.fromisoformat(jobs["facebook"].due_at.replace("Z", "+00:00")) <= \
        datetime.now(timezone.utc) + timedelta(seconds=5)
    assert api.world.store.get_video(vid).stage == Stage.SCHEDULED


def test_threads_requires_public_url(api, monkeypatch):
    enable_publishing(monkeypatch, Platform.THREADS)
    monkeypatch.delenv("REELAY_PUBLIC_URL", raising=False)
    connect_platform(api.world.accounts, api.world.profile.id, Platform.THREADS)
    vid = api.world.rows[0]
    api.patch(f"/v1/videos/{vid}", json={"threads_text": "Letting go"})
    r = api.post(f"/v1/videos/{vid}/schedule", json={"platforms": ["threads"]})
    assert r.status_code == 422 and "REELAY_PUBLIC_URL" in str(r.json())
    api.world.store.set_setting("public_url", "https://reelay.example.com")
    assert api.post(f"/v1/videos/{vid}/schedule", json={"platforms": ["threads"]}).status_code == 202


# ------------------------------------------------------------------ GET /v1/platforms
def test_platforms_report_presence_never_secrets(api):
    connect_platform(api.world.accounts, api.world.profile.id, Platform.BLUESKY)
    r = api.get("/v1/platforms", params={"profile": "Lydia Livingood"})
    assert r.status_code == 200
    info = {p["platform"]: p for p in r.json()["platforms"]}
    assert set(info) == {"youtube", "bluesky", "threads", "facebook"}
    assert info["youtube"]["connected"] and info["youtube"]["accounts"][0]["credential_present"]
    assert info["youtube"]["auto_post"] is True and info["youtube"]["setup_issue"] is None
    assert info["bluesky"]["connected"] and info["bluesky"]["accounts"][0]["last_verified_at"]
    assert info["bluesky"]["auto_post"] is False and "REELAY_PUBLISH_BLUESKY=1" in info["bluesky"]["setup_issue"]
    assert not info["threads"]["connected"] and not info["threads"]["auto_post"]
    for secret in (*SECRETS.values(), "client-secret", "refresh", "access"):
        assert secret not in r.text
    assert api.get("/v1/platforms").status_code == 400


def test_secrets_never_logged(api, caplog):
    caplog.set_level(logging.DEBUG)
    connect_platform(api.world.accounts, api.world.profile.id, Platform.BLUESKY)
    api.get("/v1/board", params={"profile": "Lydia Livingood"})
    api.get("/v1/platforms", params={"profile": "Lydia Livingood"})
    requests.get(api.base + "/v1/board?profile=x", headers={"Authorization": "Bearer guess"}, timeout=10)
    for secret in SECRETS.values():
        assert secret not in caplog.text


# ------------------------------------------------------------------ media links (Threads)
def test_media_links_serve_ranges_and_expire(api):
    path = api.world.folder / "video0.mp4"
    token = api.media.issue(path)
    full = requests.get(f"{api.base}/media/{token}", timeout=10)  # no bearer: the token is the key
    assert full.status_code == 200 and full.content == path.read_bytes()
    part = requests.get(f"{api.base}/media/{token}", headers={"Range": "bytes=0-99"}, timeout=10)
    assert part.status_code == 206 and len(part.content) == 100
    assert requests.get(f"{api.base}/media/not-a-token", timeout=10).status_code == 404
    expired = MediaLinks(ttl=0)
    assert expired.resolve(expired.issue(path)) is None


# ------------------------------------------------------------------ agent token
def test_agent_token_generated_once_stored_in_keychain(tmp_path, monkeypatch, caplog):
    from reelay.accounts import AGENT_TOKEN_KEY, AccountService
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    caplog.set_level(logging.INFO)
    monkeypatch.delenv("REELAY_API_TOKEN", raising=False)
    tokens = MemoryTokenStore()
    accounts = AccountService(Store(tmp_path / "t.db"), tokens)
    first = accounts.agent_token()
    assert len(first) >= 40 and tokens.get(AGENT_TOKEN_KEY) == first and accounts.agent_token() == first
    assert "keychain" in caplog.text and first not in caplog.text
    rotated = accounts.rotate_agent_token()
    assert rotated != first and tokens.get(AGENT_TOKEN_KEY) == rotated and rotated not in caplog.text
    monkeypatch.setenv("REELAY_API_TOKEN", "from-env")
    assert accounts.agent_token() == "from-env"


# ------------------------------------------------------------------ ticket copy + publishing flags
def test_copy_blocks_round_trip_with_platform_limits(api):
    vid = api.world.rows[0]
    copy = {"tiktok_text": "letting go 🦋 #growth", "instagram_text": "Letting go. 🦋\n\n#growth #healing",
            "facebook_text": "A note on letting go.", "hashtags": "#growth #healing #transformation"}
    r = api.patch(f"/v1/videos/{vid}", json=copy)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {k: body["overrides"][k] for k in ("tiktok_text", "instagram_text", "facebook_text")} == \
        {k: copy[k] for k in ("tiktok_text", "instagram_text", "facebook_text")}
    assert body["hashtags"] == copy["hashtags"]
    for bad in ({"tiktok_text": "x" * 4001}, {"instagram_text": "y" * 2201},
                {"instagram_text": " ".join(f"#tag{n}" for n in range(31))}, {"facebook_text": "z" * 63207}):
        r = api.patch(f"/v1/videos/{vid}", json={"title": "Should not save", **bad})
        assert r.status_code == 422, bad
    post = api.world.store.get_post(vid)
    assert post.title != "Should not save" and post.tiktok_text == copy["tiktok_text"]  # nothing truncated or saved


def test_scheduling_a_disabled_platform_is_rejected(api):
    connect_platform(api.world.accounts, api.world.profile.id, Platform.BLUESKY)
    vid = api.world.rows[0]
    r = api.post(f"/v1/videos/{vid}/schedule", json={"platforms": ["bluesky"]})
    assert r.status_code == 422 and r.json()["code"] == "platform_disabled"
    assert "not enabled" in r.json()["detail"] and "REELAY_PUBLISH_BLUESKY=1" in r.json()["detail"]
    assert api.world.store.dispatches_for(vid) == []


def test_disabled_targets_are_reported_as_manual(api):
    vid = api.world.rows[0]
    api.patch(f"/v1/videos/{vid}", json={"target_platforms": ["youtube", "bluesky", "threads"]})
    r = api.post(f"/v1/videos/{vid}/schedule", json={"scheduled_at": future().isoformat()})
    done = approve_and_run(api.world.store, api.world.pipeline, r)
    assert done.result["manual_platforms"] == ["bluesky", "threads"]
    assert [d["platform"] for d in done.result["dispatches"]] == ["youtube"]
    only_manual = api.world.rows[1]
    api.patch(f"/v1/videos/{only_manual}", json={"target_platforms": ["threads"]})
    r = api.post(f"/v1/videos/{only_manual}/schedule", json={})
    assert r.status_code == 422 and r.json()["code"] == "platform_disabled"


def test_env_override_enables_a_platform(api, monkeypatch):
    connect_platform(api.world.accounts, api.world.profile.id, Platform.BLUESKY)
    monkeypatch.setenv("REELAY_PUBLISH_BLUESKY", "1")
    info = {p["platform"]: p for p in api.get("/v1/platforms", params={"profile": "Lydia Livingood"}).json()["platforms"]}
    assert info["bluesky"]["auto_post"] is True
    api.patch(f"/v1/videos/{api.world.rows[0]}", json={"bluesky_text": "Letting go"})
    r = api.post(f"/v1/videos/{api.world.rows[0]}/schedule", json={"platforms": ["bluesky"]})
    assert approve_and_run(api.world.store, api.world.pipeline, r).result["dispatches"][0]["platform"] == "bluesky"


# ------------------------------------------------------------------ download sources
def test_sources_list_and_add(api):
    from reelay.models import Account
    base = "/v1/sources?profile=Lydia%20Livingood"
    assert requests.get(api.base + base, timeout=10).status_code == 401
    assert api.get("/v1/sources").status_code == 400
    body = api.get(base).json()
    assert body["profile"]["name"] == "Lydia Livingood"
    channel = body["sources"][0]
    assert channel["kind"] == "account" and channel["platform"] == "youtube" and channel["id"] is None
    assert channel["account"]["name"] == "Lydia Livingood" and channel["mine"] is True

    r = api.post(base, json={"url": "https://www.tiktok.com/@livingood.lifestyle"})
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["kind"] == "saved" and created["platform"] == "tiktok" and created["id"]
    assert created["name"] == "@livingood.lifestyle on TikTok" and created["mine"] is False
    again = api.post(base, json={"url": "tiktok.com/@livingood.lifestyle/", "name": "Lydia's TikTok", "mine": True})
    assert again.status_code == 201  # duplicates are allowed
    listed = api.get(base).json()["sources"]
    assert [s["kind"] for s in listed].count("saved") == 2  # API lists them as stored

    for bad in ({"url": "https://vimeo.com/x"}, {"url": ""}, {"url": TIKTOK_URL, "extra": 1},
                {"url": TIKTOK_URL, "name": "n" * 81}):
        assert api.post(base, json=bad).status_code == 422, bad
    assert api.post("/v1/sources?profile=Nobody", json={"url": TIKTOK_URL}).status_code == 400
    for secret in SECRETS.values():
        assert secret not in api.get(base).text


TIKTOK_URL = "https://www.tiktok.com/@someone"


def test_coverage_and_handoff_link_through_the_api(api):
    row = api.world.rows[0]
    api.world.pipeline.update_metadata(row, {"target_platforms": ["tiktok"], "tiktok_text": "hi"})
    v = api.patch(f"/v1/videos/{row}", json={"tiktok_text": "hi there"}).json()
    assert v["coverage"]["tiktok"] == {"state": "ready", "reason": None}
    assert v["coverage"]["threads"]["state"] == "not_applicable"
    bad = api.post(f"/v1/videos/{row}/platforms/tiktok", json={"state": "posted", "url": "https://bsky.app/x"})
    assert bad.status_code == 422 and "TikTok link" in bad.text
    ok = api.post(f"/v1/videos/{row}/platforms/tiktok",
                  json={"state": "posted", "url": "https://www.tiktok.com/@x/video/9"})
    assert approve_and_run(api.world.store, api.world.pipeline, ok).status == "executed"
    assert api.world.pipeline.card(row).coverage["tiktok"].state == "published"
    assert api.world.store.manual_posts(row)[0].url == "https://www.tiktok.com/@x/video/9"


def test_search_finds_spoken_words(api):
    row = api.world.rows[1]
    api.patch(f"/v1/videos/{row}", json={"transcript": "Today I'm talking about polarized sunglasses"})
    r = api.get("/v1/search", params={"profile": api.world.profile.name, "q": "polarized sungl"}).json()
    assert r["total"] == 1 and r["results"][0]["spoken"] and r["results"][0]["video"]["id"] == row
    assert "«sunglasses»" in r["results"][0]["snippet"]
    r = api.get("/v1/search", params={"profile": api.world.profile.name, "q": "letting go"}).json()
    assert r["total"] == 4 and not r["results"][0]["spoken"]           # original captions, downloaded or not
