"""The output folder: downloaded videos, metadata sidecars, names, manifest.

    <folder>/TikTok_20240115_7134….mp4
    <folder>/metadata/TikTok_7134….json     (keyed by platform + ID, so
    <folder>/metadata/custom_names.json      renaming a file never breaks it)
    <folder>/manifest.md
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .manifest import read_custom_names, write_manifest
from .models import Status, VideoItem
from .naming import assign_filenames

log = logging.getLogger("reelay")

CUSTOM_NAMES_FILE = "custom_names.json"


@dataclass
class RenameStep:
    item: VideoItem
    old: str
    new: str


class Library:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.metadata_dir = self.folder / "metadata"
        self.manifest_path = self.folder / "manifest.md"

    def ensure(self) -> None:
        self.metadata_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Loading / saving
    # ------------------------------------------------------------------ #
    def load(self) -> list[VideoItem]:
        """Every previously downloaded video whose file is still present."""
        if not self.metadata_dir.is_dir():
            return []
        items: dict[str, VideoItem] = {}
        for path in sorted(self.metadata_dir.glob("*.json")):
            if path.name == CUSTOM_NAMES_FILE:
                continue
            try:
                item = VideoItem.from_record(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError) as exc:
                log.warning("Skipping unreadable metadata %s (%s)", path.name, exc)
                continue
            if not item.video_id or not (self.folder / item.filename).is_file():
                continue
            item.status, item.checked = Status.DOWNLOADED, False
            items[item.key] = item
            if path.name != f"{item.key}.json":  # migrate first-version sidecar names
                self.save(item)
                path.unlink(missing_ok=True)

        # Names typed into manifest.md by hand win over stale stored ones.
        by_file = {i.filename: i for i in items.values()}
        for filename, name in read_custom_names(self.manifest_path).items():
            if filename in by_file and not by_file[filename].custom_name:
                by_file[filename].custom_name = name
        return list(items.values())

    def save(self, item: VideoItem) -> None:
        self.ensure()
        path = self.metadata_dir / f"{item.key}.json"
        _atomic_write(path, json.dumps(item.to_record(), indent=2, ensure_ascii=False))

    def load_custom_names(self) -> dict[str, str]:
        path = self.metadata_dir / CUSTOM_NAMES_FILE
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_custom_names(self, items: Iterable[VideoItem]) -> None:
        """Persist custom names for every item, downloaded or not."""
        self.ensure()
        names = self.load_custom_names()
        for item in items:
            if item.custom_name:
                names[item.key] = item.custom_name
            else:
                names.pop(item.key, None)
        _atomic_write(self.metadata_dir / CUSTOM_NAMES_FILE, json.dumps(names, indent=2, ensure_ascii=False))

    def write_manifest(self, items: Iterable[VideoItem], sources: Iterable[str] = (),
                       failures: Iterable[tuple[str, str]] = ()) -> None:
        self.ensure()
        write_manifest(self.manifest_path, [i for i in items if i.is_downloaded], sources, failures)

    # ------------------------------------------------------------------ #
    # Batch renaming
    # ------------------------------------------------------------------ #
    def plan_renames(self, items: list[VideoItem], template: str) -> list[RenameStep]:
        """Which downloaded files would change name under `template`."""
        downloaded = [i for i in items if i.is_downloaded]
        targets = assign_filenames(downloaded, template)
        return [RenameStep(i, i.filename, targets[i.key])
                for i in downloaded if targets[i.key] != i.filename]

    def apply_renames(self, steps: list[RenameStep]) -> list[str]:
        """Rename files in two phases so swaps (A→B, B→A) and case-only
        changes work. Returns human-readable errors; successes update items."""
        errors: list[str] = []
        planned_old = {s.old.lower() for s in steps}
        ready: list[tuple[RenameStep, Path]] = []

        for step in steps:
            src, dst = self.folder / step.old, self.folder / step.new
            if not src.is_file():
                errors.append(f"{step.old}: file is missing")
                continue
            # A file already at the target that isn't itself being renamed away.
            if dst.exists() and step.new.lower() not in planned_old:
                errors.append(f"{step.new}: a different file already has this name")
                continue
            tmp = self.folder / f".renaming-{uuid.uuid4().hex}.tmp"
            try:
                os.rename(src, tmp)
                ready.append((step, tmp))
            except OSError as exc:
                errors.append(f"{step.old}: {exc.strerror or exc}")

        for step, tmp in ready:
            dst = self.folder / step.new
            try:
                if dst.exists():
                    raise FileExistsError(f"{step.new} already exists")
                os.rename(tmp, dst)
                step.item.filename = step.new
            except OSError as exc:
                os.rename(tmp, self.folder / step.old)  # put it back
                errors.append(f"{step.old}: {exc}")
            self.save(step.item)
        return errors


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
