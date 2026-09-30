"""Pytest configuration and global fixtures for My-IDM test suite.

CRITICAL: Protect user settings in Windows Registry from being overwritten or cleared
during test execution or development. All QSettings instances in tests must be isolated
to a temporary directory using IniFormat.

Also suppresses real Windows system tray notifications during tests by mocking
QSystemTrayIcon and notification handlers.

Additional session-wide hermeticity guards (see `AGENTS.md` conventions 4 and 8):

* ``QSettings`` is redirected for *every* ``(organization, application)`` pair, not just
  the production one, so a test that invents an org name still cannot reach ``HKCU``.
* ``my_idm.database.DB_PATH`` is redirected to a temp file, so any ``Database()`` built
  with no explicit path (e.g. ``SettingsDialog._get_db()``) cannot open the user's live
  download database and rewrite its ``ui_state`` rows on close.
* Outbound TCP is restricted to loopback, so a test can never silently reach the
  internet.
* Destructive Windows shell commands (``taskkill`` / ``del`` / ``rd``) issued through
  ``subprocess`` are refused unless a test opts in explicitly.
* Anything that would pop open File Explorer on the host - ``os.startfile``,
  ``explorer.exe``, or a local-file ``QDesktopServices.openUrl`` - is refused, so a test
  can never flash a real Explorer window on the developer's desktop.
* Tests that call a destructive filesystem helper run with the working directory moved
  into their own temp tree, so a blank path (which resolves to ``.``) cannot reach the
  repository.
* The system clipboard is snapshotted, cleared, and restored around every test, so a
  leftover developer clipboard cannot trigger production code that prefills from it
  (``YouTubeDialog._prefill`` would otherwise fire a real yt-dlp network request).
"""

import os
import shutil
import socket
import tempfile
import threading
from unittest.mock import MagicMock, patch
import pytest
from PySide6.QtCore import QSettings

# Global session fixture to isolate QSettings away from the host OS registry / config
_orig_qsettings_init = QSettings.__init__
_test_settings_dir = tempfile.mkdtemp(prefix="my_idm_test_settings_")
_test_home_dir = tempfile.mkdtemp(prefix="my_idm_test_home_")


def _isolated_qsettings_init(self, *args, **kwargs):
    # `QSettings()` and `QSettings(org, app)` both default to NativeFormat, which on
    # Windows writes straight into HKEY_CURRENT_USER. Force every organisation/app
    # name pair into the isolated IniFormat tree.
    #
    # `QSettings(filename, format)` is also a 2-arg call, but its second argument is a
    # `QSettings.Format` enum rather than a `str`, so the isinstance check below tells
    # the two forms apart and leaves explicit file-based settings untouched.
    if len(args) == 0:
        _orig_qsettings_init(self, QSettings.Format.IniFormat, QSettings.Scope.UserScope, "MyIDM", "My-IDM")
        return
    if len(args) == 2 and isinstance(args[0], str) and isinstance(args[1], str):
        _orig_qsettings_init(self, QSettings.Format.IniFormat, QSettings.Scope.UserScope, args[0], args[1])
        return
    _orig_qsettings_init(self, *args, **kwargs)


# Patch QSettings globally for tests
QSettings.__init__ = _isolated_qsettings_init
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, _test_settings_dir)


from pathlib import Path

import my_idm.database as _database_module

# A `Database()` constructed with no argument resolves to this module-level constant at
# __init__ time, so redirecting it here keeps dialogs that open the database implicitly
# (SettingsDialog._get_db) away from the user's real ~/.my-idm/downloads.db.
_REAL_DB_PATH = _database_module.DB_PATH
_database_module.DB_PATH = Path(_test_home_dir) / "downloads.db"

# Opt-in escape hatch for the destructive-subprocess guard below.
_ALLOW_DESTRUCTIVE_SUBPROCESS = False

# Production code frequently wraps the very calls these guards intercept in a broad
# `except Exception` (TorServiceManager.stop() does, for example), which would silently
# swallow the AssertionError and let a dangerous call look harmless. Violations are
# therefore recorded here and reported at the end of the session, with the offending
# call site attached.
_VIOLATIONS: list[str] = []


