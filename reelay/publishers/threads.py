"""Threads video posts via the Meta Threads API.

Flow (developers.facebook.com/docs/threads/posts + troubleshooting):
1. POST /{threads-user-id}/threads  media_type=VIDEO, video_url, text
   → media container id. Meta downloads the video from video_url, so it must
   be a public HTTPS URL: Reelay serves a one-time expiring link through
   your Cloudflare Tunnel (REELAY_PUBLIC_URL).
2. GET /{container-id}?fields=status,error_message until FINISHED
   (IN_PROGRESS / ERROR / EXPIRED / PUBLISHED; containers expire after 24 h).
3. POST /{threads-user-id}/threads_publish  creation_id → media id.
4. GET /{media-id}?fields=permalink for the post URL.

Requires an approved Meta app with threads_basic + threads_content_publish.
Access tokens go in request bodies / headers, never in URLs.
"""

from __future__ import annotations

import time

import requests

from ..models import DispatchStatus, Platform
from .base import PermanentError, PublishJob, PublishResult, TransientError, error_from_response, network_error

GRAPH = "https://graph.threads.net/v1.0"
MAX_DURATION_SECONDS = 300
MAX_BYTES = 1_000_000_000
POLL_SECONDS = 10.0
PROCESS_TIMEOUT_SECONDS = 300


class ThreadsPublisher:
    platform = Platform.THREADS

    def __init__(self, session: requests.Session | None = None, sleep=time.sleep):
        self.session = session or requests.Session()
        self.sleep = sleep

    def _call(self, method: str, path: str, token: str, **kwargs):
        headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
        try:
            return self.session.request(method, f"{GRAPH}/{path.lstrip('/')}", headers=headers,
                                        timeout=kwargs.pop("timeout", 60), **kwargs)
        except requests.RequestException as exc:
            raise network_error(exc, "Threads", (token,)) from None

    def verify(self, credentials: dict) -> dict:
        token = credentials["access_token"]
        resp = self._call("GET", "me", token, params={"fields": "id,username"})
        if resp.status_code != 200:
            raise error_from_response(resp, "Threads", (token,))
        data = resp.json()
        return {"external_id": str(data["id"]), "display_name": data.get("username") or str(data["id"]),
                "handle": "@" + data["username"] if data.get("username") else ""}

    def publish(self, job: PublishJob) -> PublishResult:
        token = job.credentials["access_token"]
        user_id = job.account.external_id
        secrets = (token,)
        container = job.dispatch.checkpoint

        if container:
            status = self._status(container, token)
            if status == "PUBLISHED":
                return self._result(container, token)
            if status == "EXPIRED":
                container = ""
            elif status == "ERROR":
                container = ""  # make a fresh container below
        if not container:
            if job.item.duration and job.item.duration > MAX_DURATION_SECONDS:
                raise PermanentError("Threads: videos can be at most 5 minutes long.")
            if job.video_path.stat().st_size > MAX_BYTES:
                raise PermanentError("Threads: videos can be at most 1 GB.")
            if job.media_url is None:
                raise PermanentError("Threads needs a public link to the video. Set REELAY_PUBLIC_URL to your "
                                     "Cloudflare Tunnel address.")
            resp = self._call("POST", f"{user_id}/threads", token, data={
                "media_type": "VIDEO", "video_url": job.media_url(), "text": job.text})
            if resp.status_code != 200:
                raise error_from_response(resp, "Threads", secrets)
            container = str(resp.json()["id"])
            job.checkpoint(container)

        deadline = time.monotonic() + PROCESS_TIMEOUT_SECONDS
        while True:
            status, message = self._status(container, token, with_message=True)
            if status == "FINISHED":
                break
            if status == "PUBLISHED":
                return self._result(container, token)
            if status == "ERROR":
                job.checkpoint("")
                raise PermanentError(f"Threads couldn't process the video: {message or 'unknown error'}")
            if status == "EXPIRED":
                job.checkpoint("")
                raise TransientError("Threads: the upload expired before publishing; will retry.")
            if time.monotonic() > deadline:
                raise TransientError("Threads is still processing the video; will retry.")
            self.sleep(POLL_SECONDS)

        resp = self._call("POST", f"{user_id}/threads_publish", token, data={"creation_id": container})
        if resp.status_code != 200:
            if self._status(container, token) == "PUBLISHED":
                return self._result(container, token)
            raise error_from_response(resp, "Threads", secrets)
        return self._result(str(resp.json()["id"]), token)

    def _status(self, container: str, token: str, with_message: bool = False):
        resp = self._call("GET", container, token, params={"fields": "status,error_message"})
        if resp.status_code != 200:
            raise error_from_response(resp, "Threads", (token,))
        data = resp.json()
        return (data.get("status"), data.get("error_message")) if with_message else data.get("status")

    def _result(self, media_id: str, token: str) -> PublishResult:
        url = ""
        try:
            resp = self._call("GET", media_id, token, params={"fields": "permalink"})
            if resp.status_code == 200:
                url = resp.json().get("permalink", "")
        except TransientError:
            pass
        return PublishResult(DispatchStatus.PUBLISHED, remote_id=media_id, remote_url=url)
