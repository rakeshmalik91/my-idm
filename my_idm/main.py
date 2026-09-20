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
LOGS_DIR = APP_DIR / "logs"
LOG_FILE = LOGS_DIR / "my-idm.log"


def setup_logging(verbose: bool = False):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

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
    parser.add_argument(
        "urls",
        nargs="*",
        default=[],
        help="Optional download URL(s), magnet link(s), or .torrent file(s)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    setup_logging(args.verbose)

    log = logging.getLogger("my_idm")
    log.info("Starting My-IDM v1.0.0")

    # Windows taskbar icon integration
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("myidm.downloadmanager.app.1")
        except Exception:
            pass

    # Qt Application
    app = QApplication(sys.argv)
    app.setApplicationName("My-IDM")
    app.setApplicationVersion("1.0.0")
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_STYLESHEET)

    from my_idm.resources import get_app_icon
    app_icon = get_app_icon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    # Single-instance check
    from my_idm.single_instance import SingleInstanceManager, activate_window

    single_instance = SingleInstanceManager()
    payload = {
        "action": "activate",
        "backlog": args.backlog,
        "urls": [u for u in args.urls if u],
    }

    if single_instance.send_message(payload):
        log.info("My-IDM is already running. Signal sent to bring existing window to focus.")
        sys.exit(0)

    if not single_instance.start_server():
        log.warning("Could not start single instance IPC server; proceeding as standalone.")

    # Database
    db = Database()
    db.open()

    # Manager
    manager = DownloadManager(db)
    manager.start()

    # Main window
    window = MainWindow(manager)
    window.show()

    # Connect single instance IPC message receiver
    def _on_instance_message(msg: dict):
        log.info("Received IPC activation message from secondary instance: %s", msg)
        activate_window(window)

        # Handle backlog if passed
        b_path = msg.get("backlog")
        if b_path and Path(b_path).exists():
            count = manager.load_backlog(b_path)
            log.info("Loaded %d downloads from secondary instance backlog: %s", count, b_path)

        # Handle urls if passed
        for u in msg.get("urls", []):
            if u:
                manager.add_download(u)

    single_instance.message_received.connect(_on_instance_message)

    # Initial URLs from CLI args
    for u in args.urls:
        if u:
            manager.add_download(u)

    # Wire clean shutdown on application quit
    _cleaned_up = False

    def _cleanup():
        nonlocal _cleaned_up
        if _cleaned_up:
            return
        _cleaned_up = True
        single_instance.close()
        manager.stop()
        db.close()

    app.aboutToQuit.connect(_cleanup)

    # Load backlog
    backlog_path = args.backlog or str(DEFAULT_BACKLOG)
    if Path(backlog_path).exists():
        count = manager.load_backlog(backlog_path)
        log.info("Loaded %d downloads from backlog: %s", count, backlog_path)

    # Run
    exit_code = app.exec()

    # Cleanup
    _cleanup()
    log.info("My-IDM shutdown complete")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