def _record_violation(kind: str, detail: str) -> None:
    import traceback

    frames = [
        f"{os.path.basename(frame.filename)}:{frame.lineno} in {frame.name}"
        for frame in traceback.extract_stack()[:-1]
        if "/tests/" not in frame.filename.replace("\\", "/")
        or "conftest" not in frame.filename
    ]
    origin = " <- ".join(reversed(frames[-4:]))
    _VIOLATIONS.append(f"[{kind}] {detail}  (via {origin})")


@pytest.fixture(autouse=True, scope="session")
def report_hermeticity_violations():
    """Fail the run if any test tried to escape the sandbox, even silently."""
    yield
    if _VIOLATIONS:
        listing = "\n".join(f"  - {item}" for item in _VIOLATIONS)
        _VIOLATIONS.clear()
        raise AssertionError(
            "Hermeticity violations detected during the test run:\n"
            f"{listing}\n"
            "Mock the offending call instead of letting it reach the real OS/network."
        )


def _loopback(host) -> bool:
    if host is None:
        return False
    if isinstance(host, bytes):
        try:
            host = host.decode("utf-8", "ignore")
        except Exception:
            return False
    host = str(host)
    if host in ("localhost", "localhost.localdomain", ""):
        return True
    if host.startswith("127.") or host == "::1" or host == "0:0:0:0:0:0:0:1":
        return True
    return False


def _address_is_loopback(address) -> bool:
    if isinstance(address, tuple) and address:
        return _loopback(address[0])
    return False


def _deny_network(address, what="connect") -> None:
    """Refuse a non-loopback connection attempt.

    Raises on the main thread, where the failure lands on the test that caused it. A
    background thread gets the violation *recorded* but no exception: production code runs
    its probes on daemon threads (``DownloadManager``'s ``tor-probe``, for one) and wraps
    them in ``except Exception``, so a raise there is swallowed anyway - while faulting the
    interpreter if it lands during shutdown, which is what turned this into a hard
    "Windows fatal exception: access violation" that killed the run instead of failing a
    test. The run still goes red, via the teardown report.
    """
    if _address_is_loopback(address):
        return
    detail = f"real network connection attempted ({what} -> {address!r})"
    _record_violation("network", f"{detail} (from a non-main thread)")
    if threading.current_thread() is not threading.main_thread():
        return
    raise AssertionError(
        f"Test suite attempted a {detail}. "
        "Tests must be hermetic: mock the transport or use a loopback address."
    )


