"""Reelay Agent API: a local HTTP API for AI agents and automations.

Runs on a background thread inside the desktop app, or headless:

    python -m reelay.api                    # serve (Ctrl-C to stop)
    python -m reelay.api --write-contract   # regenerate api/openapi.yaml

Binds to REELAY_HOST (default 127.0.0.1) and REELAY_PORT (default 8471).
Every /v1 route needs `Authorization: Bearer <token>`; the token lives in the
system keychain (service "Reelay", account "agent_api_token") or comes from
REELAY_API_TOKEN. /openapi.json is public so agent frameworks can turn it
into tools; it contains no secrets.

Everything goes through the Pipeline and Store (tagged origin "api"), so the
desktop board redraws when an agent changes something.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

import uvicorn
from fastapi import Depends, FastAPI, Query, Request, Security
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .accounts import YOUTUBE, AccountService
from .media import MediaLinks, public_base_url
from .models import MANUAL_PLATFORMS, ContentType, Platform, Profile, Stage
from .pipeline import Card, HistoryEntry, Pipeline, PipelineError
from .products import video_products
from .flags import env_var
from .platform_text import (BLUESKY_MAX_GRAPHEMES, FACEBOOK_MAX_CHARS, INSTAGRAM_MAX_CHARS, INSTAGRAM_MAX_HASHTAGS,
                            THREADS_MAX_CHARS, TIKTOK_MAX_CHARS)
from .publishing import TITLE_MAX, parse_utc
from .store import Store

log = logging.getLogger("reelay")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8471
CONTRACT_PATH = Path(__file__).resolve().parent.parent / "api" / "openapi.yaml"

PlatformName = Literal["youtube", "bluesky", "threads", "facebook"]
StageName = Literal["backlog", "staging", "scheduled", "published"]
PublicationStatus = Literal["pending", "dispatching", "scheduled", "published", "failed"]
Visibility = Literal["public", "unlisted", "private"]
SourcePlatform = Literal["tiktok", "instagram", "youtube"]
ManualPlatformName = Literal["youtube", "tiktok", "instagram", "bluesky", "threads", "facebook"]
ContentTypeName = Literal["short", "long-form", "clip", "other"]
assert set(MANUAL_PLATFORMS) == set(ManualPlatformName.__args__) and set(ContentType.ALL) == set(ContentTypeName.__args__)

# PipelineError code → HTTP status
STATUS_FOR_CODE = {
    "not_found": 404,
    "locked": 409, "already_scheduled": 409, "already_uploading": 409, "needs_upload": 409,
    "invalid_post": 422, "invalid_field": 422, "invalid_stage": 422, "invalid_time": 422,
    "invalid_platform": 422, "platform_disabled": 422, "invalid": 422, "ticket_not_ready": 422,
    "invalid_request": 422, "no_transcript": 422,
}


# ====================================================================== schemas
class ErrorBody(BaseModel):
    detail: str = Field(description="What went wrong, in plain words.")
    code: str | None = Field(None, description="Stable machine-readable error code, e.g. 'unauthorized', "
                                               "'not_found', 'invalid_post', 'ticket_not_ready', "
                                               "'already_scheduled', 'locked'.")
    problems: list[str] | None = Field(None, description="Every individual problem found, when there are several.")


ERRORS = {
    400: {"model": ErrorBody, "description": "Bad request (e.g. missing or unknown profile)."},
    401: {"model": ErrorBody, "description": "Missing or invalid bearer token."},
    404: {"model": ErrorBody, "description": "No video with that id."},
    409: {"model": ErrorBody, "description": "Conflicts with the video's current state (already scheduled)."},
    422: {"model": ErrorBody, "description": "Invalid input; nothing was changed."},
}
SCHEDULE_422 = {"model": ErrorBody, "description": (
    "Nothing was scheduled. code=ticket_not_ready: the ticket isn't ready for one or more of the platforms being "
    "scheduled (`problems` lists each one, prefixed with the platform, e.g. \"Bluesky: Write the Bluesky post "
    "…\"). Fix them with PATCH /v1/videos/{video_id}, or repeat the request with ?force=true to schedule anyway. "
    "Other codes (invalid_post, invalid_time, platform_disabled, invalid_platform) can't be forced.")}


class ProfileRef(BaseModel):
    id: int
    name: str = Field(description="Profile name as shown in Reelay's sidebar.")


class Stats(BaseModel):
    views: int | None = Field(None, description="View count on the source platform when downloaded.")
    likes: int | None = None
    comments: int | None = None
    duration_seconds: float | None = None


class ProductOut(BaseModel):
    id: str = Field(description="The platform's product id: the same product has the same id on every video.")
    name: str = Field(description="Short name shown on the video.")
    title: str = Field(description="The product's full listing title (usually starts with the brand).")
    platform: str = Field(description="Where the product is tagged, e.g. tiktok (TikTok Shop).")
    url: str | None = Field(None, description="The product's page, when the platform gives one.")


class SourceInfo(BaseModel):
    platform: str = Field(description="Where the video was originally posted (TikTok, Instagram, YouTube).")
    url: str = Field(description="The original post's URL.")
    caption: str = Field(description="The original caption, in full.")
    hook: str = Field(description="The caption's opening line (what viewers see first).")
    uploaded_on: str | None = Field(None, description="Original upload date, YYYY-MM-DD.")
    creator: str | None = None
    stats: Stats
    products: list[ProductOut] | None = Field(None, description="Products tagged on the original post (TikTok "
                                                                "Shop). [] = none; null = not looked up yet.")


class Overrides(BaseModel):
    """Ready-to-paste copy for each platform. null = not written yet."""
    tiktok_text: str | None = Field(None, description=f"TikTok caption (max {TIKTOK_MAX_CHARS} characters).")
    instagram_text: str | None = Field(None, description=f"Instagram caption (max {INSTAGRAM_MAX_CHARS} characters, "
                                                         f"{INSTAGRAM_MAX_HASHTAGS} hashtags).")
    bluesky_text: str | None = Field(None, description=f"Bluesky text (max {BLUESKY_MAX_GRAPHEMES} characters). Used "
                                                       "instead of the title when posting to Bluesky.")
    threads_text: str | None = Field(None, description=f"Threads text (max {THREADS_MAX_CHARS} characters). Used "
                                                       "instead of the title when posting to Threads.")
    facebook_text: str | None = Field(None, description=f"Facebook post text (max {FACEBOOK_MAX_CHARS} characters). "
                                                        "Used as the Facebook description when posting.")


class ChannelRef(BaseModel):
    id: int = Field(description="Reelay's id for the connected channel.")
    name: str
    handle: str | None = None


class Publication(BaseModel):
    platform: PlatformName
    status: PublicationStatus = Field(description="pending = waiting for its time or a retry; dispatching = being "
                                                  "sent now; scheduled = accepted by the platform, goes live at "
                                                  "the set time; published = live; failed = gave up (see error).")
    publish_at: datetime | None = Field(None, description="When it goes live (UTC); null = as soon as possible.")
    url: str | None = Field(None, description="Link to the post once the platform has it.")
    error: str | None = Field(None, description="Last error for this platform, if any.")
    attempts: int = Field(description="Failed attempts so far (transient errors are retried with backoff).")
    updated_at: datetime


class ReadinessCheck(BaseModel):
    id: str = Field(description="What is checked: title, description, tags, bluesky_text, threads_text, "
                                "facebook_text, length or video.")
    label: str
    state: Literal["pass", "warn"]
    message: str = Field(description="What's fine, or what to fix, in plain words.")


class PlatformReadinessOut(BaseModel):
    state: Literal["ready", "not_ready", "not_checked"] = Field(
        description="not_checked = automatic posting to this platform is off, so it's posted by hand and never "
                    "blocks.")
    ready: bool = Field(description="False only when state is not_ready.")
    auto_post: bool
    checks: list[ReadinessCheck] = Field(description="Empty when not_checked.")


class PlatformStateOut(BaseModel):
    target: bool = Field(description="The video is going to this platform.")
    state: Literal["none", "failed", "scheduled", "sending", "published"] = Field(
        description="none = not scheduled yet; scheduled = waiting to go live (by Reelay, or scheduled by hand in the "
                    "platform's app); sending = Reelay is posting it now; published = live; failed = Reelay gave up.")
    at: datetime | None = Field(None, description="When it goes or went live (UTC).")
    automatic: bool = Field(description="Reelay is posting it itself; its state updates on its own.")


class PlatformMark(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["scheduled", "posted", "none"] = Field(
        description="scheduled = you scheduled it in the platform's own app (give `at`, when it goes live); posted = "
                    "it's live (`at` defaults to now, can't be in the future); none = clear what was marked.")
    at: AwareDatetime | None = None


class TicketFields(BaseModel):
    transcript: str | None = None
    thumbnail_path: str | None = Field(None, description="Local path of the cover image.")
    content_type: ContentTypeName | None = None
    topic: str | None = None
    keywords: str | None = None
    creator: str | None = Field(None, description="Brand or person the content belongs to.")
    original_publish_date: datetime | None = Field(None, description="When the content was first published (UTC).")
    derived_from: int | None = Field(None, description="Id of the video this one was cut from; becomes null if "
                                                       "that video is deleted.")


class Provenance(BaseModel):
    source_platform: str | None = Field(None, description="Platform the video was imported from.")
    source_url: str | None = Field(None, description="Link to the original post.")
    imported_at: datetime | None = Field(None, description="When the video was brought into Reelay (UTC).")
    authorization: str | None = Field(None, description="The right to use it, e.g. \"Own content\" or "
                                                        "\"Managed account\". null = not recorded.")


class HistoryItem(BaseModel):
    method: Literal["automatic", "manual"] = Field(description="automatic = posted by Reelay; manual = posted by "
                                                               "hand and marked as posted.")
    status: str = Field(description="For automatic: pending, dispatching, scheduled, published or failed. For "
                                    "manual: posted.")
    at: datetime | None = Field(None, description="When it went (or goes) live, UTC.")
    url: str | None = None
    error: str | None = None
    note: str | None = None
    id: int = Field(description="Job id (automatic) or manual post id (manual).")


class Video(BaseModel):
    id: int = Field(description="Video id used by the other endpoints.")
    profile: str = Field(description="Profile (creator) the video belongs to.")
    stage: StageName = Field(description="Board column.")
    title: str = Field(description="Title used for YouTube and Facebook (and as default post text).")
    description: str
    tags: list[str]
    target_platforms: list[ManualPlatformName] = Field(description="Where this video is going: platforms Reelay "
                                                       "posts to itself, and ones posted by hand (TikTok, Instagram).")
    overrides: Overrides = Field(description="Per-platform copy (the card's copy blocks).")
    hashtags: str | None = Field(None, description="The card's shared hashtag block, e.g. \"#growth #healing\".")
    youtube_channel: ChannelRef | None = Field(None, description="Connected YouTube channel the video posts to.")
    visibility: Visibility = Field(description="YouTube visibility for immediate posts. Scheduled YouTube videos "
                                               "are uploaded private and go public at scheduled_at.")
    scheduled_at: datetime | None = Field(None, description="When the video goes live (UTC), if scheduled.")
    draft_saved: bool = Field(description="False when title/description/tags are suggestions generated from the "
                                          "original caption and haven't been saved yet.")
    custom_name: str = Field(description="Reelay's custom file name for the video, if set.")
    notes: str
    file_path: str | None = Field(None, description="Local path of the downloaded video file.")
    file_available: bool = Field(description="Whether the video file exists on disk.")
    publications: list[Publication] = Field(description="Per-platform publishing status (empty until scheduled).")
    source: SourceInfo
    ready: bool = Field(description="READY: at least one of the video's target platforms is ready (platforms with "
                                    "automatic posting off count as ready). Scheduling refuses platforms that "
                                    "aren't (422 ticket_not_ready) unless forced.")
    readiness: dict[ManualPlatformName, PlatformReadinessOut] = Field(description="Checklist per target platform.")
    platform_states: dict[ManualPlatformName, PlatformStateOut] = Field(
        description="Every platform's tag: whether the video is going there and how far it has got. The card moves to "
                    "Scheduled once every target is scheduled or live, and to Published once every target is live.")
    ticket: TicketFields
    provenance: Provenance
    archived: bool = Field(False, description="Archived videos are hidden from the board (files and history kept).")
    publishing_history: dict[ManualPlatformName, list[HistoryItem]] = Field(
        description="What went where, when and how: automatic posts and manual ones, oldest first, per platform. "
                    "Platforms with no history are omitted.")
    updated_at: datetime | None = None


class BoardPage(BaseModel):
    profile: ProfileRef
    stage: StageName | None = Field(None, description="The stage filter applied, if any.")
    total: int = Field(description="Videos matching the filter.")
    limit: int
    offset: int
    next_offset: int | None = Field(None, description="Pass as offset to get the next page; null on the last page.")
    videos: list[Video]


class VideoPatch(BaseModel):
    """Only these fields can be changed. Omitted fields are left as they are."""
    model_config = ConfigDict(extra="forbid")

    title: str = Field(None, min_length=1, max_length=TITLE_MAX,
                       description="Title (1-100 characters, no < or >). Used on YouTube and Facebook.")
    description: str = Field(None, description="Description (max 5,000 bytes, no < or >).")
    tags: list[str] = Field(None, description="Tags without '#', 500 characters in total.")
    target_platforms: list[ManualPlatformName] = Field(None, min_length=1,
                                                       description="Where the video is going (any of the six).")
    bluesky_text: str | None = Field(None, description=f"Bluesky post text, max {BLUESKY_MAX_GRAPHEMES} characters "
                                                       "(grapheme clusters, so an emoji counts once). null clears it.")
    threads_text: str | None = Field(None, description=f"Threads post text, max {THREADS_MAX_CHARS} characters "
                                                       "(emoji count as their UTF-8 byte length). null clears it.")
    tiktok_text: str | None = Field(None, description=f"TikTok caption, max {TIKTOK_MAX_CHARS} characters. "
                                                      "null clears it.")
    instagram_text: str | None = Field(None, description=f"Instagram caption, max {INSTAGRAM_MAX_CHARS} characters "
                                                         f"and {INSTAGRAM_MAX_HASHTAGS} hashtags. null clears it.")
    facebook_text: str | None = Field(None, description=f"Facebook post text, max {FACEBOOK_MAX_CHARS} characters. "
                                                        "null clears it.")
    hashtags: str | None = Field(None, description="Shared hashtag block for the card, e.g. \"#growth #healing\". "
                                                   "null clears it.")
    transcript: str | None = Field(None, description="Full transcript. null clears it. Ticket fields like this "
                                                     "can be edited at any stage.")
    thumbnail_path: str | None = Field(None, description="Local path of the cover image.")
    content_type: ContentTypeName | None = None
    topic: str | None = Field(None, max_length=200)
    keywords: str | None = Field(None, max_length=1000)
    creator: str | None = Field(None, max_length=200, description="Brand or person name.")
    original_publish_date: AwareDatetime | None = Field(None, description="ISO 8601 with a timezone.")
    source_platform: SourcePlatform | None = None
    source_url: str | None = Field(None, max_length=2048)
    authorization: str | None = Field(None, max_length=200, description="e.g. \"Own content\", \"Managed account\".")
    derived_from: int | None = Field(None, description="Id of the video this one was cut from (same profile), or "
                                                       "null.")


class BulkUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_ids: list[int] = Field(min_length=1, max_length=500)
    changes: VideoPatch = Field(description="The same fields PATCH /v1/videos/{video_id} accepts.")


class BulkResult(BaseModel):
    changed: int = Field(description="Tickets updated. All or nothing: on any problem nothing is written (422).")


class VideoIds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_ids: list[int] = Field(min_length=1, max_length=500)


class FillResult(BaseModel):
    filled: dict[int, list[str]] = Field(description="Per video id, the fields filled from the original post. "
                                                     "Videos with nothing to fill are left out.")


class TranscriptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replace: bool = Field(False, description="Replace an existing transcript.")


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    csv: str = Field(min_length=1, max_length=5_000_000, description="The sheet as CSV (or TSV) text, header row "
                                                                     "first.")
    mapping: dict[str, str | None] | None = Field(
        None, description="Column header → field (title, description, tags, target_platforms, tiktok_text, "
                          "instagram_text, bluesky_text, threads_text, facebook_text, hashtags, transcript, topic, "
                          "keywords, creator, content_type, original_publish_date, authorization, thumbnail_path) "
                          "or a match key (match_url, match_id, match_file, match_name); null ignores the column. "
                          "Omit to let Reelay guess from the headers.")
    overwrite: bool = Field(False, description="false: only fill empty fields. true: replace what's there.")


class ImportRowOut(BaseModel):
    row: int = Field(description="Sheet row number (the header is row 1).")
    video_id: int | None = None
    matched_by: str | None = None
    fields: list[str] = Field(description="Fields this row sets.")
    kept: list[str] = Field(description="Fields left as they were (already filled, or already scheduled).")
    problems: list[str] = Field(description="Why the row is skipped, if it is.")


class ImportReport(BaseModel):
    applied: bool = Field(description="false for a dry run.")
    changed: int = Field(description="Tickets changed (or that would change).")
    mapping: dict[str, str | None]
    summary: str
    rows: list[ImportRowOut]


class SourcePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=80)
    mine: bool | None = None


class RevisionOut(BaseModel):
    id: int
    at: datetime
    origin: str = Field(description="Who made the change: local = the Reelay app, api = an agent through this API.")
    field: str
    old_value: str | None = Field(None, description="Previous value (lists as JSON text).")
    new_value: str | None = None


class RevisionsResponse(BaseModel):
    video_id: int
    revisions: list[RevisionOut] = Field(description="Every logged change to the ticket, newest first.")


class ManualPostCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: ManualPlatformName
    posted_at: AwareDatetime | None = Field(None, description="When it went live; omit for now. Can't be in the "
                                                              "future.")
    url: str | None = Field(None, max_length=2048, description="Link to the live post, if known.")
    note: str | None = Field(None, max_length=500)


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_at: AwareDatetime | None = Field(
        None, description="When to go live, ISO 8601 with a timezone, at least 10 minutes ahead. Omit to use the "
                          "card's saved time, or publish as soon as possible if it has none.")
    platforms: list[PlatformName] | None = Field(
        None, min_length=1, description="Platforms for this request; each must have automatic posting enabled "
                                        "(422 platform_disabled otherwise). Omit to use the video's "
                                        "target_platforms that are enabled; the rest are reported as manual.")


class QueueJob(BaseModel):
    platform: PlatformName
    job_id: int = Field(description="Id of this platform's publishing job.")
    status: PublicationStatus
    due_at: datetime = Field(description="When Reelay sends it. YouTube and Facebook are sent right away and "
                                         "scheduled on the platform; Bluesky and Threads are sent at publish time.")
    publish_at: datetime | None = None
    queue_position: int | None = Field(None, description="Place in line: 0 = being processed now, null = not "
                                                         "waiting. YouTube uploads run one at a time.")


class ScheduleResponse(BaseModel):
    video_id: int
    stage: StageName
    scheduled_at: datetime | None = Field(None, description="When it goes live (UTC); null = as soon as possible.")
    target_platforms: list[PlatformName] = Field(description="Platforms Reelay will post to.")
    manual_platforms: list[ManualPlatformName] = Field(default_factory=list, description="The video's other targets, "
                                                 "which have automatic posting turned off: post these by hand.")
    jobs: list[QueueJob]


class PlatformAccount(BaseModel):
    name: str
    handle: str | None = None
    external_id: str = Field(description="The platform's id for the account (channel id, DID, user id, Page id).")
    credential_present: bool = Field(description="A credential is stored in the system keychain. This is not a "
                                                 "live check; see last_verified_at.")
    last_verified_at: datetime | None = Field(None, description="Last time the credential worked against the "
                                                                "platform (sign-in check or successful post). "
                                                                "null = never verified.")


class PlatformInfo(BaseModel):
    platform: PlatformName
    connected: bool = Field(description="True when at least one account has a credential in the keychain. "
                                        "\"Connected\" means a credential is present, not that it was verified "
                                        "recently: check last_verified_at.")
    auto_post: bool = Field(description="Whether Reelay may post to this platform automatically. When false, "
                                        "scheduling to it is rejected and its copy is posted by hand. YouTube is "
                                        "always true; the others follow REELAY_PUBLISH_<PLATFORM> or Settings.")
    accounts: list[PlatformAccount]
    setup_issue: str | None = Field(None, description="What still needs setting up before posting works, if anything.")


class PlatformsResponse(BaseModel):
    profile: ProfileRef
    platforms: list[PlatformInfo]


class SourceAccount(BaseModel):
    id: int = Field(description="Reelay's id for the connected account.")
    platform: Literal["youtube"]
    name: str
    handle: str | None = None


class SourceOut(BaseModel):
    id: int | None = Field(None, description="Saved source id; null for a connected account.")
    kind: Literal["saved", "account"] = Field(description="saved = a profile URL saved in Reelay (downloaded via "
                                                          "the link). account = a connected YouTube channel, listed "
                                                          "through the YouTube Data API (no link needed).")
    name: str
    platform: SourcePlatform
    url: str | None = Field(None, description="Profile URL (for accounts: the channel's URL, for reference).")
    mine: bool = Field(description="One of this profile's own accounts.")
    account: SourceAccount | None = Field(None, description="The connected account behind an account source.")
    last_fetched_at: datetime | None = Field(None, description="Last time this saved source was fetched.")


class SourcesResponse(BaseModel):
    profile: ProfileRef
    sources: list[SourceOut] = Field(description="Every source: connected YouTube channels first, then saved "
                                                 "sources. Duplicate URLs are listed as stored (not merged).")


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, description="Public TikTok, Instagram or YouTube profile URL.")
    name: str | None = Field(None, max_length=80, description="Display name. Defaults to the handle in the URL, "
                                                              "e.g. \"@livingood.lifestyle on TikTok\".")
    mine: bool = Field(False, description="Mark as one of this profile's own accounts.")


# ====================================================================== errors
class ApiError(Exception):
    def __init__(self, status: int, detail: str, code: str | None = None, problems: list[str] | None = None):
        self.status, self.detail, self.code, self.problems = status, detail, code, problems


def _error(status: int, detail: str, code: str | None = None, problems: list[str] | None = None,
           headers: dict | None = None) -> JSONResponse:
    body = ErrorBody(detail=detail, code=code, problems=problems).model_dump(exclude_none=True)
    return JSONResponse(body, status_code=status, headers=headers)


def _validation_message(err: dict) -> str:
    loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body",)) or "request"
    return f"{loc}: {err.get('msg', 'invalid')}"


# ====================================================================== app
def create_app(store: Store, pipeline: Pipeline, accounts: AccountService, token_provider: Callable[[], str],
               media: MediaLinks | None = None) -> FastAPI:
    app = FastAPI(
        title="Reelay Agent API",
        version=__version__,
        description="Read a creator's video board (each video is a content ticket with a readiness checklist), "
                    "apply approved edits, record manual posts, and schedule posts to YouTube, Bluesky, Threads and "
                    "Facebook. All /v1 routes need `Authorization: Bearer <token>`. Changes "
                    "show up immediately on the Reelay desktop board.",
        docs_url=None, redoc_url=None,
    )
    media = media or pipeline.media
    bearer = HTTPBearer(auto_error=False, description="Agent API token (system keychain: service \"Reelay\", "
                                                      "account \"agent_api_token\"; or REELAY_API_TOKEN).")

    def require_token(creds: HTTPAuthorizationCredentials | None = Security(bearer)) -> None:
        expected = token_provider()
        if creds is None or creds.scheme.lower() != "bearer" or not expected or \
                not secrets.compare_digest(creds.credentials.encode(), expected.encode()):
            raise ApiError(401, "Missing or invalid bearer token.", "unauthorized")

    # ---------------------------------------------------------------- error handlers
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return _error(exc.status, exc.detail, exc.code, exc.problems, headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError):
        errors = exc.errors()
        if any(e.get("loc") == ("query", "profile") for e in errors):
            return _error(400, "The profile query parameter is required (a profile name from Reelay's sidebar).",
                          "profile_required")
        problems = [_validation_message(e) for e in errors]
        return _error(422, problems[0] if len(problems) == 1 else "The request has invalid fields.",
                      "invalid_request", problems)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException):
        return _error(exc.status_code, str(exc.detail), None)

    @app.exception_handler(Exception)
    async def _unexpected(_request: Request, exc: Exception):
        log.exception("Agent API error")
        return _error(500, "Internal error; see the Reelay log.", "internal")

    # ---------------------------------------------------------------- helpers
    def find_profile(name: str) -> Profile:
        name = (name or "").strip()
        matches = [p for p in store.list_profiles() if p.name.casefold() == name.casefold()]
        if not matches and name.isdigit():
            profile = store.get_profile(int(name))
            matches = [profile] if profile else []
        if len(matches) != 1:
            known = ", ".join(p.name for p in store.list_profiles())
            raise ApiError(400, f"Unknown profile “{name}”. Known profiles: {known}." if not matches
                           else f"More than one profile is named “{name}”.", "unknown_profile")
        return matches[0]

    def to_video(card: Card, profile: Profile, channels: dict) -> Video:
        row, item = card.row, card.row.item
        post, saved = (card.post, True) if card.post else pipeline.post_or_default(row)
        path = item.path(profile.output_folder)
        channel = channels.get(post.account_id)
        date = item.upload_date
        return Video(
            id=row.id, profile=profile.name, stage=row.stage, title=post.title, description=post.description,
            tags=post.tags, target_platforms=post.target_platforms,
            overrides=Overrides(tiktok_text=post.tiktok_text, instagram_text=post.instagram_text,
                                bluesky_text=post.bluesky_text, threads_text=post.threads_text,
                                facebook_text=post.facebook_text),
            hashtags=post.hashtags,
            youtube_channel=ChannelRef(id=channel.id, name=channel.display_name, handle=channel.handle or None)
            if channel else None,
            visibility=post.privacy, scheduled_at=parse_utc(post.publish_at), draft_saved=saved,
            custom_name=item.custom_name, notes=row.notes,
            file_path=str(path) if path else None, file_available=bool(path and path.is_file()),
            publications=[Publication(platform=d.platform, status=d.status, publish_at=parse_utc(d.publish_at),
                                      url=d.remote_url or None, error=d.error or None, attempts=d.attempts,
                                      updated_at=parse_utc(d.updated_at)) for d in card.dispatches],
            ready=card.readiness.ready if card.readiness else True,
            readiness={name: PlatformReadinessOut(state=r.state, ready=r.ready, auto_post=r.auto_post,
                                                  checks=[ReadinessCheck(id=c.id, label=c.label, state=c.state,
                                                                         message=c.message) for c in r.checks])
                       for name, r in (card.readiness.platforms.items() if card.readiness else [])},
            ticket=TicketFields(**{f: getattr(row.ticket, f) for f in TicketFields.model_fields
                                   if f != "original_publish_date"},
                                original_publish_date=parse_utc(row.ticket.original_publish_date)),
            provenance=Provenance(source_platform=row.ticket.source_platform, source_url=row.ticket.source_url,
                                  imported_at=parse_utc(row.ticket.imported_at),
                                  authorization=row.ticket.authorization),
            archived=bool(row.archived_at),
            platform_states={p: PlatformStateOut(target=s.target, state=s.state, at=parse_utc(s.at) if s.at else None,
                                                 automatic=s.automatic)
                             for p, s in (card.platform_states or pipeline.platform_states(row.id)).items()},
            publishing_history={p: [to_history(e) for e in entries]
                                for p, entries in pipeline.publishing_history(row.id).items()},
            source=SourceInfo(platform=item.platform, url=item.url, caption=item.description, hook=item.hook,
                              uploaded_on=f"{date[:4]}-{date[4:6]}-{date[6:]}" if len(date) == 8 else None,
                              creator=item.uploader or None,
                              stats=Stats(views=item.view_count, likes=item.like_count,
                                          comments=item.comment_count, duration_seconds=item.duration),
                              products=None if item.products is None else [
                                  ProductOut(id=p.id, name=p.name, title=p.title, platform=p.platform,
                                             url=p.url or None) for p in video_products(item)]),
            updated_at=parse_utc(row.updated_at),
        )

    def to_history(e: HistoryEntry) -> HistoryItem:
        return HistoryItem(method=e.method, status=e.status, at=parse_utc(e.at), url=e.url or None,
                           error=e.error or None, note=e.note or None, id=e.id)

    def video_by_id(video_id: int) -> Video:
        try:
            card = pipeline.card(video_id)
        except PipelineError as exc:
            raise ApiError(404, f"No video with id {video_id}.", exc.code) from None
        profile = store.get_profile(card.row.profile_id)
        channels = {a.id: a for a in store.list_accounts(profile.id, YOUTUBE)}
        return to_video(card, profile, channels)

    def pipeline_error(exc: PipelineError) -> ApiError:
        status = STATUS_FOR_CODE.get(exc.code, 422)
        if status == 404:
            return ApiError(404, str(exc), exc.code)
        return ApiError(status, str(exc), exc.code, exc.problems if len(exc.problems) > 1 or
                        exc.problems[0] != str(exc) else None)

    # ---------------------------------------------------------------- routes
    v1 = dict(dependencies=[Depends(require_token)], tags=["videos"])

    @app.get("/v1/board", response_model=BoardPage, operation_id="getBoard",
             summary="List a profile's videos on the board", responses={k: ERRORS[k] for k in (400, 401, 422)},
             **v1)
    def get_board(
        profile: str = Query(..., description="Profile name exactly as shown in Reelay's sidebar."),
        stage: StageName | None = Query(None, description="Only this board column."),
        limit: int = Query(50, ge=1, le=200, description="Page size (1-200)."),
        offset: int = Query(0, ge=0, description="Videos to skip (from next_offset of the previous page)."),
        archived: bool = Query(False, description="true lists archived videos instead."),
    ) -> BoardPage:
        """Videos in board order (Backlog → Staging → Scheduled → Published), with their post metadata, target
        platforms, per-platform status, and the original caption and stats. Videos fetched but not yet
        downloaded aren't on the board, and archived videos only appear with archived=true."""
        prof = find_profile(profile)
        if archived:
            cards = [pipeline.card(r.id) for r in pipeline.archived(prof.id) if stage in (None, r.stage)]
        else:
            columns = pipeline.board(prof.id)
            cards = [c for s in Stage.ALL if stage in (None, s) for c in columns[s]]
        channels = {a.id: a for a in store.list_accounts(prof.id, YOUTUBE)}
        page = cards[offset:offset + limit]
        return BoardPage(profile=ProfileRef(id=prof.id, name=prof.name), stage=stage, total=len(cards),
                         limit=limit, offset=offset,
                         next_offset=offset + limit if offset + limit < len(cards) else None,
                         videos=[to_video(c, prof, channels) for c in page])

    @app.patch("/v1/videos/{video_id}", response_model=Video, operation_id="updateVideo",
               summary="Update a video's post metadata", responses={k: ERRORS[k] for k in (401, 404, 409, 422)},
               **v1)
    def update_video(video_id: int, patch: VideoPatch) -> Video:
        """Partial update of the ticket: its copy (title, description, tags, target_platforms, tiktok_text,
        instagram_text, bluesky_text, threads_text, facebook_text, hashtags) and its fields (transcript,
        thumbnail_path, content_type, topic, keywords, creator, original_publish_date, source_platform, source_url,
        authorization, derived_from). Everything is validated first; if anything is invalid nothing is written
        (422). Over-limit text is rejected, never shortened. Copy can't change once the video is scheduled or
        published (409); ticket fields can. Each changed field is logged (GET /v1/videos/{video_id}/revisions).
        Returns the updated video."""
        changes = patch.model_dump(exclude_unset=True)
        if "original_publish_date" in changes and changes["original_publish_date"] is not None:
            changes["original_publish_date"] = changes["original_publish_date"].isoformat()
        if not changes:
            raise ApiError(422, "Send at least one field to change.", "invalid_request")
        with store.origin("api"):
            try:
                pipeline.update_metadata(video_id, changes)
            except PipelineError as exc:
                raise pipeline_error(exc) from None
        return video_by_id(video_id)

    @app.post("/v1/videos/{video_id}/schedule", response_model=ScheduleResponse, operation_id="scheduleVideo",
              summary="Schedule or publish a video to its platforms",
              responses={**{k: ERRORS[k] for k in (401, 404, 409)}, 422: SCHEDULE_422}, **v1)
    def schedule_video(video_id: int, request: ScheduleRequest | None = None,
                       force: bool = Query(False, description="true schedules even if the ticket isn't ready "
                                                              "(skips the Ready gate). Platform rules that would make "
                                                              "the post fail still apply.")) -> ScheduleResponse:
        """Queues automatic posting through Reelay's pipeline. YouTube uploads join the app's upload queue (one at
        a time) and are scheduled on YouTube. Bluesky, Threads and Facebook post automatically only when enabled
        (see GET /v1/platforms auto_post); otherwise their copy is posted by hand. The Ready gate refuses any
        platform whose checklist isn't passing (422 ticket_not_ready, see the video's `readiness`) unless
        force=true. 409 if any requested platform is already scheduled or published."""
        request = request or ScheduleRequest()
        with store.origin("api"):
            try:
                result = pipeline.schedule(video_id, scheduled_at=request.scheduled_at,
                                           platforms=list(request.platforms) if request.platforms else None,
                                           force=force)
            except PipelineError as exc:
                raise pipeline_error(exc) from None
        targets = [d.platform for d in result.dispatches
                   if d.platform in (request.platforms or result.post.target_platforms) and d.platform not in result.manual]
        jobs = [QueueJob(platform=d.platform, job_id=d.id, status=d.status, due_at=parse_utc(d.due_at),
                         publish_at=parse_utc(d.publish_at), queue_position=pipeline.queue_position(d))
                for d in result.dispatches if d.platform in targets]
        row = store.get_video(video_id)
        return ScheduleResponse(video_id=video_id, stage=row.stage, scheduled_at=parse_utc(result.post.publish_at),
                                target_platforms=targets, manual_platforms=result.manual, jobs=jobs)

    @app.get("/v1/videos/{video_id}/revisions", response_model=RevisionsResponse, operation_id="getRevisions",
             summary="A ticket's change history", responses={k: ERRORS[k] for k in (401, 404, 422)}, **v1)
    def get_revisions(video_id: int) -> RevisionsResponse:
        """Every saved change to the ticket's copy and fields: when, who (local app or api), which field, and the
        old and new values. Newest first. Typing isn't logged, only saves."""
        if store.get_video(video_id) is None:
            raise ApiError(404, f"No video with id {video_id}.", "not_found")
        return RevisionsResponse(video_id=video_id, revisions=[
            RevisionOut(id=r.id, at=parse_utc(r.at), origin=r.origin, field=r.field, old_value=r.old_value,
                        new_value=r.new_value) for r in store.revisions(video_id)])

    @app.post("/v1/videos/{video_id}/manual-posts", response_model=HistoryItem, status_code=201,
              operation_id="markPosted", summary="Record that a video was posted by hand",
              responses={k: ERRORS[k] for k in (401, 404, 422)}, **v1)
    def mark_posted(video_id: int, body: ManualPostCreate) -> HistoryItem:
        """For the copy-paste workflow: record that the video went out on a platform, so its publishing history is
        complete. Doesn't post anything or move the card."""
        with store.origin("api"):
            try:
                manual = pipeline.mark_posted(video_id, body.platform, body.posted_at, body.url or "", body.note or "")
            except PipelineError as exc:
                raise pipeline_error(exc) from None
        return HistoryItem(method="manual", status="posted", at=parse_utc(manual.posted_at), url=manual.url or None,
                           note=manual.note or None, id=manual.id)

    @app.post("/v1/videos/bulk-update", response_model=BulkResult, operation_id="bulkUpdateVideos",
              summary="Apply the same edit to many videos", responses={k: ERRORS[k] for k in (401, 404, 422)}, **v1)
    def bulk_update(body: BulkUpdate) -> BulkResult:
        """Same rules as PATCH /v1/videos/{video_id}, for many videos at once, in one transaction: if any video
        has a problem, nothing is written and every problem is listed (422)."""
        changes = body.changes.model_dump(exclude_unset=True)
        if not changes:
            raise ApiError(422, "Send at least one field to change.", "invalid_request")
        if "original_publish_date" in changes and changes["original_publish_date"] is not None:
            changes["original_publish_date"] = changes["original_publish_date"].isoformat()
        with store.origin("api"):
            try:
                changed = pipeline.bulk_update(body.video_ids, changes)
            except PipelineError as exc:
                raise pipeline_error(exc) from None
        return BulkResult(changed=changed)

    @app.post("/v1/videos/fill-from-original", response_model=FillResult, operation_id="fillFromOriginal",
              summary="Fill empty copy from the original posts", responses={k: ERRORS[k] for k in (401, 404, 422)},
              **v1)
    def fill_from_original(body: VideoIds) -> FillResult:
        """Fills each video's EMPTY copy blocks (and creator) from its original caption, within every platform's
        limits; text that doesn't fit is left empty, never shortened. Copy of scheduled or published videos is
        untouched. One transaction; each change is logged."""
        with store.origin("api"):
            try:
                return FillResult(filled=pipeline.fill_from_original(body.video_ids))
            except PipelineError as exc:
                raise pipeline_error(exc) from None

    @app.post("/v1/videos/{video_id}/platforms/{platform}", response_model=Video, operation_id="markPlatform",
              summary="Tick a platform as scheduled or posted (by hand)",
              responses={k: ERRORS[k] for k in (401, 404, 409, 422)}, **v1)
    def mark_platform(video_id: int, platform: ManualPlatformName, body: PlatformMark) -> Video:
        """The ticket's per-platform checkboxes, for posts made in the platform's own app. Marking makes the
        platform a target if it wasn't. Then the card moves on its own: to Scheduled when every target is
        scheduled or live, to Published when every target is live (a scheduled time passing counts). 409 for a
        platform Reelay is posting itself."""
        with store.origin("api"):
            try:
                pipeline.set_platform_state(video_id, platform, body.state, body.at)
            except PipelineError as exc:
                raise pipeline_error(exc) from None
        return video_by_id(video_id)

    @app.post("/v1/videos/{video_id}/archive", response_model=Video, operation_id="archiveVideo",
              summary="Archive a video", responses={k: ERRORS[k] for k in (401, 404, 409, 422)}, **v1)
    def archive_video(video_id: int) -> Video:
        """Hides the video from the board and the Download page; its file, ticket and history are kept. 409 if it's
        scheduled or uploading."""
        with store.origin("api"):
            try:
                pipeline.archive([video_id], True)
            except PipelineError as exc:
                raise pipeline_error(exc) from None
        return video_by_id(video_id)

    @app.post("/v1/videos/{video_id}/unarchive", response_model=Video, operation_id="unarchiveVideo",
              summary="Bring an archived video back", responses={k: ERRORS[k] for k in (401, 404, 422)}, **v1)
    def unarchive_video(video_id: int) -> Video:
        with store.origin("api"):
            try:
                pipeline.archive([video_id], False)
            except PipelineError as exc:
                raise pipeline_error(exc) from None
        return video_by_id(video_id)

    @app.post("/v1/videos/{video_id}/revisions/{revision_id}/restore", response_model=Video,
              operation_id="restoreRevision", summary="Undo one logged change",
              responses={k: ERRORS[k] for k in (401, 404, 409, 422)}, **v1)
    def restore_revision(video_id: int, revision_id: int) -> Video:
        """Puts the field back to the value it had before that change. The restore is itself a new, logged
        change. Same rules as PATCH (copy is locked once scheduled)."""
        with store.origin("api"):
            try:
                pipeline.restore_revision(video_id, revision_id)
            except PipelineError as exc:
                raise pipeline_error(exc) from None
        return video_by_id(video_id)

    @app.post("/v1/videos/{video_id}/transcript", response_model=Video, operation_id="fetchTranscript",
              summary="Get the transcript from the original post's captions",
              responses={k: ERRORS[k] for k in (401, 404, 422)}, **v1)
    def fetch_transcript(video_id: int, body: TranscriptRequest | None = None) -> Video:
        """Downloads the original post's captions (creator subtitles first, else automatic captions) and saves
        them as the ticket's transcript. Network call; takes a few seconds. 422 no_transcript when the post has
        no captions; 422 invalid_post when a transcript exists and replace is false."""
        from .ytdl import base_options
        row = store.get_video(video_id)
        if row is None:
            raise ApiError(404, f"No video with id {video_id}.", "not_found")
        profile = store.get_profile(row.profile_id)
        with store.origin("api"):
            try:
                pipeline.fetch_transcript(video_id, base_options(profile.cookies_browser or None),
                                          replace_existing=bool(body and body.replace))
            except PipelineError as exc:
                raise pipeline_error(exc) from None
        return video_by_id(video_id)

    @app.post("/v1/import", response_model=ImportReport, operation_id="importSheet",
              summary="Import a spreadsheet (CSV) into tickets", tags=["import"],
              dependencies=[Depends(require_token)], responses={k: ERRORS[k] for k in (400, 401, 422)})
    def import_sheet(body: ImportRequest,
                     profile: str = Query(..., description="Profile name exactly as shown in Reelay's sidebar."),
                     dry_run: bool = Query(True, description="true (default) only reports what would change.")
                     ) -> ImportReport:
        """Matches each row to a video (by link, video ID, file name or your name for it), maps columns to ticket
        fields, and fills them. Empty cells never clear anything; over-limit or invalid rows are skipped and
        reported. With dry_run=false every changing row is written in one transaction, each change logged."""
        from .sheet_import import FIELDS, MATCH_KEYS, SheetError, guess_mapping, read_csv
        prof = find_profile(profile)
        try:
            headers, rows = read_csv(body.csv)
        except SheetError as exc:
            raise ApiError(422, str(exc), "invalid_sheet") from None
        mapping = guess_mapping(headers) if body.mapping is None else {h: body.mapping.get(h) for h in headers}
        bad = sorted({t for t in mapping.values() if t and t not in FIELDS and t not in MATCH_KEYS})
        if bad:
            raise ApiError(422, f"Unknown field(s) in mapping: {', '.join(bad)}.", "invalid_request")
        plan = pipeline.plan_sheet(prof.id, headers, rows, mapping, body.overwrite)
        changed = len(plan.changing)
        if not dry_run and changed:
            with store.origin("api"):
                pipeline.apply_sheet(plan)
        return ImportReport(applied=not dry_run, changed=changed, mapping=mapping, summary=plan.summary(),
                            rows=[ImportRowOut(row=r.number, video_id=r.video_id, matched_by=r.matched_by or None,
                                               fields=r.fields, kept=r.kept, problems=r.problems)
                                  for r in plan.rows])

    @app.get("/v1/platforms", response_model=PlatformsResponse, operation_id="getPlatforms",
             summary="Which platforms are connected for a profile", tags=["platforms"],
             dependencies=[Depends(require_token)], responses={k: ERRORS[k] for k in (400, 401, 422)})
    def get_platforms(profile: str = Query(..., description="Profile name exactly as shown in Reelay's sidebar.")
                      ) -> PlatformsResponse:
        """Credential presence and account names per platform. Never returns secrets. "connected" means a
        credential is stored in the keychain, not that it was checked recently (see last_verified_at)."""
        prof = find_profile(profile)
        infos = []
        for platform in Platform.ALL:
            listed = store.list_accounts(prof.id, platform)
            entries = [PlatformAccount(name=a.display_name, handle=a.handle or None, external_id=a.external_id,
                                       credential_present=accounts.has_credentials(a),
                                       last_verified_at=parse_utc(a.last_verified_at)) for a in listed]
            issue = None
            auto_post = platform in pipeline.enabled_platforms()
            if not auto_post:
                issue = (f"Automatic posting is off (copy-paste only). Turn it on with {env_var(platform)}=1 or "
                         "Reelay → Settings → Automatic posting, then restart Reelay.")
            elif platform == Platform.YOUTUBE and accounts.oauth_client() is None:
                issue = "Import Google API credentials in Reelay → Accounts."
            elif not listed:
                issue = f"No {Platform.LABELS[platform]} account connected in Reelay → Accounts."
            elif not any(e.credential_present for e in entries):
                issue = "The stored sign-in is missing from the keychain; reconnect the account."
            elif platform == Platform.THREADS and not public_base_url(store):
                issue = "Set REELAY_PUBLIC_URL to your Cloudflare Tunnel address so Threads can fetch videos."
            infos.append(PlatformInfo(platform=platform, connected=any(e.credential_present for e in entries),
                                      auto_post=auto_post, accounts=entries, setup_issue=issue))
        return PlatformsResponse(profile=ProfileRef(id=prof.id, name=prof.name), platforms=infos)

    # ---------------------------------------------------------------- download sources
    from .sources import SourceError, SourceService
    sources = SourceService(store, accounts)

    def to_source(view) -> SourceOut:
        acc = view.account
        return SourceOut(id=view.source_id, kind=view.kind, name=view.name, platform=view.platform, url=view.url,
                         mine=view.mine,
                         account=SourceAccount(id=acc.id, platform="youtube", name=acc.display_name,
                                               handle=acc.handle or None) if acc else None,
                         last_fetched_at=parse_utc(view.last_fetched_at) if view.kind == "saved" else None)

    @app.get("/v1/sources", response_model=SourcesResponse, operation_id="getSources",
             summary="List a profile's download sources", tags=["sources"], dependencies=[Depends(require_token)],
             responses={k: ERRORS[k] for k in (400, 401, 422)})
    def get_sources(profile: str = Query(..., description="Profile name exactly as shown in Reelay's sidebar.")
                    ) -> SourcesResponse:
        """Where this profile's videos are downloaded from: connected YouTube channels (listed through the
        YouTube Data API) and saved TikTok / Instagram / YouTube profile URLs. Downloading pulls videos in;
        publishing out is separate (see /v1/platforms)."""
        prof = find_profile(profile)
        return SourcesResponse(profile=ProfileRef(id=prof.id, name=prof.name),
                               sources=[to_source(v) for v in sources.all_sources(prof.id)])

    @app.post("/v1/sources", response_model=SourceOut, status_code=201, operation_id="addSource",
              summary="Save a download source for a profile", tags=["sources"],
              dependencies=[Depends(require_token)], responses={k: ERRORS[k] for k in (400, 401, 422)})
    def add_source(body: SourceCreate,
                   profile: str = Query(..., description="Profile name exactly as shown in Reelay's sidebar.")
                   ) -> SourceOut:
        """Saves a TikTok, Instagram or YouTube profile URL so it can be fetched in one click on the Download
        page. Saving the same URL twice is allowed (the Download page shows it once)."""
        prof = find_profile(profile)
        with store.origin("api"):
            try:
                saved = sources.add(prof.id, body.url, body.name, body.mine)
            except SourceError as exc:
                raise ApiError(422, str(exc), "invalid_source") from None
        view = next(v for v in sources.all_sources(prof.id) if v.source_id == saved.id)
        return to_source(view)

    def source_by_id(source_id: int):
        view = next((v for v in sources.all_sources(store.get_source(source_id).profile_id)
                     if v.source_id == source_id), None) if store.get_source(source_id) else None
        if view is None:
            raise ApiError(404, f"No saved source with id {source_id}.", "not_found")
        return view

    @app.patch("/v1/sources/{source_id}", response_model=SourceOut, operation_id="updateSource",
               summary="Rename a saved source or mark it as yours", tags=["sources"],
               dependencies=[Depends(require_token)], responses={k: ERRORS[k] for k in (401, 404, 422)})
    def update_source(source_id: int, body: SourcePatch) -> SourceOut:
        """Saved sources only (connected channels are managed in Reelay → Accounts)."""
        source_by_id(source_id)
        changes = body.model_dump(exclude_unset=True)
        if not changes:
            raise ApiError(422, "Send name and/or mine.", "invalid_request")
        with store.origin("api"):
            try:
                if "name" in changes:
                    sources.rename(source_id, changes["name"] or "")
                if changes.get("mine") is not None:
                    sources.set_mine(source_id, changes["mine"])
            except SourceError as exc:
                raise ApiError(422, str(exc), "invalid_source") from None
        return to_source(source_by_id(source_id))

    @app.delete("/v1/sources/{source_id}", status_code=204, operation_id="deleteSource",
                summary="Remove a saved source", tags=["sources"], dependencies=[Depends(require_token)],
                responses={k: ERRORS[k] for k in (401, 404, 422)})
    def delete_source(source_id: int) -> Response:
        """Removes the saved link only. Videos already listed or downloaded from it stay."""
        source_by_id(source_id)
        with store.origin("api"):
            sources.remove(source_id)
        return Response(status_code=204)

    # ---------------------------------------------------------------- media links (Threads)
    @app.api_route("/media/{token}", methods=["GET", "HEAD"], include_in_schema=False)
    def media_file(token: str):
        path = media.resolve(token)
        if path is None or not path.is_file():
            raise ApiError(404, "This link has expired or doesn't exist.", "not_found")
        return FileResponse(path, media_type="video/mp4")

    return app


