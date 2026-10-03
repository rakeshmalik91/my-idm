"""Entry point for My-IDM download manager."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from my_idm.database import Database, APP_DIR
from my_idm.main_window import MainWindow
from my_idm.manager import DownloadManager
from my_idm.paths import backlog_path, ensure_data_dir, logs_dir
from my_idm.styles import DARK_STYLESHEET


DEFAULT_BACKLOG = backlog_path()
LOGS_DIR = logs_dir()
LOG_FILE = LOGS_DIR / "my-idm.log"


def setup_logging(verbose: bool = False):
    # Creates the data directory as a side effect, and reports the offending path plus the
    # MYIDM_DATA_DIR escape hatch if it cannot - a bare OSError here would surface as a stack trace
    # during startup with no hint of what to change.
    ensure_data_dir()
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
        "--no-splash",
        action="store_true",
        help="Disable the startup splash screen",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Internal flag: skip single-instance check when restarting",
    )
    parser.add_argument(
        "--autostart",
        action="store_true",
        help="Start minimized to the tray; set by the launch-at-login registration",
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
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("My-IDM")
        except Exception:
            pass

    # Qt Application
    app = QApplication(sys.argv)
    app.setApplicationName("My-IDM")
    app.setApplicationDisplayName("My-IDM")
    app.setApplicationVersion("1.0.0")
    # Associate the process with our .desktop entry.
    #
    # Without this, Qt derives WM_CLASS from the application name alone and the desktop entry's
    # StartupWMClass never matches. The visible symptom is a window with no taskbar icon and a tray
    # icon the user cannot associate with it, on GNOME and KDE - both of which match strictly.
    # It is also the app id the XDG GlobalShortcuts portal requires for global hotkeys on Wayland
    # (cross-platform.md §4.2), so it is not cosmetic.
    app.setDesktopFileName("my-idm.desktop")
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_STYLESHEET)

    from my_idm.resources import get_app_icon
    app_icon = get_app_icon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    # Single-instance check (skip if --restart flag is set)
    from my_idm.single_instance import SingleInstanceManager, activate_window

    single_instance = SingleInstanceManager()
    payload = {
        "action": "activate",
        "backlog": args.backlog,
        "urls": [u for u in args.urls if u],
    }

    if not args.restart and single_instance.send_message(payload):
        log.info("My-IDM is already running. Signal sent to bring existing window to focus.")
        sys.exit(0)

    if not single_instance.start_server():
        log.warning("Could not start single instance IPC server; proceeding as standalone.")

    # Splash screen
    splash = None
    if not args.no_splash:
        try:
            from my_idm.splash import IDMSplashScreen
            splash = IDMSplashScreen()
            splash.show()
            splash.set_message("Starting My-IDM...", 15)
        except Exception as e:
            log.warning("Could not initialize splash screen: %s", e)
            splash = None

    # Database
    if splash:
        splash.set_message("Opening database...", 30)
    db = Database()
    db.open()

    # Manager
    if splash:
        splash.set_message("Starting download engines...", 55)
    manager = DownloadManager(db)
    manager.start()

    # Main window
    if splash:
        splash.set_message("Loading user interface...", 80)
    window = MainWindow(manager, show_exit_splash=not args.no_splash)

    start_in_tray = manager.general_config.enable_system_tray and manager.general_config.start_minimized
    if args.autostart:
        # Launched by the login item rather than by the user, so the window would be an
        # interruption. Only honoured when a tray actually exists: `main_window` hides to the tray
        # on close but has no icon to come back to if the desktop provides none, and an
        # autostarted instance that starts invisible is unrecoverable without a second launch.
        start_in_tray = QSystemTrayIcon.isSystemTrayAvailable()
        if not start_in_tray:
            log.warning(
                "Started by the login item but no system tray is available; showing the window "
                "instead of launching invisibly."
            )
    if splash:
        splash.set_message("Ready!", 100)
        if start_in_tray:
            splash.close()
        else:
            window.show()
            from PySide6.QtCore import QTimer
            QTimer.singleShot(400, lambda: splash.finish(window))
    else:
        if not start_in_tray:
            window.show()

    # Connect single instance IPC message receiver
    def _on_instance_message(msg: dict):
        log.info("Received IPC activation message from secondary instance: %s", msg)
        activate_window(window)

        # Handle backlog if passed
        b_path = msg.get("backlog")
        if b_path:
            count = manager.process_backlogs(extra_filepath=b_path)
            log.info("Processed %d downloads from secondary instance backlog: %s", count, b_path)

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

    # Load and process backlog files (project root, user home, app dir, and configured locations)
    count = manager.process_backlogs(extra_filepath=args.backlog)
    log.info("Finished processing backlog files (total added: %d)", count)

    # Run
    exit_code = app.exec()

    # Cleanup
    _cleanup()
    log.info("My-IDM shutdown complete")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
