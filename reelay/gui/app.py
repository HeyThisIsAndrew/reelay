"""Application entry point: logging, database, theme, crash reporting, main window."""

from __future__ import annotations

import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from .. import APP_ID, APP_NAME, __version__
from ..paths import db_path, log_dir
from ..services import Services
from ..store import Store
from ..tokens import KeyringTokenStore, TokenStore
from .theme import apply_theme

log = logging.getLogger("reelay")


def log_file_path() -> Path:
    return log_dir() / "app.log"


def setup_logging() -> None:
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    handler = RotatingFileHandler(log_file_path(), maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr is not None:  # windowed PyInstaller builds have no console
        console = logging.StreamHandler()
        console.setLevel(logging.WARNING)
        root.addHandler(console)


def install_excepthook() -> None:
    def hook(exc_type, exc, tb):
        log.error("Unhandled error:\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
        if QApplication.instance():
            QMessageBox.critical(None, APP_NAME,
                                 f"Something went wrong:\n\n{exc}\n\nDetails were saved to:\n{log_file_path()}")
    sys.excepthook = hook


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent.parent))
    return base / relative


def create_window(app: QApplication, store: Store, tokens: TokenStore, start_services: bool = False):
    """Build the main window. start_services=True also starts the dispatcher and
    agent API (the real app does; tests don't)."""
    apply_theme(app, store.get_setting("theme", "dark"))
    from . import tips
    tips.install(app, store)
    from .main_window import MainWindow
    services = Services(store, tokens)
    window = MainWindow(services)
    if start_services:
        services.start_background(api=True)
    return window


def main() -> int:
    setup_logging()
    log.info("Starting %s %s", APP_NAME, __version__)
    QCoreApplication.setOrganizationName(APP_ID)
    QCoreApplication.setApplicationName(APP_ID)
    app = QApplication(sys.argv)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(__version__)
    icon = resource_path("packaging/assets/icon.png")
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    install_excepthook()

    store = Store(db_path())
    interrupted = store.reset_interrupted_uploads()  # pre-dispatcher posts only
    if interrupted:
        log.warning("%d upload(s) were interrupted last session", interrupted)
    window = create_window(app, store, KeyringTokenStore(), start_services=True)
    app.aboutToQuit.connect(window.services.stop)  # also covers Cmd-Q without a close event
    window.show()
    code = app.exec()
    store.close()
    return code
