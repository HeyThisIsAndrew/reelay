"""Even card grids: every row full, every card the same size.

Card grids (Accounts, Download Sources, the Automations recipes) never leave an orphan in the last row: the
number of columns is the most that divides the number of cards evenly. If a count only divides by 1 (5, 7…),
that's a content problem to fix (merge, split or move a card), not something the grid papers over.

The Library's video grid is different: it's a paged list of videos, where a short last row is normal, so it
doesn't use this.
"""

from __future__ import annotations

WIDE, MEDIUM = 1180, 720          # content width (px) for 3 and 2 columns


def balanced_columns(count: int, max_columns: int) -> int:
    """The most columns (<= max_columns) that divide count evenly; 1 if none do."""
    if count <= 0 or max_columns <= 1:
        return 1
    return next(c for c in range(min(max_columns, count), 0, -1) if count % c == 0)


def max_columns_for(width: int, wide: int = WIDE, medium: int = MEDIUM) -> int:
    """How many columns a content area this wide can hold: 3, 2 or 1."""
    return 3 if width >= wide else 2 if width >= medium else 1


def place_even(layout, widgets: list, columns: int) -> None:
    """Lay `widgets` out in `columns` equal columns (cells fill their row: no top alignment), then give them
    all the same height."""
    for w in widgets:
        layout.removeWidget(w)
    for n, w in enumerate(widgets):
        layout.addWidget(w, n // columns, n % columns)
    for c in range(max(layout.columnCount(), columns)):
        layout.setColumnStretch(c, 1 if c < columns else 0)
    for r in range(layout.rowCount()):
        layout.setRowStretch(r, 0)
    equalize_heights(widgets)


def equalize_heights(widgets: list) -> int:
    """Give every widget the height of the tallest one's natural size. Returns that height. Call it again
    whenever their content changes."""
    shown = [w for w in widgets if not w.isHidden()]
    if not shown:
        return 0
    height = max(w.sizeHint().height() for w in shown)
    for w in shown:
        w.setFixedHeight(height)
    return height
