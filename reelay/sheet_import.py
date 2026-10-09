"""Import a spreadsheet (CSV, or a Google Sheet link) into tickets.

The manual workflow this replaces: a sheet with one row per video and columns
for the title, captions, hashtags and so on. Each row is matched to a video on
the profile (by the video's link, its ID, the file name, or your name for it),
each column is mapped to a ticket field, and the result is previewed before
anything is written. The import then lands in ONE transaction: every row or
none, each change logged with origin "import".

Rules:
- Empty cells never clear anything.
- By default only empty fields are filled; "overwrite" replaces what's there.
- A row with an invalid value (over a platform's limit, a bad date…) is skipped
  as a whole and reported, never shortened.
- Copy on videos already scheduled or published isn't changed (it was sent to
  the platforms); their ticket fields still are.

Pure functions, no Qt and no network (fetch_google_sheet takes a session).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import ContentType, Platform, Post, Ticket, VideoRow

# Target fields and the column headers that mean them (lower-case, punctuation-free).
FIELDS: dict[str, tuple[str, ...]] = {
    "title": ("title", "youtube title", "yt title", "video title", "headline"),
    "description": ("description", "youtube description", "yt description", "desc"),
    "tags": ("tags", "youtube tags", "yt tags"),
    "target_platforms": ("platforms", "target platforms", "post to", "targets"),
    "tiktok_text": ("tiktok", "tiktok caption", "tiktok text", "tiktok copy"),
    "instagram_text": ("instagram", "instagram caption", "ig", "ig caption", "instagram text", "reels caption"),
    "bluesky_text": ("bluesky", "bluesky text", "bluesky post", "bsky"),
    "threads_text": ("threads", "threads text", "threads post"),
    "facebook_text": ("facebook", "facebook text", "facebook post", "fb", "fb caption"),
    "hashtags": ("hashtags", "hash tags", "hashtag"),
    "transcript": ("transcript", "script", "voiceover"),
    "topic": ("topic", "subject", "theme"),
    "keywords": ("keywords", "keyword", "seo keywords"),
    "creator": ("creator", "brand", "author"),
    "content_type": ("type", "content type", "format"),
    "original_publish_date": ("original publish date", "publish date", "posted", "posted on", "date posted",
                              "original date", "upload date", "date"),
    "authorization": ("permission", "authorization", "rights", "license"),
    "thumbnail_path": ("cover", "thumbnail", "cover image", "thumbnail path"),
}
# Columns that identify which video a row is about.
MATCH_KEYS: dict[str, tuple[str, ...]] = {
    "match_url": ("url", "link", "video url", "video link", "original url", "source url", "tiktok url",
                  "tiktok link", "post url", "original link"),
    "match_id": ("video id", "id", "tiktok id"),
    "match_file": ("file", "file name", "filename"),
    "match_name": ("your name", "custom name", "name", "short name", "slug"),
}
LABELS = {
    "title": "YouTube title", "description": "YouTube description", "tags": "YouTube tags",
    "target_platforms": "Post automatically to", "tiktok_text": "TikTok caption",
    "instagram_text": "Instagram caption", "bluesky_text": "Bluesky post", "threads_text": "Threads post",
    "facebook_text": "Facebook post", "hashtags": "Hashtags", "transcript": "Transcript", "topic": "Topic",
    "keywords": "Keywords", "creator": "Creator", "content_type": "Type", "original_publish_date": "First published",
    "authorization": "Permission", "thumbnail_path": "Cover image", "match_url": "Video link (to match)",
    "match_id": "Video ID (to match)", "match_file": "File name (to match)", "match_name": "Your name (to match)",
}
POST_FIELDS = ("title", "description", "tags", "target_platforms", "tiktok_text", "instagram_text", "bluesky_text",
               "threads_text", "facebook_text", "hashtags")
_ID_PATTERNS = (re.compile(r"/video/(\d+)"), re.compile(r"/photo/(\d+)"), re.compile(r"[?&]v=([\w-]{6,})"),
                re.compile(r"youtu\.be/([\w-]{6,})"), re.compile(r"/shorts/([\w-]{6,})"),
                re.compile(r"/(?:reel|reels|p|tv)/([\w-]+)"))


class SheetError(Exception):
    """A plain-language reason the sheet can't be read."""


# ====================================================================== reading
def read_csv(text: str) -> tuple[list[str], list[list[str]]]:
    """(headers, rows) from CSV/TSV text. Handles a BOM, ; or tab separators
    and blank trailing rows."""
    text = text.lstrip("﻿")
    if not text.strip():
        raise SheetError("The sheet is empty.")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise SheetError("The sheet needs a header row and at least one row of videos.")
    headers = [h.strip() for h in rows[0]]
    width = len(headers)
    return headers, [(r + [""] * width)[:width] for r in rows[1:]]


