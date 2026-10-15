"""Batch-rename preview and the naming-token help."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QTableWidgetItem, QVBoxLayout

from ..library import RenameStep
from ..naming import TOKENS
from .theme import set_prop
from .widgets import table as make_table


class RenamePreviewDialog(QDialog):
    """Shows every old → new filename before anything on disk changes."""

    def __init__(self, steps: list[RenameStep], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Rename Downloaded Files")
        self.resize(760, 460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 16)
        lay.setSpacing(10)

        title = QLabel(f"Rename {len(steps)} file{'s' if len(steps) != 1 else ''}?")
        title.setObjectName("DetailTitle")
        sub = QLabel("Files are renamed in place in your output folder, and the manifest and "
                     "metadata are updated to match. Nothing is re-downloaded.")
        sub.setWordWrap(True)
        sub.setProperty("muted", True)
        lay.addWidget(title)
        lay.addWidget(sub)

        table = make_table(["CURRENT NAME", "NEW NAME"], rows=len(steps), select="none", grid=False)
        table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for row, step in enumerate(steps):
            table.setItem(row, 0, QTableWidgetItem(step.old))
            table.setItem(row, 1, QTableWidgetItem(step.new))
        lay.addWidget(table, 1)

        buttons = QDialogButtonBox()
        cancel = buttons.addButton("Cancel", QDialogButtonBox.RejectRole)
        ok = buttons.addButton(f"Rename {len(steps)} File{'s' if len(steps) != 1 else ''}",
                               QDialogButtonBox.AcceptRole)
        set_prop(ok, "primary")
        ok.setDefault(True)
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        lay.addWidget(buttons)


def tokens_help_html() -> str:
    rows = "".join(f"<tr><td style='padding-right:12px'><b>{{{k}}}</b></td><td>{v}</td></tr>"
                   for k, v in TOKENS.items())
    return ("<b>Naming template</b><br>Type any text plus these tokens:"
            f"<table cellpadding='2'>{rows}</table>"
            "Example: <b>{date}_{custom}</b> → 20261002_letting-go.mp4")