# ====================================================================== server
def configured_address() -> tuple[str, int]:
    host = os.environ.get("REELAY_HOST", DEFAULT_HOST)
    try:
        port = int(os.environ.get("REELAY_PORT", DEFAULT_PORT))
    except ValueError:
        log.warning("Ignoring invalid REELAY_PORT; using %d", DEFAULT_PORT)
        port = DEFAULT_PORT
    return host, port


class ApiServer:
    """uvicorn on a background thread, stoppable from any thread."""

    def __init__(self, app: FastAPI, host: str | None = None, port: int | None = None):
        default_host, default_port = configured_address()
        self.host = host or default_host
        self.port = default_port if port is None else port
        self.app = app
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return bool(self._server and self._server.started and self._thread and self._thread.is_alive())

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 10.0) -> bool:
        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_config=None, access_log=False,
                                lifespan="off", server_header=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="reelay-api", daemon=True)
        self._thread.start()
        deadline = threading.Event()
        for _ in range(int(timeout * 20)):
            if self._server.started or not self._thread.is_alive():
                break
            deadline.wait(0.05)
        if not self.running:
            self.error = f"Couldn't start the agent API on {self.host}:{self.port} (is the port already in use?)."
            log.error(self.error)
            return False
        if self.port == 0 and self._server.servers:
            self.port = self._server.servers[0].sockets[0].getsockname()[1]
        if self.host == "0.0.0.0":
            log.warning("Agent API is listening on ALL network interfaces (REELAY_HOST=0.0.0.0). "
                        "This is meant for phone apps on your LAN only.")
        log.info("Agent API listening on %s", self.url)
        return True

    def stop(self, timeout: float = 5.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("Agent API thread didn't stop within %.0fs", timeout)
        self._server = self._thread = None


# ====================================================================== contract + standalone
def contract_dict(app: FastAPI) -> dict:
    """The OpenAPI document as plain JSON-compatible data."""
    return jsonable_encoder(app.openapi())


def write_contract(path: Path = CONTRACT_PATH) -> Path:
    import yaml

    from .store import Store as _Store
    from .tokens import MemoryTokenStore
    from .uploads import UploadManager

    store = _Store(":memory:")
    accounts = AccountService(store, MemoryTokenStore())
    app = create_app(store, Pipeline(store, accounts, UploadManager()), accounts, lambda: "x")
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ("# Reelay Agent API contract. GENERATED from the code: do not edit by hand.\n"
              "# Regenerate with:  python -m reelay.api --write-contract\n"
              "# tests/test_api_contract.py fails if the live /openapi.json drifts from this file.\n")
    path.write_text(header + yaml.safe_dump(contract_dict(app), sort_keys=False, allow_unicode=True, width=110),
                    encoding="utf-8")
    store.close()
    return path


def main(argv: list[str] | None = None) -> int:
    """Run the dispatcher and agent API without the desktop window."""
    import argparse
    import signal
    import sys

    parser = argparse.ArgumentParser(description="Reelay agent API + dispatcher (headless).")
    parser.add_argument("--write-contract", action="store_true", help="Regenerate api/openapi.yaml and exit.")
    args = parser.parse_args(argv)
    if args.write_contract:
        print(f"Wrote {write_contract()}")
        return 0

    from PySide6.QtCore import QCoreApplication, QTimer

    from .gui.app import setup_logging
    from .paths import db_path
    from .services import Services
    from .tokens import KeyringTokenStore

    setup_logging()
    logging.getLogger().setLevel(logging.INFO)
    app = QCoreApplication(sys.argv[:1])   # runs the upload queue's Qt event loop
    store = Store(db_path())
    services = Services(store, KeyringTokenStore())
    services.start_background(api=True)
    if services.api is None or not services.api.running:
        services.stop()
        store.close()
        return 1
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    ticker = QTimer()
    ticker.start(200)                       # lets Python handle Ctrl-C while Qt's loop runs
    ticker.timeout.connect(lambda: None)
    print(f"Reelay agent API on {services.api.url}  (Ctrl-C to stop)", flush=True)
    code = app.exec()
    log.info("Shutting down the agent API and dispatcher…")
    services.stop()
    store.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
