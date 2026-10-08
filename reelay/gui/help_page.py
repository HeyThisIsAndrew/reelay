"""Help page: the user guide, shown offline from help.md bundled with the app."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QTextBrowser, QVBoxLayout, QWidget

HELP_FILE = Path(__file__).with_name("help.md")


def help_markdown() -> str:
    try:
        return HELP_FILE.read_text(encoding="utf-8")
    except OSError:
        return "# Reelay Help\n\nThe help file is missing from this copy of Reelay."


class HelpPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 18, 24, 18)
        self.browser = QTextBrowser(objectName="HelpText")
        self.browser.setOpenExternalLinks(True)
        self.browser.document().setDocumentMargin(18)
        self.browser.setMarkdown(help_markdown())
        lay.addWidget(self.browser)
