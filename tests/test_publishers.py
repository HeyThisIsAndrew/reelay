"""Bluesky / Threads / Facebook publishers against scripted fake transports.
Checks the documented request flow, payloads, resume-after-crash, error
classification, and that secrets never reach URLs or messages."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from reelay.models import Account, Dispatch, DispatchStatus, Post, VideoItem
from reelay.publishers import BlueskyPublisher, FacebookPublisher, ThreadsPublisher
from reelay.publishers.base import PermanentError, PublishJob, TransientError, redact
from reelay.publishers.bluesky import make_tid
from reelay.publishers.facebook import STARTED
from tests.conftest import FakeResponse, FakeSession

APP_PASSWORD = "abcd-efgh-ijkl-mnop"
THREADS_TOKEN = "THQVJ-threads-secret"
PAGE_TOKEN = "EAAB-page-secret"


def make_job(tmp_path: Path, platform: str, credentials: dict, text: str = "Letting go 🦋 #growth https://x.co/a",
             checkpoint: str = "", publish_at: str | None = None, media_url=None, account_id="acct"):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\0" * 4096)
    saved: list[str] = []
    dispatch = Dispatch(video_row=1, platform=platform, due_at="2026-10-08T12:00:00Z", publish_at=publish_at,
                        checkpoint=checkpoint, id=9)
    job = PublishJob(dispatch=dispatch, post=Post(video_row=1, title="Letting go", description="A short video."),
                     item=VideoItem(platform="TikTok", video_id="1", url="u", width=1080, height=1920, duration=30),
                     video_path=video, account=Account(profile_id=1, platform=platform, external_id=account_id,
                                                       display_name="x"),
                     credentials=credentials, text=text, media_url=media_url, checkpoint=saved.append)
    return job, saved


def no_secret_in_urls(session: FakeSession, *secrets):
    for call in session.calls:
        for secret in secrets:
            assert secret not in call["url"], call["url"]
            assert secret not in json.dumps(call.get("params") or {})


# ====================================================================== Bluesky
BSKY_CREDS = {"handle": "lydia.bsky.social", "app_password": APP_PASSWORD}
SESSION = {"accessJwt": "jwt-123", "did": "did:plc:abc", "handle": "lydia.bsky.social",
           "didDoc": {"service": [{"id": "#atproto_pds", "serviceEndpoint": "https://pds.example.host"}]}}
BLOB = {"$type": "blob", "ref": {"$link": "bafkrei"}, "mimeType": "video/mp4", "size": 4096}


def bluesky_session(upload_response=None, job_states=("JOB_STATE_ENCODING", "JOB_STATE_COMPLETED")):
    s = FakeSession()
    s.on("POST", "com.atproto.server.createSession", FakeResponse(200, SESSION), repeat=True)
    s.on("GET", "com.atproto.server.getServiceAuth", FakeResponse(200, {"token": "svc-token"}))
    s.on("POST", "app.bsky.video.uploadVideo", upload_response or FakeResponse(
        200, {"jobStatus": {"jobId": "job-1", "did": SESSION["did"], "state": "JOB_STATE_CREATED"}}))
    for state in job_states:
        body = {"jobId": "job-1", "did": SESSION["did"], "state": state}
        if state == "JOB_STATE_COMPLETED":
            body["blob"] = BLOB
        if state == "JOB_STATE_FAILED":
            body["error"] = "video too long"
        s.on("GET", "app.bsky.video.getJobStatus", FakeResponse(200, {"jobStatus": body}))
    s.on("GET", "com.atproto.repo.getRecord", FakeResponse(400, {"error": "RecordNotFound"}), repeat=True)
    s.on("POST", "com.atproto.repo.createRecord",
         FakeResponse(200, {"uri": "at://did:plc:abc/app.bsky.feed.post/3kabc", "cid": "c"}))
    return s


def test_bluesky_full_flow(tmp_path):
    s = bluesky_session()
    job, saved = make_job(tmp_path, "bluesky", BSKY_CREDS)
    result = BlueskyPublisher(s, sleep=lambda _s: None).publish(job)
    assert result.status == DispatchStatus.PUBLISHED and result.remote_url.startswith(
        "https://bsky.app/profile/lydia.bsky.social/post/")
    login = s.calls[0]
    assert login["json"] == {"identifier": "lydia.bsky.social", "password": APP_PASSWORD}
    auth = next(c for c in s.calls if "getServiceAuth" in c["url"])
    assert auth["url"].startswith("https://pds.example.host/") and auth["params"]["aud"] == "did:web:pds.example.host"
    assert auth["params"]["lxm"] == "com.atproto.repo.uploadBlob"
    upload = next(c for c in s.calls if "uploadVideo" in c["url"])
    assert upload["url"].startswith("https://video.bsky.app/")
    assert upload["params"] == {"did": "did:plc:abc", "name": "clip.mp4"}
    assert upload["headers"]["Authorization"] == "Bearer svc-token" and upload["headers"]["Content-Type"] == "video/mp4"
    create = next(c for c in s.calls if "createRecord" in c["url"])["json"]
    record = create["record"]
    assert create["repo"] == "did:plc:abc" and create["collection"] == "app.bsky.feed.post" and create["rkey"]
    assert record["embed"] == {"$type": "app.bsky.embed.video", "video": BLOB,
                               "aspectRatio": {"width": 1080, "height": 1920}}
    assert record["text"] == job.text and record["createdAt"].endswith("Z")
    assert {f["features"][0]["$type"].split("#")[1] for f in record["facets"]} == {"tag", "link"}
    assert json.loads(saved[-1]) == {"blob": BLOB, "rkey": create["rkey"]}  # checkpoint before createRecord
    no_secret_in_urls(s, APP_PASSWORD, "jwt-123", "svc-token")


def test_bluesky_resume_does_not_post_twice(tmp_path):
    s = FakeSession()
    s.on("POST", "createSession", FakeResponse(200, SESSION))
    s.on("GET", "com.atproto.repo.getRecord", FakeResponse(200, {"uri": "at://x"}))
    checkpoint = json.dumps({"blob": BLOB, "rkey": "3kexisting"})
    job, _ = make_job(tmp_path, "bluesky", BSKY_CREDS, checkpoint=checkpoint)
    result = BlueskyPublisher(s).publish(job)
    assert result.remote_url.endswith("/post/3kexisting")
    assert not any("createRecord" in c["url"] or "uploadVideo" in c["url"] for c in s.calls)


def test_bluesky_reuses_already_processed_upload(tmp_path):
    s = bluesky_session(upload_response=FakeResponse(409, {"jobId": "job-1", "error": "already_exists"}),
                        job_states=("JOB_STATE_COMPLETED",))
    job, _ = make_job(tmp_path, "bluesky", BSKY_CREDS)
    assert BlueskyPublisher(s, sleep=lambda _s: None).publish(job).status == DispatchStatus.PUBLISHED


def test_bluesky_errors(tmp_path):
    bad_login = FakeSession().on("POST", "createSession", FakeResponse(401, {"error": "AuthenticationRequired"}))
    job, _ = make_job(tmp_path, "bluesky", BSKY_CREDS)
    with pytest.raises(PermanentError) as err:
        BlueskyPublisher(bad_login).publish(job)
    assert APP_PASSWORD not in str(err.value) and "App Password" in str(err.value)

    failed = bluesky_session(job_states=("JOB_STATE_FAILED",))
    with pytest.raises(PermanentError, match="video too long"):
        BlueskyPublisher(failed, sleep=lambda _s: None).publish(job)

    flaky = FakeSession().on("POST", "createSession", FakeResponse(503, {"error": "Unavailable"}))
    with pytest.raises(TransientError):
        BlueskyPublisher(flaky).publish(job)

    def boom(*_a):
        raise requests.ConnectionError(f"failed sending password={APP_PASSWORD}")
    offline = FakeSession().on("POST", "createSession", boom)
    with pytest.raises(TransientError) as err:
        BlueskyPublisher(offline).publish(job)
    assert APP_PASSWORD not in str(err.value)


def test_tids_are_sortable_13_char_keys():
    a, b = make_tid(1_700_000_000_000_000), make_tid(1_700_000_000_000_001)
    assert len(a) == 13 and a < b and set(a) <= set("234567abcdefghijklmnopqrstuvwxyz")


# ====================================================================== Threads
THREADS_CREDS = {"access_token": THREADS_TOKEN, "user_id": "1784"}


def test_threads_container_poll_publish(tmp_path):
    s = FakeSession()
    s.on("POST", "graph.threads.net/v1.0/1784/threads", FakeResponse(200, {"id": "C1"}))
    s.on("GET", "/v1.0/C1", FakeResponse(200, {"status": "IN_PROGRESS"}))
    s.on("GET", "/v1.0/C1", FakeResponse(200, {"status": "FINISHED"}))
    s.on("POST", "/1784/threads_publish", FakeResponse(200, {"id": "M1"}))
    s.on("GET", "/v1.0/M1", FakeResponse(200, {"permalink": "https://www.threads.net/@livingood/post/abc"}))
    job, saved = make_job(tmp_path, "threads", THREADS_CREDS, account_id="1784",
                          media_url=lambda: "https://reelay.example.com/media/tok")
    result = ThreadsPublisher(s, sleep=lambda _s: None).publish(job)
    assert result.remote_id == "M1" and result.remote_url.endswith("/post/abc")
    create = s.calls[0]
    assert create["data"] == {"media_type": "VIDEO", "video_url": "https://reelay.example.com/media/tok",
                              "text": job.text}
    assert create["headers"]["Authorization"] == f"Bearer {THREADS_TOKEN}"
    assert s.calls[3]["data"] == {"creation_id": "C1"} and saved == ["C1"]
    no_secret_in_urls(s, THREADS_TOKEN)


def test_threads_resume_and_failures(tmp_path):
    published = FakeSession().on("GET", "/v1.0/C9", FakeResponse(200, {"status": "PUBLISHED"}), repeat=True)
    job, _ = make_job(tmp_path, "threads", THREADS_CREDS, checkpoint="C9", account_id="1784")
    assert ThreadsPublisher(published).publish(job).remote_id == "C9"
    assert not any(c["method"] == "POST" for c in published.calls)

    errored = FakeSession()
    errored.on("POST", "/1784/threads", FakeResponse(200, {"id": "C2"}))
    errored.on("GET", "/v1.0/C2", FakeResponse(200, {"status": "ERROR", "error_message": "FAILED_DOWNLOADING_VIDEO"}))
    job, saved = make_job(tmp_path, "threads", THREADS_CREDS, account_id="1784", media_url=lambda: "https://p/m")
    with pytest.raises(PermanentError, match="FAILED_DOWNLOADING_VIDEO"):
        ThreadsPublisher(errored, sleep=lambda _s: None).publish(job)
    assert saved[-1] == ""  # cleared, so a retry creates a fresh container

    job, _ = make_job(tmp_path, "threads", THREADS_CREDS, account_id="1784", media_url=None)
    with pytest.raises(PermanentError, match="REELAY_PUBLIC_URL"):
        ThreadsPublisher(FakeSession()).publish(job)

    limited = FakeSession().on("POST", "/1784/threads", FakeResponse(
        400, {"error": {"message": "Application request limit reached", "code": 4}}))
    job, _ = make_job(tmp_path, "threads", THREADS_CREDS, account_id="1784", media_url=lambda: "https://p/m")
    with pytest.raises(TransientError):
        ThreadsPublisher(limited).publish(job)


# ====================================================================== Facebook
FB_CREDS = {"page_id": "1029", "page_access_token": PAGE_TOKEN}


def test_facebook_schedules_natively(tmp_path):
    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    when = now + timedelta(days=2)
    s = FakeSession().on("POST", "graph-video.facebook.com/v26.0/1029/videos", FakeResponse(200, {"id": "V77"}))
    job, saved = make_job(tmp_path, "facebook", FB_CREDS, publish_at=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          account_id="1029")
    result = FacebookPublisher(s, now=lambda: now).publish(job)
    assert result.status == DispatchStatus.SCHEDULED and result.remote_url.endswith("/1029/videos/V77")
    call = s.calls[0]
    assert call["data"]["published"] == "false" and call["data"]["scheduled_publish_time"] == str(int(when.timestamp()))
    assert call["data"]["title"] == "Letting go" and call["data"]["description"] == "A short video."
    assert call["files"]["source"][0] == "clip.mp4" and saved == [STARTED]
    no_secret_in_urls(s, PAGE_TOKEN)


def test_facebook_publishes_now_when_no_future_time(tmp_path):
    s = FakeSession().on("POST", "/videos", FakeResponse(200, {"id": "V1"}))
    job, _ = make_job(tmp_path, "facebook", FB_CREDS, account_id="1029")
    assert FacebookPublisher(s).publish(job).status == DispatchStatus.PUBLISHED
    assert "published" not in s.calls[0]["data"]


def test_facebook_never_reuploads_after_an_unknown_outcome(tmp_path):
    job, _ = make_job(tmp_path, "facebook", FB_CREDS, checkpoint=STARTED, account_id="1029")
    s = FakeSession()
    with pytest.raises(PermanentError, match="Check the Page"):
        FacebookPublisher(s).publish(job)
    assert s.calls == []


def test_facebook_error_classification(tmp_path):
    expired = FakeSession().on("POST", "/videos", FakeResponse(400, {"error": {
        "message": "Error validating access token", "code": 190}}))
    job, saved = make_job(tmp_path, "facebook", FB_CREDS, account_id="1029")
    with pytest.raises(PermanentError, match="access token"):
        FacebookPublisher(expired).publish(job)
    assert saved[-1] == ""  # Facebook answered: nothing was created, safe to retry later

    down = FakeSession().on("POST", "/videos", FakeResponse(500, {"error": {"message": "Service unavailable",
                                                                           "code": 2}}))
    with pytest.raises(TransientError):
        FacebookPublisher(down).publish(make_job(tmp_path, "facebook", FB_CREDS, account_id="1029")[0])

    def timeout(*_a):
        raise requests.ReadTimeout(f"read timed out access_token={PAGE_TOKEN}")
    hung = FakeSession().on("POST", "/videos", timeout)
    with pytest.raises(PermanentError) as err:  # may have been created: don't retry blindly
        FacebookPublisher(hung).publish(make_job(tmp_path, "facebook", FB_CREDS, account_id="1029")[0])
    assert PAGE_TOKEN not in str(err.value)


# ====================================================================== verify + redact
def test_verify_returns_identity_only(tmp_path):
    s = FakeSession()
    s.on("POST", "createSession", FakeResponse(200, SESSION))
    s.on("GET", "graph.threads.net/v1.0/me", FakeResponse(200, {"id": "1784", "username": "livingood"}))
    s.on("GET", "graph.facebook.com/v26.0/1029", FakeResponse(200, {"id": "1029", "name": "Livin'Good"}))
    assert BlueskyPublisher(s).verify(BSKY_CREDS) == {"external_id": "did:plc:abc",
                                                      "display_name": "lydia.bsky.social",
                                                      "handle": "@lydia.bsky.social"}
    assert ThreadsPublisher(s).verify(THREADS_CREDS)["handle"] == "@livingood"
    assert FacebookPublisher(s).verify(FB_CREDS)["display_name"] == "Livin'Good"
    no_secret_in_urls(s, APP_PASSWORD, THREADS_TOKEN, PAGE_TOKEN)


def test_redact():
    assert redact("x access_token=abc123&y=1", ()) == "x access_token=[redacted]&y=1"
    assert redact("my secret-value here", ("secret-value",)) == "my [redacted] here"
