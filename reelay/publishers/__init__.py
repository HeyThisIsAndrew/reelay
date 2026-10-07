"""Platform publishers used by the dispatcher.

YouTube isn't here: it goes through the app-wide UploadManager (one upload
at a time, resumable), driven by the Pipeline.
"""

from .base import PermanentError, PublishError, PublishJob, PublishResult, Publisher, TransientError, redact
from .bluesky import BlueskyPublisher
from .facebook import FacebookPublisher
from .threads import ThreadsPublisher


def default_publishers(session=None) -> dict:
    return {p.platform: p for p in (BlueskyPublisher(session), ThreadsPublisher(session), FacebookPublisher(session))}


__all__ = ["BlueskyPublisher", "FacebookPublisher", "PermanentError", "PublishError", "PublishJob", "PublishResult",
           "Publisher", "ThreadsPublisher", "TransientError", "default_publishers", "redact"]
