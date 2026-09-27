"""Pytest configuration and global fixtures for My-IDM test suite.

CRITICAL: Protect user settings in Windows Registry from being overwritten or cleared
during test execution or development. All QSettings instances in tests must be isolated
to a temporary directory using IniFormat.

Also suppresses real Windows system tray notifications during tests by mocking
QSystemTrayIcon and notification handlers.
"""

import shutil
import tempfile
from unittest.mock import MagicMock, patch
import pytest
from PySide6.QtCore import QSettings

# Global session fixture to isolate QSettings away from the host OS registry / config
_orig_qsettings_init = QSettings.__init__
_test_settings_dir = tempfile.mkdtemp(prefix="my_idm_test_settings_")


def _isolated_qsettings_init(self, *args, **kwargs):
    # If called with default app ("MyIDM", "My-IDM") or no args, force isolated IniFormat in temp dir
    if len(args) == 0:
        _orig_qsettings_init(self, QSettings.Format.IniFormat, QSettings.Scope.UserScope, "MyIDM", "My-IDM")
        return
    if len(args) == 2 and args[0] == "MyIDM" and args[1] == "My-IDM":
        _orig_qsettings_init(self, QSettings.Format.IniFormat, QSettings.Scope.UserScope, "MyIDM", "My-IDM")
        return
    _orig_qsettings_init(self, *args, **kwargs)


# Patch QSettings globally for tests
QSettings.__init__ = _isolated_qsettings_init
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, _test_settings_dir)


from pathlib import Path


@pytest.fixture(autouse=True, scope="session")
def isolate_qsettings_session():
    """Ensure user settings in registry / OS are never touched during test execution."""
    yield
    try:
        shutil.rmtree(_test_settings_dir, ignore_errors=True)
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

