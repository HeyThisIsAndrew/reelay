"""Transcripts from the original post's captions (subtitles), via yt-dlp.

TikTok and YouTube often publish captions alongside a video: creator-made
subtitles first, otherwise automatic captions. They come as WebVTT, which is
turned into plain text here (timestamps, cue numbers and styling removed, and
the rolling repeats of automatic captions collapsed). No speech-to-text runs
locally, so a video without captions has no transcript to fetch.
"""

from __future__ import annotations

import html
import re

PREFERRED_LANGS = ("en", "en-US", "en-GB", "eng-US", "en-orig")
TEXT_FORMATS = ("vtt", "srt")
_TIMING = re.compile(r"^\s*(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{3}\s*-->")
_TAG = re.compile(r"<[^>]+>")


class TranscriptError(Exception):
    """A plain-language reason there is no transcript."""


def captions_to_text(data: str) -> str:
    """WebVTT or SRT → plain paragraphs. Consecutive duplicate lines (how
    automatic captions scroll) are kept once."""
    lines: list[str] = []
    in_note = False
    for raw in data.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            in_note = False
            continue
        if in_note or line.startswith(("WEBVTT", "Kind:", "Language:", "STYLE", "REGION")):
            continue
        if line.startswith("NOTE"):
            in_note = True
            continue
        if line.isdigit() or _TIMING.match(line):
            continue
        text = html.unescape(_TAG.sub("", line)).strip()
        if text and (not lines or lines[-1] != text):
            lines.append(text)
    # Automatic captions often repeat the previous line as the start of the next one.
    merged: list[str] = []
    for text in lines:
        if merged and text.startswith(merged[-1]) and len(text) > len(merged[-1]):
            merged[-1] = text
        elif not merged or merged[-1] != text:
            merged.append(text)
    return " ".join(merged).strip()


def pick_track(info: dict) -> dict | None:
    """The best text caption track: creator subtitles before automatic ones,
    English first, then any language."""
    for key in ("subtitles", "automatic_captions"):
        tracks = info.get(key) or {}
        langs = [lang for lang in PREFERRED_LANGS if lang in tracks] + \
                sorted(lang for lang in tracks if lang.startswith("en") and lang not in PREFERRED_LANGS) + \
                sorted(lang for lang in tracks if not lang.startswith("en") and lang != "live_chat")
        for lang in langs:
            for fmt in tracks.get(lang) or []:
                if fmt.get("ext") in TEXT_FORMATS and fmt.get("url"):
                    return {**fmt, "lang": lang, "automatic": key == "automatic_captions"}
    return None


def fetch_transcript(url: str, ydl_opts: dict, ydl_factory=None) -> tuple[str, dict]:
    """(transcript text, the track used) for the post at `url`. Raises
    TranscriptError. Network: one metadata request and one captions request."""
    if ydl_factory is None:
        import yt_dlp
        ydl_factory = yt_dlp.YoutubeDL
    opts = {**ydl_opts, "skip_download": True, "noplaylist": True, "quiet": True, "no_warnings": True}
    try:
        with ydl_factory(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            track = pick_track(info or {})
            if track is None:
                raise TranscriptError("This post has no captions to make a transcript from.")
            data = ydl.urlopen(track["url"]).read().decode("utf-8", "replace")
    except TranscriptError:
        raise
    except Exception as exc:  # noqa: BLE001 — yt-dlp raises many types; report them plainly
        from .ytdl import friendly_error
        raise TranscriptError(friendly_error(exc, url)) from None
    text = captions_to_text(data)
    if not text:
        raise TranscriptError("The captions for this post are empty.")
    return text, track