def read_file(path: str | Path) -> tuple[list[str], list[list[str]]]:
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return read_csv(raw.decode(encoding))
        except UnicodeDecodeError:
            continue
    raise SheetError("That file isn't readable text. Save the sheet as CSV and try again.")


def google_export_url(link: str) -> str | None:
    """A Google Sheets link → its CSV export link (keeps the tab, if any)."""
    m = re.search(r"docs\.google\.com/spreadsheets/d/([\w-]+)", link or "")
    if not m:
        return None
    gid = re.search(r"[#&?]gid=(\d+)", link)
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv" + \
           (f"&gid={gid.group(1)}" if gid else "")


def fetch_google_sheet(link: str, session) -> tuple[list[str], list[list[str]]]:
    """Download a Google Sheet as CSV. It must be shared as "Anyone with the link"."""
    url = google_export_url(link)
    if url is None:
        raise SheetError("That isn't a Google Sheets link.")
    try:
        resp = session.get(url, timeout=30, allow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        raise SheetError(f"Couldn't reach Google Sheets: {exc}") from None
    kind = (resp.headers or {}).get("Content-Type", "")
    if resp.status_code != 200 or "text/html" in kind:
        raise SheetError("Google didn't share that sheet. In Google Sheets, click Share and choose "
                           "\"Anyone with the link\" (Viewer), or use File → Download → CSV and import the file.")
    resp.encoding = "utf-8"
    return read_csv(resp.text)


# ====================================================================== mapping
def _norm(header: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", header.lower()).split())


def guess_mapping(headers: list[str]) -> dict[str, str | None]:
    """header → target field (or a match key), None = ignore. Each target is
    used once, by the first column that names it."""
    used: set[str] = set()
    mapping: dict[str, str | None] = {}
    table = {**MATCH_KEYS, **FIELDS}
    for header in headers:
        key = _norm(header)
        target = next((t for t, aliases in table.items() if key in aliases and t not in used), None)
        if target:
            used.add(target)
        mapping[header] = target
    return mapping


# ====================================================================== values
def video_key_from_url(url: str) -> str | None:
    for pattern in _ID_PATTERNS:
        m = pattern.search(url or "")
        if m:
            return m.group(1)
    return None


def parse_date(value: str) -> str | None:
    """Common sheet date formats → ISO 8601 UTC. None if not a date."""
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%Y%m%d",
                "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return None


def convert(target: str, value: str) -> tuple[object, str | None]:
    """(value for the field, problem or None)."""
    value = value.strip()
    if target == "tags":
        return [t.strip().lstrip("#") for t in re.split(r"[,;\n]", value) if t.strip().lstrip("#")], None
    if target == "target_platforms":
        names = {**{p: p for p in Platform.LABELS}, **{v.lower(): k for k, v in Platform.LABELS.items()},
                 "yt": "youtube", "fb": "facebook", "bsky": "bluesky"}
        parts = [p.strip().lower() for p in re.split(r"[,;/\n]", value) if p.strip()]
        unknown = [p for p in parts if p not in names]
        if unknown:
            return None, f"Unknown platform(s): {', '.join(unknown)}."
        return list(dict.fromkeys(names[p] for p in parts)), None
    if target == "content_type":
        key = re.sub(r"[\s_]+", "-", value.lower()).replace("longform", "long-form")
        key = {"shorts": "short", "reel": "short", "reels": "short", "long": "long-form", "video": "long-form",
               "clips": "clip"}.get(key, key)
        if key not in ContentType.ALL:
            return None, f"Type “{value}” isn't one of: Short, Long-form, Clip, Other."
        return key, None
    if target == "original_publish_date":
        iso = parse_date(value)
        return (iso, None) if iso else (None, f"“{value}” isn't a date Reelay understands (try 2026-10-02).")
    return value, None


# ====================================================================== matching
@dataclass
class RowPlan:
    number: int                         # the sheet's row number (header = 1)
    video_id: int | None = None
    matched_by: str = ""                # "link" | "video ID" | "file name" | "your name"
    title: str = ""                     # the matched video, for people
    post_changes: dict = field(default_factory=dict)
    ticket_changes: dict = field(default_factory=dict)
    kept: list[str] = field(default_factory=list)       # fields left alone (already filled, or locked)
    problems: list[str] = field(default_factory=list)   # why the row is skipped

    @property
    def fields(self) -> list[str]:
        return [*self.post_changes, *self.ticket_changes]

    @property
    def will_change(self) -> bool:
        return self.video_id is not None and not self.problems and bool(self.fields)


@dataclass
class ImportPlan:
    rows: list[RowPlan]
    mapping: dict[str, str | None]

    @property
    def changing(self) -> list[RowPlan]:
        return [r for r in self.rows if r.will_change]

    def summary(self) -> str:
        unmatched = sum(1 for r in self.rows if r.video_id is None)
        problems = sum(1 for r in self.rows if r.video_id is not None and r.problems)
        unchanged = sum(1 for r in self.rows if r.video_id is not None and not r.problems and not r.fields)
        parts = [f"{len(self.changing)} ticket{'s' if len(self.changing) != 1 else ''} will change"]
        if unchanged:
            parts.append(f"{unchanged} already up to date")
        if problems:
            parts.append(f"{problems} skipped (see why)")
        if unmatched:
            parts.append(f"{unmatched} row{'s' if unmatched != 1 else ''} didn't match a video")
        return " · ".join(parts) + "."


def _empty(value) -> bool:
    return value is None or value == "" or value == []


def plan_import(headers: list[str], rows: list[list[str]], mapping: dict[str, str | None],
                videos: list[VideoRow], posts: dict[int, Post], locked: set[int], overwrite: bool,
                validate) -> ImportPlan:
    """Work out exactly what an import would do. `posts`: each video's current
    (or suggested) post. `locked`: videos whose copy can't change. `validate`:
    callable(row, post_changes, ticket_changes) -> list of problems."""
    by_key: dict[str, dict[str, VideoRow]] = {"match_url": {}, "match_id": {}, "match_file": {}, "match_name": {}}
    for v in videos:
        item = v.item
        by_key["match_id"][item.video_id.lower()] = v
        by_key["match_url"][item.video_id.lower()] = v
        if item.filename:
            by_key["match_file"][item.filename.lower()] = v
            by_key["match_file"][Path(item.filename).stem.lower()] = v
        if item.custom_name:
            by_key["match_name"][item.custom_name.lower()] = v
    columns = {target: headers.index(h) for h, target in mapping.items() if target and h in headers}
    how = {"match_url": "link", "match_id": "video ID", "match_file": "file name", "match_name": "your name"}
    plans: list[RowPlan] = []
    seen: dict[int, int] = {}
    for n, cells in enumerate(rows, start=2):
        plan = RowPlan(number=n)
        for key in ("match_url", "match_id", "match_file", "match_name"):
            if key not in columns:
                continue
            raw = cells[columns[key]].strip()
            if not raw:
                continue
            lookup = (video_key_from_url(raw) or raw) if key == "match_url" else raw
            video = by_key[key].get(lookup.lower())
            if video is not None:
                by = "video ID" if key == "match_url" and not video_key_from_url(raw) else how[key]
                plan.video_id, plan.matched_by, plan.title = video.id, by, video.item.hook[:80]
                break
        if plan.video_id is None:
            plan.problems.append("No video on this profile matches this row." if any(
                k in columns for k in by_key) else "Map a column to the video's link, ID, file name or your name.")
            plans.append(plan)
            continue
        if plan.video_id in seen:
            plan.problems.append(f"Same video as row {seen[plan.video_id]}; only the first row is used.")
            plans.append(plan)
            continue
        seen[plan.video_id] = n
        video = next(v for v in videos if v.id == plan.video_id)
        post = posts[plan.video_id]
        for target, col in columns.items():
            if target.startswith("match_"):
                continue
            cell = cells[col]
            if not cell.strip():
                continue                        # empty cells never clear anything
            value, problem = convert(target, cell)
            if problem:
                plan.problems.append(f"{LABELS[target]}: {problem}")
                continue
            if target in POST_FIELDS:
                current = getattr(post, target)
                if plan.video_id in locked:
                    plan.kept.append(f"{LABELS[target]} (already scheduled or published)")
                elif current == value:
                    continue
                elif _empty(current) or overwrite:
                    plan.post_changes[target] = value
                else:
                    plan.kept.append(f"{LABELS[target]} (already filled)")
            else:
                current = getattr(video.ticket, target)
                if current == value:
                    continue
                if _empty(current) or overwrite:
                    plan.ticket_changes[target] = value
                else:
                    plan.kept.append(f"{LABELS[target]} (already filled)")
        if not plan.problems and plan.fields:
            plan.problems.extend(validate(video, plan.post_changes, plan.ticket_changes))
        plans.append(plan)
    return ImportPlan(plans, mapping)


def ticket_from(changes: dict, ticket: Ticket) -> Ticket:
    return Ticket(**{**ticket.__dict__, **changes})
