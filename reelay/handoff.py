"""Prepare to Publish: a guided hand-off for platforms Reelay doesn't post to itself.

TikTok and Instagram don't let a desktop app post a creator's video for them in
a way that keeps their own sounds, tags and Shop links, so the person posts in
the platform's own uploader. Reelay does everything around that click:

1. checks the video and caption are ready (``prepare``),
2. copies the caption to the clipboard,
3. opens the platform's upload page in the default browser,
4. shows the .mp4 in Finder / Explorer, selected, ready to drag in,
5. waits for the person to paste the live post's link, then records it with
   the time it went live (``Pipeline.confirm_handoff``).

This module is UI-free: steps 2–4 are done by the dialog with Qt, except
``reveal_in_file_manager`` which is plain OS calls.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .models import Platform, Post
from .platform_text import hashtag_count, text_problem

UPLOAD_URLS = {
    Platform.TIKTOK: "https://www.tiktok.com/tiktokstudio/upload",
    Platform.INSTAGRAM: "https://www.instagram.com/",
    Platform.YOUTUBE: "https://studio.youtube.com/",
    Platform.FACEBOOK: "https://business.facebook.com/latest/reels_composer",
    Platform.THREADS: "https://www.threads.com/",
    Platform.BLUESKY: "https://bsky.app/",
}
UPLOAD_HINTS = {
    Platform.TIKTOK: "TikTok Studio's upload page opens. Drag the video in, paste the caption, add sounds, "
                     "product links and cover, then post or schedule.",
    Platform.INSTAGRAM: "Instagram opens. Click + Create → Post, drag the video in (it becomes a Reel), paste "
                        "the caption, then share.",
    Platform.YOUTUBE: "YouTube Studio opens. Click Create → Upload videos and drag the video in.",
    Platform.FACEBOOK: "Meta Business Suite's Reel composer opens. Drag the video in and paste the text.",
    Platform.THREADS: "Threads opens. Start a new thread, attach the video and paste the text.",
    Platform.BLUESKY: "Bluesky opens. Start a new post, attach the video and paste the text.",
}
# Hosts a live post's link can be on (short links included).
POST_HOSTS = {
    Platform.TIKTOK: ("tiktok.com",),
    Platform.INSTAGRAM: ("instagram.com", "instagr.am"),
    Platform.YOUTUBE: ("youtube.com", "youtu.be"),
    Platform.FACEBOOK: ("facebook.com", "fb.watch", "fb.com"),
    Platform.THREADS: ("threads.com", "threads.net"),
    Platform.BLUESKY: ("bsky.app",),
}
CAPTION_FIELD = {
    Platform.TIKTOK: "tiktok_text", Platform.INSTAGRAM: "instagram_text", Platform.FACEBOOK: "facebook_text",
    Platform.THREADS: "threads_text", Platform.BLUESKY: "bluesky_text",
}
OK, WARN, BLOCK = "ok", "warn", "block"


@dataclass
class Step:
    label: str
    status: str        # ok | warn | block
    detail: str


@dataclass
class HandoffPlan:
    platform: str
    caption: str
    video: Path | None
    url: str
    hint: str
    checks: list[Step] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(c.status == BLOCK for c in self.checks)

    @property
    def warnings(self) -> list[Step]:
        return [c for c in self.checks if c.status == WARN]


def caption_for(post: Post, platform: str) -> str:
    """The text to paste: the platform's own caption (or the description), plus the
    shared hashtag block when those hashtags aren't in it already."""
    if platform == Platform.YOUTUBE:
        text = post.description or ""
    else:
        text = (getattr(post, CAPTION_FIELD.get(platform, ""), None) or "").strip() or (post.description or "")
    text = text.strip()
    block = (post.hashtags or "").strip()
    if block and platform != Platform.YOUTUBE:
        missing = [t for t in block.split() if t.lower() not in text.lower()]
        if missing:
            text = f"{text}\n\n{' '.join(missing)}".strip()
    return text


def prepare(post: Post, platform: str, video: Path | None, duration: float | None = None) -> HandoffPlan:
    """Everything the hand-off needs, with a check list. Blocked = can't post this yet."""
    label = Platform.LABELS.get(platform, platform)
    caption = caption_for(post, platform)
    checks: list[Step] = []
    exists = video is not None and Path(video).is_file()
    checks.append(Step("Video file", OK if exists else BLOCK,
                       Path(video).name if exists else "The video isn't on this computer. Download it first."))
    own = (getattr(post, CAPTION_FIELD.get(platform, ""), None) or "").strip() if platform in CAPTION_FIELD else ""
    if not caption:
        checks.append(Step("Caption", BLOCK, f"Write the {label} caption first."))
    else:
        problem = text_problem(platform, caption) if platform in CAPTION_FIELD else None
        if problem:
            checks.append(Step("Caption", BLOCK, problem))
        elif platform in CAPTION_FIELD and not own:
            checks.append(Step("Caption", WARN, f"No {label} caption yet, so the YouTube description is used."))
        else:
            checks.append(Step("Caption", OK, f"{len(caption):,} characters"
                               + (f", {hashtag_count(caption)} hashtags" if hashtag_count(caption) else "")))
    if platform == Platform.TIKTOK and duration and duration > 60 * 60:
        checks.append(Step("Length", BLOCK, "TikTok uploads can be at most 60 minutes long."))
    if platform == Platform.INSTAGRAM and duration and duration > 15 * 60:
        checks.append(Step("Length", WARN, "Reels over 15 minutes post as a regular video."))
    return HandoffPlan(platform, caption, Path(video) if exists else None, UPLOAD_URLS.get(platform, ""),
                       UPLOAD_HINTS.get(platform, ""), checks)


def check_post_url(platform: str, url: str) -> str | None:
    """A problem with the pasted link, or None. Empty is allowed (record without a link)."""
    url = (url or "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "Paste the full link, starting with https://."
    host = parsed.netloc.lower().split(":")[0]
    hosts = POST_HOSTS.get(platform, ())
    if hosts and not any(host == h or host.endswith("." + h) for h in hosts):
        return f"That isn't a {Platform.LABELS.get(platform, platform)} link."
    return None


def reveal_in_file_manager(path: Path | str, runner=subprocess.Popen) -> bool:
    """Show the file selected in Finder / Explorer (or open its folder elsewhere)."""
    path = Path(path)
    if not path.exists():
        return False
    try:
        if sys.platform == "darwin":
            runner(["open", "-R", str(path)])
        elif os.name == "nt":
            runner(["explorer", f"/select,{path}"])
        else:
            runner(["xdg-open", str(path.parent)])
    except OSError:
        return False
    return True
