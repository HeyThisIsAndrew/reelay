"""Bluesky (AT Protocol) video posts.

Two ways in, same posting:
- Sign in with Bluesky (OAuth, bluesky_oauth.py): every PDS call carries the
  DPoP-bound access token; tokens refresh as needed and the rotated ones are
  saved back to the Keychain through job.save_credentials.
- App Password (Advanced Setup, and every account connected before OAuth):
  createSession as below.

Flow (docs.bsky.app "Uploading video" tutorial + app.bsky.video lexicons):
1. com.atproto.server.createSession on the entryway with handle + App Password
   → access JWT, DID and the account's PDS (OAuth: already in the sign-in).
2. com.atproto.server.getServiceAuth (aud = did:web:<PDS host>,
   lxm = com.atproto.repo.uploadBlob, 30-minute expiry) → service token.
3. app.bsky.video.uploadVideo on video.bsky.app (?did=&name=, body = mp4)
   → jobStatus; a 409 means this exact file was already processed.
4. Poll app.bsky.video.getJobStatus until JOB_STATE_COMPLETED → blob.
5. com.atproto.repo.createRecord app.bsky.feed.post with an
   app.bsky.embed.video embed, using a record key chosen up front so a
   retry can tell whether the post already exists.
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from .. import bluesky_oauth
from ..handles import clean_app_password, clean_handle
from ..oauth import SignInError
from ..models import DispatchStatus, Platform
from ..platform_text import bluesky_facets
from .base import PermanentError, PublishJob, PublishResult, TransientError, error_from_response, network_error

ENTRYWAY = "https://bsky.social"
VIDEO_SERVICE = "https://video.bsky.app"
MAX_VIDEO_BYTES = 300_000_000
JOB_POLL_SECONDS = 2.0
JOB_TIMEOUT_SECONDS = 600

_B32 = "234567abcdefghijklmnopqrstuvwxyz"


def make_tid(now_us: int | None = None) -> str:
    """An AT Protocol TID (sortable record key): 53-bit µs timestamp + 10-bit clock id."""
    us = now_us if now_us is not None else time.time_ns() // 1000
    value = (us << 10) | random.getrandbits(10)
    out = ""
    for _ in range(13):
        out = _B32[value & 31] + out
        value >>= 5
    return out


class _Connection:
    """The signed-in account: who it is, where its PDS is, and how to call it."""

    def __init__(self, did: str, handle: str, pds: str, secrets: tuple, call, oauth=None, auth=None):
        self.did, self.handle, self.pds, self.secrets = did, handle, pds.rstrip("/"), tuple(secrets)
        self._call, self.oauth = call, oauth
        self.auth = dict(auth or {})          # App Password: the session's Bearer header (OAuth adds its own)

    def pds_call(self, method: str, url: str, **kwargs):
        if self.oauth is None:
            return self._call(method, url, self.secrets, **kwargs)
        try:
            resp = self.oauth.request(method, url, **kwargs)
        except SignInError as exc:
            raise PermanentError(f"Bluesky: {exc}") from None
        except requests.RequestException as exc:
            raise network_error(exc, "Bluesky", self.oauth.secrets()) from None
        self.secrets = tuple(self.secrets) + self.oauth.secrets()
        return resp

    @property
    def job_auth(self) -> dict:
        """Headers for the video service's job status: the App Password session's token, or none for an
        OAuth sign-in (its token is bound to the PDS, and the status call doesn't need one)."""
        return {} if self.oauth is not None else self.auth


class BlueskyPublisher:
    platform = Platform.BLUESKY

    def __init__(self, session: requests.Session | None = None, sleep=time.sleep):
        self.session = session or requests.Session()
        self.sleep = sleep

    # ---------------------------------------------------------------- helpers
    def _call(self, method: str, url: str, secrets, **kwargs):
        try:
            resp = self.session.request(method, url, timeout=kwargs.pop("timeout", 60), **kwargs)
        except requests.RequestException as exc:
            raise network_error(exc, "Bluesky", secrets) from None
        return resp

    def login(self, handle: str, password: str, service: str = ENTRYWAY) -> dict:
        """createSession → {"accessJwt", "did", "handle", "pds"}. Takes the handle however it was typed
        ("@name.bsky.social", a profile link) and an App Password with spaces between its groups."""
        handle, password = clean_handle(handle, "bluesky"), clean_app_password(password)
        secrets = (password,)
        resp = self._call("POST", f"{service.rstrip('/')}/xrpc/com.atproto.server.createSession", secrets,
                          json={"identifier": handle, "password": password})
        if resp.status_code != 200:
            err = error_from_response(resp, "Bluesky", secrets)
            if resp.status_code in (400, 401):
                raise PermanentError("Bluesky: sign-in failed. Check the handle and App Password.")
            raise err
        data = resp.json()
        pds = service
        for svc in (data.get("didDoc") or {}).get("service", []):
            if svc.get("id") in ("#atproto_pds", f"{data.get('did')}#atproto_pds"):
                pds = svc.get("serviceEndpoint") or pds
        return {"accessJwt": data["accessJwt"], "did": data["did"], "handle": data.get("handle", handle),
                "pds": pds.rstrip("/"), "emailConfirmed": data.get("emailConfirmed")}

    def connect(self, credentials: dict, save=None) -> _Connection:
        """Sign in with whatever is saved: an OAuth session (Sign in with Bluesky) or an App Password."""
        if bluesky_oauth.is_oauth_secret(credentials):
            try:
                oauth = bluesky_oauth.OAuthConnection(credentials, http=self.session, save=save)
            except SignInError as exc:
                raise PermanentError(f"Bluesky: {exc}") from None
            return _Connection(oauth.did, oauth.handle, oauth.pds, oauth.secrets(), self._call, oauth)
        s = self.login(credentials["handle"], credentials["app_password"], credentials.get("service") or ENTRYWAY)
        return _Connection(s["did"], s["handle"], s["pds"], (credentials.get("app_password", ""), s["accessJwt"]),
                           self._call, auth={"Authorization": f"Bearer {s['accessJwt']}"})

    def verify(self, credentials: dict, save=None) -> dict:
        """Live check used by Accounts: returns {"external_id", "display_name", "handle"}. Nothing is posted.
        `save` receives rotated tokens when an OAuth sign-in had to refresh."""
        conn = self.connect(credentials, save)
        if conn.oauth is not None:
            resp = conn.pds_call("GET", f"{conn.pds}/xrpc/com.atproto.server.getSession")
            if resp.status_code != 200:
                if resp.status_code in (400, 401):
                    raise PermanentError(f"Bluesky: {bluesky_oauth.EXPIRED}")
                raise error_from_response(resp, "Bluesky", conn.secrets)
            handle = str(_json(resp).get("handle") or conn.handle)
            return {"external_id": conn.did, "display_name": handle, "handle": "@" + handle.lstrip("@")}
        return {"external_id": conn.did, "display_name": conn.handle, "handle": "@" + conn.handle.lstrip("@")}

    # ---------------------------------------------------------------- publish
    def publish(self, job: PublishJob) -> PublishResult:
        state = json.loads(job.dispatch.checkpoint) if job.dispatch.checkpoint else {}
        conn = self.connect(job.credentials, job.save_credentials)
        did, pds = conn.did, conn.pds
        session = {"did": did, "handle": conn.handle}

        rkey = state.get("rkey")
        if rkey and self._record_exists(conn, rkey):
            return self._result(session, rkey)

        blob = state.get("blob")
        if not blob:
            blob = self._upload_video(job, conn)
            state["blob"] = blob
            job.checkpoint(json.dumps(state))

        rkey = rkey or make_tid()
        state["rkey"] = rkey
        job.checkpoint(json.dumps(state))   # before createRecord: a retry checks for this key first

        item = job.item
        embed = {"$type": "app.bsky.embed.video", "video": blob}
        if item.width and item.height:
            embed["aspectRatio"] = {"width": int(item.width), "height": int(item.height)}
        record = {
            "$type": "app.bsky.feed.post",
            "text": job.text,
            "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "embed": embed,
        }
        facets = bluesky_facets(job.text)
        if facets:
            record["facets"] = facets
        resp = conn.pds_call("POST", f"{pds}/xrpc/com.atproto.repo.createRecord", headers=conn.auth,
                             json={"repo": did, "collection": "app.bsky.feed.post", "rkey": rkey, "record": record})
        if resp.status_code != 200:
            if self._record_exists(conn, rkey):
                return self._result(session, rkey)
            raise error_from_response(resp, "Bluesky", conn.secrets)
        return self._result(session, rkey, resp.json().get("uri"))

    def _upload_video(self, job: PublishJob, conn: _Connection) -> dict:
        size = job.video_path.stat().st_size
        if size > MAX_VIDEO_BYTES:
            raise PermanentError("Bluesky: the video is larger than Bluesky's 300 MB limit.")
        did, pds = conn.did, conn.pds
        host = urlparse(pds).hostname
        resp = conn.pds_call("GET", f"{pds}/xrpc/com.atproto.server.getServiceAuth", headers=conn.auth, params={
            "aud": f"did:web:{host}", "lxm": "com.atproto.repo.uploadBlob", "exp": int(time.time()) + 1800})
        if resp.status_code != 200:
            raise error_from_response(resp, "Bluesky", conn.secrets)
        service_token = resp.json()["token"]
        secrets = tuple(conn.secrets) + (service_token,)
        headers = {"Authorization": f"Bearer {service_token}", "Content-Type": "video/mp4",
                   "Content-Length": str(size)}
        with job.video_path.open("rb") as fh:
            resp = self._call("POST", f"{VIDEO_SERVICE}/xrpc/app.bsky.video.uploadVideo", secrets, headers=headers,
                              params={"did": did, "name": job.video_path.name}, data=fh, timeout=600)
        data = _json(resp)
        status = data.get("jobStatus") or data
        if resp.status_code == 409 and status.get("jobId"):
            pass  # already uploaded earlier; reuse that job
        elif resp.status_code != 200:
            raise error_from_response(resp, "Bluesky", secrets)
        if status.get("blob"):
            return status["blob"]
        job_id = status.get("jobId")
        if not job_id:
            raise TransientError("Bluesky: the video service didn't return a processing job.")
        return self._wait_for_job(job_id, conn.job_auth, secrets)

    def _wait_for_job(self, job_id: str, auth: dict, secrets) -> dict:
        deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
        while True:
            resp = self._call("GET", f"{VIDEO_SERVICE}/xrpc/app.bsky.video.getJobStatus", secrets, headers=auth,
                              params={"jobId": job_id})
            if resp.status_code != 200:
                raise error_from_response(resp, "Bluesky", secrets)
            status = resp.json().get("jobStatus", {})
            if status.get("state") == "JOB_STATE_COMPLETED" and status.get("blob"):
                return status["blob"]
            if status.get("state") == "JOB_STATE_FAILED":
                raise PermanentError(f"Bluesky couldn't process the video: "
                                     f"{status.get('error') or status.get('failureCode') or 'unknown error'}")
            if time.monotonic() > deadline:
                raise TransientError("Bluesky is still processing the video; will retry.")
            self.sleep(JOB_POLL_SECONDS)

    @staticmethod
    def _record_exists(conn: _Connection, rkey: str) -> bool:
        resp = conn.pds_call("GET", f"{conn.pds}/xrpc/com.atproto.repo.getRecord", headers=conn.auth,
                             params={"repo": conn.did, "collection": "app.bsky.feed.post", "rkey": rkey})
        return resp.status_code == 200

    @staticmethod
    def _result(session: dict, rkey: str, uri: str | None = None) -> PublishResult:
        return PublishResult(DispatchStatus.PUBLISHED,
                             remote_id=uri or f"at://{session['did']}/app.bsky.feed.post/{rkey}",
                             remote_url=f"https://bsky.app/profile/{session['handle']}/post/{rkey}")


def _json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
