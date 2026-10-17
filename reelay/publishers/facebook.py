"""Facebook Page video posts via the Graph API.

A vertical video of 3–90 seconds goes up as a **Reel** (the Reels Publishing API):
  1. POST /{page-id}/video_reels  upload_phase=start                → video_id
  2. POST rupload.facebook.com/video-upload/{version}/{video_id}     the file (Authorization: OAuth <token>,
     offset, file_size headers)
  3. POST /{page-id}/video_reels  upload_phase=finish, video_state=PUBLISHED, or SCHEDULED with
     scheduled_publish_time (Reels can be scheduled 10 minutes to 29 days ahead).
Anything else (landscape, longer, or scheduled further out than 29 days) is a regular Page video:
POST /{page-id}/videos (multipart `source`) with title + description, and published=false plus
scheduled_publish_time for a time between 10 minutes and 6 months ahead. Facebook then publishes either on
its own. Needs a Page access token (CREATE_CONTENT on the Page; pages_manage_posts, pages_read_engagement,
pages_show_list).

Neither call has an idempotency key, so Reelay records a checkpoint just before the step that creates the
post (the upload for a Page video, the finish for a Reel). If a retry finds that marker without a result
(the app died mid-request), it fails the platform instead of risking a duplicate. Failures before that step
created nothing, so they're safe to retry.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from ..models import DispatchStatus, Platform
from ..publishing import parse_utc
from ..readiness import is_facebook_reel
from .base import PermanentError, PublishJob, PublishResult, TransientError, error_from_response, redact

GRAPH_VERSION = "v26.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
GRAPH_VIDEO = f"https://graph-video.facebook.com/{GRAPH_VERSION}"
RUPLOAD = f"https://rupload.facebook.com/video-upload/{GRAPH_VERSION}"
MIN_LEAD = timedelta(minutes=10)
MAX_LEAD = timedelta(days=182)
REEL_MAX_LEAD = timedelta(days=29)
STARTED = "upload-started"
REEL_STARTED = "reel-finish-started"
UNSURE = ("Facebook: a previous upload was interrupted and may have been posted. Check the Page, then schedule "
          "Facebook again if it isn't there.")


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
        if job.dispatch.checkpoint in (STARTED, REEL_STARTED) and not job.dispatch.remote_id:
            raise PermanentError(UNSURE)

        description = job.post.facebook_text or job.post.description
        when = parse_utc(job.dispatch.publish_at)
        scheduled = when is not None and when > self.now() + MIN_LEAD
        shape = job.media if job.media is not None else job.item          # the real file, when it's known
        if is_facebook_reel(shape.width, shape.height, shape.duration) and \
                (not scheduled or when <= self.now() + REEL_MAX_LEAD):
            return self._publish_reel(job, page_id, token, description, when if scheduled else None)
        data = {"title": job.post.title, "description": description, "access_token": token}
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

    def _publish_reel(self, job: PublishJob, page_id: str, token: str, description: str, when) -> PublishResult:
        secrets = (token,)
        auth = {"Authorization": f"Bearer {token}"}

        def network(exc) -> TransientError:
            return TransientError(redact(f"Facebook: network problem ({type(exc).__name__})", secrets))

        # 1-2: start a Reel and send the file. Nothing is posted yet, so any failure here is safe to retry.
        try:
            resp = self.session.request("POST", f"{GRAPH}/{page_id}/video_reels", data={"upload_phase": "start"},
                                        headers=auth, timeout=60)
        except requests.RequestException as exc:
            raise network(exc) from None
        if resp.status_code != 200:
            raise error_from_response(resp, "Facebook", secrets)
        video_id = str(resp.json().get("video_id", ""))
        if not video_id:
            raise TransientError("Facebook didn't start the Reel upload. Reelay will try again.")
        try:
            with job.video_path.open("rb") as fh:
                resp = self.session.request("POST", f"{RUPLOAD}/{video_id}", data=fh, timeout=1200, headers={
                    "Authorization": f"OAuth {token}", "offset": "0",
                    "file_size": str(job.video_path.stat().st_size), "Content-Type": "application/octet-stream"})
        except requests.RequestException as exc:
            raise network(exc) from None
        if resp.status_code != 200:
            raise error_from_response(resp, "Facebook", secrets)

        # 3: finish, which makes the post: checkpoint first, so an unanswered request is never repeated.
        data = {"video_id": video_id, "upload_phase": "finish", "title": job.post.title, "description": description,
                "video_state": "SCHEDULED" if when else "PUBLISHED"}
        if when:
            data["scheduled_publish_time"] = str(int(when.timestamp()))
        job.checkpoint(REEL_STARTED)
        try:
            resp = self.session.request("POST", f"{GRAPH}/{page_id}/video_reels", data=data, headers=auth,
                                        timeout=120)
        except (requests.ConnectionError, requests.ConnectTimeout) as exc:
            job.checkpoint("")  # never reached Facebook: safe to retry
            raise network(exc) from None
        except requests.RequestException as exc:  # sent, but no answer: it may be posted
            raise PermanentError(redact(f"Facebook: the Reel's outcome is unknown ({type(exc).__name__}). "
                                        "Check the Page before retrying.", secrets)) from None
        if resp.status_code != 200 or not resp.json().get("success"):
            job.checkpoint("")  # Facebook answered with an error, so nothing was posted
            if resp.status_code != 200:
                raise error_from_response(resp, "Facebook", secrets)
            raise PermanentError("Facebook didn't accept the Reel.")
        return PublishResult(DispatchStatus.SCHEDULED if when else DispatchStatus.PUBLISHED, remote_id=video_id,
                             remote_url=f"https://www.facebook.com/reel/{video_id}")
