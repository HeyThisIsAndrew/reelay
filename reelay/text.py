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
