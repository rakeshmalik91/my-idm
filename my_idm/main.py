"""Entry point for My-IDM download manager."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from my_idm.database import Database, APP_DIR
from my_idm.main_window import MainWindow
from my_idm.manager import DownloadManager
from my_idm.styles import DARK_STYLESHEET


DEFAULT_BACKLOG = APP_DIR / "backlog.txt"
LOG_FILE = APP_DIR / "my-idm.log"


def setup_logging(verbose: bool = False):
    APP_DIR.mkdir(parents=True, exist_ok=True)

    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    # File handler
    file_handler = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(fmt))

    # Console handler
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter(fmt))

    logging.root.setLevel(logging.DEBUG)
    logging.root.addHandler(file_handler)
    logging.root.addHandler(console_handler)

    # Quiet noisy loggers
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("PySide6").setLevel(logging.WARNING)


def parse_args():
    parser = argparse.ArgumentParser(
        description="My-IDM — A full-featured download manager"
    )
    parser.add_argument(
        "--backlog", "-b",
        type=str,
        default=None,
        help="Path to a backlog file with URLs (one per line)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose (debug) logging",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    setup_logging(args.verbose)

    log = logging.getLogger("my_idm")
    log.info("Starting My-IDM v1.0.0")

    # Qt Application
    app = QApplication(sys.argv)
    app.setApplicationName("My-IDM")
    app.setApplicationVersion("1.0.0")
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_STYLESHEET)

    # Database
    db = Database()
    db.open()

    # Manager
    manager = DownloadManager(db)
    manager.start()

    # Main window
    window = MainWindow(manager)
    window.show()

    # Load backlog
    backlog_path = args.backlog or str(DEFAULT_BACKLOG)
    if Path(backlog_path).exists():
        count = manager.load_backlog(backlog_path)
        log.info("Loaded %d downloads from backlog: %s", count, backlog_path)

    # Resume incomplete downloads from history
    for entry in db.get_all_downloads():
        if entry.status in ("downloading", "queued"):
            log.info("Auto-resuming: %s (%s)", entry.filename or entry.url, entry.id)
            manager.resume_download(entry.id)

    # Run
    exit_code = app.exec()

    # Cleanup
    manager.stop()
    db.close()
    log.info("My-IDM shutdown complete")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
