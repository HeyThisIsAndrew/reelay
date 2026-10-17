"""Board columns you can rename, add, remove and reorder (per profile).

Reelay's workflow has four built-in steps (backlog, staging, scheduled,
published) that automatic moves rely on: scheduling puts a card in
"scheduled", going live puts it in "published". A column shows one or more of
those steps, and any number of your own steps:

- Every built-in step lives in exactly one column. Removing a column hands its
  built-in steps to the neighbouring column, so nothing goes missing.
- Columns you add get their own step ("custom:…") that behaves like Staging:
  you drag cards in and out; nothing moves them automatically.
- A card dropped on a column goes to that column's main step.
- A board has 2 to 6 columns. Each has a title and a one-line subtitle saying what it holds; both are yours
  to change (an empty subtitle shows the default for the column's steps).

The default is four columns in plain words: Inbox, In Prep, Scheduled, Posted. (The steps' stored keys stay
backlog, staging, scheduled and published.)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field

from .models import Stage

CUSTOM_PREFIX = "custom:"
NAME_MAX = 40
SUBTITLE_MAX = 60
MIN_COLUMNS, MAX_COLUMNS = 2, 6
# What each built-in step holds, shown under a column's title unless the column has its own subtitle
DEFAULT_SUBTITLES = {
    Stage.BACKLOG: "Downloaded, not started",
    Stage.STAGING: "Writing captions & picking platforms",
    Stage.SCHEDULED: "Waiting to go live",
    Stage.PUBLISHED: "Live on your channels",
}

# The names columns had before they were renamed; a saved board still using one gets the new name
OLD_NAMES = {Stage.BACKLOG: "Backlog", Stage.STAGING: "Staging", Stage.PUBLISHED: "Published"}


class ColumnError(ValueError):
    """A plain-language reason a column change isn't possible."""


def is_custom(stage: str) -> bool:
    return isinstance(stage, str) and stage.startswith(CUSTOM_PREFIX) and len(stage) > len(CUSTOM_PREFIX)


def is_stage(stage: str) -> bool:
    return stage in Stage.ALL or is_custom(stage)


def rank(stage: str) -> int:
    """Workflow order for automatic moves: your own steps count as Staging."""
    return Stage.ALL.index(stage) if stage in Stage.ALL else Stage.ALL.index(Stage.STAGING)


@dataclass
class Column:
    id: str
    name: str
    primary: str                               # where a dropped card goes
    stages: list[str] = field(default_factory=list)
    subtitle: str = ""                         # "" shows the default for its steps (see purpose)

    def to_record(self) -> dict:
        return asdict(self)

    @property
    def purpose(self) -> str:
        """The line under the title: the column's own subtitle, else what its built-in steps hold."""
        return self.subtitle or " · ".join(DEFAULT_SUBTITLES[s] for s in self.builtin) or "Your own step"

    @property
    def builtin(self) -> list[str]:
        return [s for s in self.stages if s in Stage.ALL]


def default_columns() -> list[Column]:
    return [Column(id=s, name=Stage.LABELS[s], primary=s, stages=[s]) for s in Stage.ALL]


def setting_key(profile_id: int) -> str:
    return f"board_columns.{profile_id}"


def validate(columns: list[Column], limits: bool = True) -> list[Column]:
    """Check a set of columns. `limits` enforces 2–6 columns (when saving a change); a board saved before
    those limits still loads as it was."""
    if not columns:
        raise ColumnError(f"The board needs at least {MIN_COLUMNS} columns.")
    if limits and len(columns) < MIN_COLUMNS:
        raise ColumnError(f"The board needs at least {MIN_COLUMNS} columns.")
    if limits and len(columns) > MAX_COLUMNS:
        raise ColumnError(f"The board can have at most {MAX_COLUMNS} columns.")
    seen: list[str] = []
    for col in columns:
        col.name = (col.name or "").strip()
        col.subtitle = (col.subtitle or "").strip()
        if not col.name:
            raise ColumnError("Every column needs a name.")
        if len(col.name) > NAME_MAX:
            raise ColumnError(f"Column names can be at most {NAME_MAX} characters.")
        if len(col.subtitle) > SUBTITLE_MAX:
            raise ColumnError(f"Column subtitles can be at most {SUBTITLE_MAX} characters.")
        if not col.stages or col.primary not in col.stages or not all(is_stage(s) for s in col.stages):
            raise ColumnError(f"Column “{col.name}” is set up incorrectly.")
        seen += col.stages
    missing = [Stage.LABELS[s] for s in Stage.ALL if s not in seen]
    if missing or len(seen) != len(set(seen)):
        steps = ", ".join(Stage.LABELS[s] for s in Stage.ALL)
        raise ColumnError(f"Each of Reelay's own steps ({steps}) must be in exactly one column.")
    return columns


def load(store, profile_id: int) -> list[Column]:
    raw = store.get_setting(setting_key(profile_id), "")
    if not raw:
        return default_columns()
    try:
        columns = validate([Column(**c) for c in json.loads(raw)], limits=False)
    except (ValueError, TypeError, KeyError):
        return default_columns()
    for col in columns:
        if OLD_NAMES.get(col.id) == col.name:
            col.name = Stage.LABELS[col.id]
    return columns


def save(store, profile_id: int, columns: list[Column]) -> list[Column]:
    columns = validate(columns)
    store.set_setting(setting_key(profile_id), json.dumps([c.to_record() for c in columns]))
    return columns


def column_for(columns: list[Column], stage: str) -> Column:
    """The column showing a step (a step whose column is gone shows with Staging)."""
    for col in columns:
        if stage in col.stages:
            return col
    return column_for(columns, Stage.STAGING) if stage != Stage.STAGING else columns[0]


def add(columns: list[Column], name: str, index: int | None = None) -> Column:
    if len(columns) >= MAX_COLUMNS:
        raise ColumnError(f"The board can have at most {MAX_COLUMNS} columns.")
    stage = CUSTOM_PREFIX + uuid.uuid4().hex[:8]
    col = Column(id=stage, name=name.strip() or "New column", primary=stage, stages=[stage])
    columns.insert(len(columns) if index is None else index, col)
    return col


def remove(columns: list[Column], column_id: str) -> tuple[Column, Column]:
    """Remove a column; its built-in steps join the neighbour (the one before
    it, or after it if it's first). Returns (removed, neighbour). Cards in the
    removed column's own step must be moved to neighbour.primary by the caller."""
    if len(columns) <= MIN_COLUMNS:
        raise ColumnError(f"The board needs at least {MIN_COLUMNS} columns.")
    index = next((i for i, c in enumerate(columns) if c.id == column_id), None)
    if index is None:
        raise ColumnError("That column no longer exists.")
    removed = columns.pop(index)
    neighbour = columns[index - 1] if index > 0 else columns[0]
    neighbour.stages += removed.builtin   # drops on the neighbour still go to its own main step
    return removed, neighbour