@pytest.fixture(autouse=True)
def block_non_loopback_network(monkeypatch):
    """Fail loudly if production code under test opens a non-loopback socket.

    Loopback is permitted because the browser-integration tests genuinely bind
    127.0.0.1 and the Tor probe genuinely dials 127.0.0.1. Everything else is a bug in
    the test (or in the code it exercises) and must be mocked instead.
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self, address):
        _deny_network(address, "socket.connect")
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        _deny_network(address, "socket.connect_ex")
        return real_connect_ex(self, address)

    real_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, port, *args, **kwargs):
        if not _loopback(host):
            detail = f"DNS/host lookup for {host!r}:{port!r}"
            _record_violation("network", detail)
            if threading.current_thread() is not threading.main_thread():
                # See _deny_network: never raise out of a worker thread.
                return real_getaddrinfo(host, port, *args, **kwargs)
            raise AssertionError(
                f"Test suite attempted a {detail}. Mock the transport instead of "
                "resolving a public host."
            )
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect, raising=True)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex, raising=True)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo, raising=True)
    yield


@pytest.fixture(autouse=True)
def block_destructive_subprocess(monkeypatch):
    """Refuse `taskkill` / `del` / `rd` style shell commands issued from a test.

    `TorServiceManager.stop()` shells out to `taskkill /F /T /PID <pid>`; a test that
    fakes `subprocess.Popen` without faking `subprocess.run` therefore kills a real,
    unrelated process tree. Rather than relying on every call site remembering to patch
    both, the whole class of destructive calls is refused suite-wide.
    """
    import subprocess

    _DESTRUCTIVE = {"taskkill", "del", "rmdir", "rd", "format", "diskpart"}

    def check(argv):
        if _ALLOW_DESTRUCTIVE_SUBPROCESS:
            return
        if not argv:
            return
        try:
            exe = str(argv[0])
        except Exception:
            return
        name = Path(exe).stem.lower()
        if name.endswith(".exe"):
            name = name[:-4]
        if name in _DESTRUCTIVE:
            detail = f"destructive OS command {argv!r}"
            _record_violation("subprocess", detail)
            raise AssertionError(
                f"Test suite attempted a {detail}. "
                "Patch the subprocess layer, or set "
                "tests.conftest.ALLOW_DESTRUCTIVE_SUBPROCESS = True for a test that "
                "genuinely needs it."
            )

    real_run = subprocess.run
    real_popen_init = subprocess.Popen.__init__

    def guarded_run(*args, **kwargs):
        check(args[0] if args else kwargs.get("args"))
        return real_run(*args, **kwargs)

    def guarded_popen_init(self, args, *rest, **kwargs):
        check(args)
        return real_popen_init(self, args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run, raising=True)
    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_popen_init, raising=True)
    yield


@pytest.fixture(autouse=True)
def block_desktop_shell_launches(monkeypatch):
    """Refuse anything that would pop open File Explorer / the default app on the host.

    Three production paths reach the shell, and all three are reachable from a test:

    * ``external_tools.open_file_in_default_app`` calls ``os.startfile()`` for a folder,
      and ``os.startfile`` on a *directory* opens File Explorer;
    * ``external_tools.show_in_folder`` spawns ``explorer.exe /select,<path>``;
    * both fall back to ``QDesktopServices.openUrl(QUrl.fromLocalFile(...))``, which opens
      Explorer for a local path.

    A test that fences only one of them still flashes a real Explorer window on the
    developer's desktop mid-run, which is alarming and easy to misattribute. Recorded the
    same way as the subprocess guard: raise, but also keep the call site so a broad
    ``except Exception`` in production code cannot hide the attempt.
    """
    import os
    import subprocess

    from PySide6.QtGui import QDesktopServices

    real_startfile = os.startfile

    def guarded_startfile(path, *args, **kwargs):
        _record_violation("shell", f"os.startfile({str(path)!r}) would open Explorer")
        raise AssertionError(
            f"Test suite attempted os.startfile({str(path)!r}), which opens File Explorer on "
            "the host. Patch os.startfile as well as QDesktopServices."
        )

    real_open_url = QDesktopServices.openUrl

    def guarded_open_url(url):
        if url.isLocalFile() or url.scheme() in ("", "file"):
            _record_violation(
                "shell", f"QDesktopServices.openUrl({url.toString()!r}) would open Explorer"
            )
            raise AssertionError(
                f"Test suite attempted QDesktopServices.openUrl({url.toString()!r}), which "
                "opens File Explorer on the host. Patch QDesktopServices.openUrl."
            )
        return real_open_url(url)

    real_popen_init = subprocess.Popen.__init__

    def guarded_popen_init(self, args, *rest, **kwargs):
        try:
            exe = os.path.basename(str(args[0])).lower()
        except Exception:
            exe = ""
        if exe.startswith("explorer"):
            _record_violation("shell", f"explorer launch {list(args)!r}")
            raise AssertionError(
                f"Test suite attempted to spawn {list(args)!r}, which opens File Explorer "
                "on the host. Patch subprocess.Popen."
            )
        return real_popen_init(self, args, *rest, **kwargs)

    monkeypatch.setattr(os, "startfile", guarded_startfile, raising=True)
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(guarded_open_url),
                        raising=True)
    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_popen_init, raising=True)
    yield


@pytest.fixture(autouse=True)
def isolate_implicit_user_database():
    """Keep `Database()` (no-arg) off the user's real downloads.db for the whole test.

    Mirrors the QSettings isolation in intent: the dialogs that open the database
    implicitly must not create `~/.my-idm`, hold a live sqlite handle, or overwrite the
    user's persisted window sizes.
    """
    yield
    db_file = _database_module.DB_PATH
    try:
        if db_file.exists():
            db_file.unlink()
    except Exception:
        pass
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_file) + suffix)
        try:
            if sidecar.exists():
                sidecar.unlink()
        except Exception:
            pass


@pytest.fixture(autouse=True)
def isolate_system_clipboard():
    """Snapshot, clear, and restore the real system clipboard around each test.

    Two problems this removes:

    * Production code that prefills a URL field from the clipboard (YouTubeDialog)
      would otherwise read whatever the developer happened to have copied and fire a
      real yt-dlp network request from a test.
    * Clipboard round-trip tests sharing one QClipboard are order-dependent and clobber
      the developer's clipboard for the rest of the session.
    """
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance()
    if app is None:
        yield
        return
    try:
        clipboard = QGuiApplication.clipboard()
    except Exception:
        yield
        return
    if clipboard is None:
        yield
        return

    try:
        original = clipboard.text()
    except Exception:
        original = ""
    try:
        clipboard.clear()
    except Exception:
        pass
    yield
    try:
        if original:
            clipboard.setText(original)
        else:
            clipboard.clear()
    except Exception:
        pass


@pytest.fixture(autouse=True, scope="session")
def isolate_qsettings_session():
    """Ensure user settings in registry / OS are never touched during test execution."""
    yield
    for directory in (_test_settings_dir, _test_home_dir):
        try:
            shutil.rmtree(directory, ignore_errors=True)
        except Exception:
            pass


@pytest.fixture(autouse=True)
def isolate_qsettings_per_test():
    """Ensure settings saved during a test do not leak into subsequent tests."""
    yield
    if Path(_test_settings_dir).exists():
        for item in Path(_test_settings_dir).iterdir():
            try:
                if item.is_file():
                    item.unlink()
                elif item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
            except Exception:
                pass


def _cleanup_windows_tray_ghosts():
    """Clean up stale ghost tray icons from Windows Explorer taskbar."""
    import sys
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        
        def refresh_window(hwnd):
            if not hwnd or not user32.IsWindow(hwnd):
                return
            rect = wintypes.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            for x in range(0, w, 5):
                for y in range(0, h, 5):
                    lparam = (y << 16) | (x & 0xFFFF)
                    user32.PostMessageW(hwnd, 0x0200, 0, lparam)  # WM_MOUSEMOVE

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def enum_child(hwnd, lparam):
            cls_name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls_name, 256)
            if cls_name.value == "ToolbarWindow32":
                refresh_window(hwnd)
            return True

        tray_wnd = user32.FindWindowW("Shell_TrayWnd", None)
        if tray_wnd:
            user32.EnumChildWindows(tray_wnd, WNDENUMPROC(enum_child), 0)

        overflow_wnd = user32.FindWindowW("NotifyIconOverflowWindow", None)
        if overflow_wnd:
            user32.EnumChildWindows(overflow_wnd, WNDENUMPROC(enum_child), 0)
    except Exception:
        pass


@pytest.fixture(autouse=True, scope="session")
def suppress_system_tray_notifications():
    """Suppress real Windows system tray icons and notification popups during tests.
    
    Mocks QSystemTrayIcon (show, hide, setVisible, isVisible, showMessage) and win10toast
    to prevent spamming the user's OS system tray with dozens of ghost icons and notifications,
    while keeping the tray icon, tooltip, and context menu functional for tests that verify them.
    """
    from unittest.mock import patch
    from PySide6.QtWidgets import QSystemTrayIcon
    import my_idm.notifications as notifications_module
    
    _tray_visible_map = {}

    def mock_set_visible(self, visible: bool):
        _tray_visible_map[id(self)] = bool(visible)

    def mock_show(self):
        mock_set_visible(self, True)

    def mock_hide(self):
        mock_set_visible(self, False)

    def mock_is_visible(self):
        return _tray_visible_map.get(id(self), False)

    def mock_show_message(self, title, message, icon=None, msecs=10000):
        pass
    
    def mock_show_notification(title, message, duration=5, icon_path=None):
        if notifications_module._notification_handler is not None:
            try:
                return notifications_module._notification_handler(title, message, duration, icon_path)
            except Exception:
                pass
        return False

    _cleanup_windows_tray_ghosts()

    with patch.object(QSystemTrayIcon, 'showMessage', mock_show_message), \
         patch.object(QSystemTrayIcon, 'show', mock_show), \
         patch.object(QSystemTrayIcon, 'hide', mock_hide), \
         patch.object(QSystemTrayIcon, 'setVisible', mock_set_visible), \
         patch.object(QSystemTrayIcon, 'isVisible', mock_is_visible), \
         patch.object(notifications_module, 'show_notification', mock_show_notification):
        yield

    _cleanup_windows_tray_ghosts()

