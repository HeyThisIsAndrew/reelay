"""Facebook Page video posts via the Graph API.

POST /{page-id}/videos (multipart `source`) with title + description and a
Page access token (CREATE_CONTENT on the Page; pages_manage_posts,
pages_read_engagement, pages_show_list). For a future publish time, send
published=false and scheduled_publish_time, which Facebook allows between
10 minutes and 6 months ahead; Facebook then publishes it on its own.

Video uploads have no idempotency key, so before uploading Reelay records a
"started" checkpoint. If a retry finds that marker without a result (the app
died mid-request), it fails the platform instead of risking a duplicate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from ..models import DispatchStatus, Platform
from ..publishing import parse_utc
from .base import PermanentError, PublishJob, PublishResult, TransientError, error_from_response, redact

GRAPH_VERSION = "v26.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
GRAPH_VIDEO = f"https://graph-video.facebook.com/{GRAPH_VERSION}"
MIN_LEAD = timedelta(minutes=10)
MAX_LEAD = timedelta(days=182)
STARTED = "upload-started"


class FacebookPublisher:
    platform = Platform.FACEBOOK

    def __init__(self, session: requests.Session | None = None, now=lambda: datetime.now(timezone.utc)):
        self.session = session or requests.Session()
        self.now = now

    def verify(self, credentials: dict) -> dict:
        token, page_id = credentials["page_access_token"], credentials["page_id"]
        try:
            resp = self.session.request("GET", f"{GRAPH}/{page_id}", params={"fields": "id,name"},
                                        headers={"Authorization": f"Bearer {token}"}, timeout=30)
        except requests.RequestException as exc:
            raise TransientError(redact(f"Facebook: network problem ({type(exc).__name__})", (token,))) from None
        if resp.status_code != 200:
            raise error_from_response(resp, "Facebook", (token,))
        data = resp.json()
        return {"external_id": str(data["id"]), "display_name": data.get("name") or str(data["id"]), "handle": ""}

    def publish(self, job: PublishJob) -> PublishResult:
        token = job.credentials["page_access_token"]
        page_id = job.credentials.get("page_id") or job.account.external_id
        secrets = (token,)
        if job.dispatch.checkpoint == STARTED and not job.dispatch.remote_id:
            raise PermanentError("Facebook: a previous upload was interrupted and may have been posted. "
                                 "Check the Page, then schedule Facebook again if it isn't there.")

        description = job.post.facebook_text or job.post.description
        data = {"title": job.post.title, "description": description, "access_token": token}
        when = parse_utc(job.dispatch.publish_at)
        scheduled = when is not None and when > self.now() + MIN_LEAD
        if scheduled:
            if when > self.now() + MAX_LEAD:
                raise PermanentError("Facebook can only schedule up to 6 months ahead.")
            data.update(published="false", scheduled_publish_time=str(int(when.timestamp())))

        job.checkpoint(STARTED)
        try:
            with job.video_path.open("rb") as fh:
                resp = self.session.request("POST", f"{GRAPH_VIDEO}/{page_id}/videos", data=data, timeout=1200,
                                            files={"source": (job.video_path.name, fh, "video/mp4")})
        except (requests.ConnectionError, requests.ConnectTimeout) as exc:
            job.checkpoint("")  # never reached Facebook: safe to retry
            raise TransientError(redact(f"Facebook: network problem ({type(exc).__name__})", secrets)) from None
        except requests.RequestException as exc:  # sent, but no answer: may have been created
            raise PermanentError(redact(f"Facebook: the upload's outcome is unknown ({type(exc).__name__}). "
                                        "Check the Page before retrying.", secrets)) from None
        if resp.status_code != 200:
            job.checkpoint("")  # Facebook answered with an error, so nothing was created
            raise error_from_response(resp, "Facebook", secrets)
        video_id = str(resp.json().get("id", ""))
        return PublishResult(DispatchStatus.SCHEDULED if scheduled else DispatchStatus.PUBLISHED,
                             remote_id=video_id, remote_url=f"https://www.facebook.com/{page_id}/videos/{video_id}")
