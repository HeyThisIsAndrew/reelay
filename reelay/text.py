"""Small text helpers shared by the engine and the GUI."""

from __future__ import annotations

import re


def short_title(text: str, limit: int = 90) -> str:
    """First sentence of a caption, capped at `limit` characters."""
    text = " ".join((text or "").split())
    first = re.split(r"(?<=[.!?…])\s+", text, maxsplit=1)[0]
    if 20 <= len(first) <= limit:
        return first
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def clock(dt) -> str:
    """'9:05 AM' on every platform (Windows strftime has no %-I)."""
    return f"{dt.hour % 12 or 12}:{dt:%M} {dt:%p}"


def local_text(utc_iso: str | None) -> str:
    """'Tue Oct 20, 9:00 AM' in the computer's time zone."""
    from datetime import datetime, timezone
    if not utc_iso:
        return ""
    try:
        when = datetime.strptime(utc_iso.replace(".000Z", "Z"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    local = when.astimezone()
    return f"{local:%a %b} {local.day}, {clock(local)}"
