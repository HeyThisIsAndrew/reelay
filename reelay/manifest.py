"""manifest.md: the human review table for the migration."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .models import VideoItem

COLUMNS = [
    "Original URL",
    "Downloaded Filename",
    "Original Title / Hook",
    "Description Snippet",
    "New Custom Name (Blank)",
]
DESCRIPTION_SNIPPET_LEN = 140
HOOK_LEN = 100


def md_cell(text: str | None) -> str:
    """Make arbitrary text safe inside a Markdown table cell."""
    return re.sub(r"\s+", " ", text or "").strip().replace("|", r"\|")


def truncate(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def read_custom_names(path: Path) -> dict[str, str]:
    """Names typed into the last column of an existing manifest, by filename."""
    if not path.is_file():
        return {}
    names: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        if len(cells) != len(COLUMNS) or cells == COLUMNS:
            continue
        filename, custom = cells[1].strip("`"), cells[4].replace(r"\|", "|")
        if filename and custom and not set(custom) <= set("-: "):
            names[filename] = custom
    return names


def write_manifest(path: Path, items: Iterable[VideoItem], sources: Iterable[str] = (),
                   failures: Iterable[tuple[str, str]] = ()) -> None:
    items = sorted((i for i in items if i.filename), key=lambda i: i.filename.lower())
    failures = list(failures)
    sources = [s for s in sources if s]
    lines = [
        "# Content Migration Manifest",
        "",
        f"- **Generated:** {datetime.now():%Y-%m-%d %H:%M}",
        f"- **Source(s):** {', '.join(sources) if sources else '(existing downloads)'}",
        f"- **Videos in folder:** {len(items)}",
        f"- **Failed this session:** {len(failures)}",
        "",
        "Fill in the last column with the new SEO filename for each video. "
        "Full metadata (tags, views, likes, duration) is in `metadata/`.",
        "",
        "| " + " | ".join(COLUMNS) + " |",
        "|" + "|".join("---" for _ in COLUMNS) + "|",
    ]
    for item in items:
        lines.append("| " + " | ".join([
            md_cell(item.url),
            f"`{md_cell(item.filename)}`",
            md_cell(truncate(item.hook, HOOK_LEN)),
            md_cell(truncate(item.description, DESCRIPTION_SNIPPET_LEN)),
            md_cell(item.custom_name),
        ]) + " |")
    if failures:
        lines += ["", "## Failed downloads", "", "| URL | Reason |", "|---|---|"]
        lines += [f"| {md_cell(url)} | {md_cell(reason)} |" for url, reason in failures]

    tmp = path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)  # atomic: a crash never leaves a half-written manifest
