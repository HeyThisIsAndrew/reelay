"""Shared types for platform publishers.

A publisher turns one claimed Dispatch into a live (or platform-scheduled)
post. It raises TransientError for things worth retrying (network, 5xx,
rate limits) and PermanentError for things that won't fix themselves (bad
credentials, rejected media). Error messages never contain secrets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

import requests

from ..models import Account, Dispatch, Post, VideoItem


class PublishError(Exception):
    transient = False


class TransientError(PublishError):
    transient = True


class PermanentError(PublishError):
    transient = False


@dataclass
class PublishJob:
    dispatch: Dispatch
    post: Post
    item: VideoItem
    video_path: Path
    account: Account
    credentials: dict
    text: str                                   # resolved platform text ("" for YouTube)
    media_url: Callable[[], str] | None = None  # public expiring link (Threads)
    checkpoint: Callable[[str], None] = field(default=lambda _c: None)
    media: object | None = None                 # what the file really is (probe.MediaInfo), when it's known
    save_credentials: Callable[[dict], None] = field(default=lambda _s: None)   # rotated tokens (Bluesky OAuth)


@dataclass
class PublishResult:
    status: str          # DispatchStatus.SCHEDULED or PUBLISHED
    remote_id: str = ""
    remote_url: str = ""


# Platforms whose publisher can safely pick up where an interrupted send left off
# (it records a checkpoint before the step that creates the post, and checks it on
# retry), so a send cut short by a crash is resumed automatically. Every other
# platform's interrupted send waits for the person to say what happened.
RESUMABLE = frozenset({"bluesky", "threads"})


class Publisher(Protocol):
    platform: str

    def publish(self, job: PublishJob) -> PublishResult | None:
        """None means "handed off" (the result arrives later, e.g. YouTube uploads)."""


_SECRET_PARAMS = re.compile(r"(access_token|password|token|key)=[^&\s\"']+", re.IGNORECASE)


def redact(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    """Strip anything secret from an error message."""
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[redacted]")
    return _SECRET_PARAMS.sub(lambda m: f"{m.group(1)}=[redacted]", text)


def error_from_response(resp, platform: str, secrets=()) -> PublishError:
    """Map an HTTP error response to Transient/Permanent with a readable message."""
    try:
        data = resp.json()
    except ValueError:
        data = {}
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):  # Meta Graph style
        message = err.get("error_user_msg") or err.get("message") or ""
        code = err.get("code")
        transient_codes = {1, 2, 4, 17, 32, 341, 368, 613}   # Meta: temporary / rate limiting
        transient = resp.status_code >= 500 or resp.status_code == 429 or code in transient_codes \
            or bool(err.get("is_transient"))
    else:  # AT Protocol style: {"error": "Name", "message": "..."}
        message = (data.get("message") or err or "") if isinstance(data, dict) else ""
        transient = resp.status_code >= 500 or resp.status_code == 429
    message = redact(f"{platform}: {message or f'HTTP {resp.status_code}'}", secrets)
    return (TransientError if transient else PermanentError)(message)


def network_error(exc: requests.RequestException, platform: str, secrets=()) -> TransientError:
    return TransientError(redact(f"{platform}: network problem ({type(exc).__name__})", secrets))
