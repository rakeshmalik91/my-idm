"""Desktop notification utilities, per platform.

The registered handler is the primary path: the GUI registers ``QSystemTrayIcon.showMessage`` at
startup, which works on all three platforms when a tray exists. The fallbacks here matter for the
cases the tray does not cover - no system tray (GNOME without AppIndicator, some Wayland
compositors), or notifications raised before the GUI finishes starting.

Windows uses ``win10toast``; Linux uses ``notify-send``; macOS uses ``osascript``. PyObjC is
deliberately *not* used, so it stays an optional packaging extra rather than a hard dependency.
"""

import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Delegate handler type: (title, message, duration_secs, icon_path) -> bool
_notification_handler: Optional[Callable[[str, str, int, Optional[str]], bool]] = None


def register_notification_handler(handler: Callable[[str, str, int, Optional[str]], bool]) -> None:
    """Register a custom notification handler (e.g. QSystemTrayIcon from GUI)."""
    global _notification_handler
    _notification_handler = handler


def unregister_notification_handler(handler: Optional[Callable] = None) -> None:
    """Unregister the active notification handler."""
    global _notification_handler
    if handler is None or _notification_handler == handler:
        _notification_handler = None


try:
    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning, message=r".*pkg_resources is deprecated.*")
        from win10toast import ToastNotifier
    _toaster = ToastNotifier()
    # Safeguard _show_toast to prevent unhandled background thread exceptions
    # (e.g. Shell_NotifyIcon failure in headless tests or non-interactive sessions)
    _orig_show_toast = _toaster._show_toast

    def _safe_show_toast(*args, **kwargs):
        try:
            return _orig_show_toast(*args, **kwargs)
        except Exception as _exc:
            log.debug("Toast notification suppressed: %s", _exc)

    _toaster._show_toast = _safe_show_toast
    _HAS_WIN10TOAST = True
except ImportError:
    _toaster = None
    _HAS_WIN10TOAST = False


def _notify_via_notify_send(title: str, message: str, duration: int, icon_path: Optional[str]) -> bool:
    """Linux: `notify-send`, the libnotify CLI.

    Preferred over the D-Bus binding because it needs no extra Python package, and
    `notify-send` is present wherever libnotify is. Arguments are passed as a list, never a
    shell string: the message contains a filename, which is attacker-influenced from a download
    name, so a shell would be an injection point.
    """
    executable = shutil.which("notify-send")
    if not executable:
        return False
    cmd = [
        executable,
        "--app-name=My-IDM",
        f"--expire-time={int(duration) * 1000}",
    ]
    if icon_path:
        # Only pass the icon if it exists; notify-send treats a missing path as a hard error
        # rather than ignoring it, which would lose the notification entirely.
        if Path(icon_path).is_file():
            cmd.append(f"--icon={icon_path}")
    cmd += [title, message]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=10)
        return proc.returncode == 0
    except Exception as exc:
        log.debug("notify-send failed: %s", exc)
        return False


def _notify_via_osascript(title: str, message: str, duration: int, icon_path: Optional[str]) -> bool:
    """macOS: `osascript` with the `display notification` Apple Event.

    Uses the scripting bridge rather than PyObjC so that PyObjC stays an optional extra
    (`antivirus.md`/`cross-platform.md` §5) instead of a hard dependency.

    The title and message are interpolated into AppleScript source, so both are escaped for the
    language. A quote or backslash in a filename would otherwise end the string early - and
    because the argument is argv rather than a shell string, that injection is confined to
    AppleScript, which is still not something a download name should be able to do.
    """
    executable = shutil.which("osascript")
    if not executable:
        return False

    def esc(text: str) -> str:
        return (
            text.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\r\n", " ")
            .replace("\n", " ")
            .replace("\r", " ")
        )

    script = (
        f'display notification "{esc(message)}" '
        f'with title "{esc(title)}" '
        f'subtitle "My-IDM"'
    )
    try:
        proc = subprocess.run(
            [executable, "-e", script], capture_output=True, timeout=10
        )
        return proc.returncode == 0
    except Exception as exc:
        log.debug("osascript notification failed: %s", exc)
        return False


#: Tried in order after the registered handler. First success wins; all failures return False
#: without raising, because a notification is a courtesy and must never abort a download's
#: completion path.
_PLATFORM_FALLBACKS = {
    "win32": (),
    "darwin": (_notify_via_osascript,),
    "default": (_notify_via_notify_send,),
}


def show_notification(title: str, message: str, duration: int = 5, icon_path: Optional[str] = None) -> bool:
    """Show a desktop notification using the best mechanism this platform offers.

    Order: the registered handler (the GUI registers ``QSystemTrayIcon.showMessage``), then the
    platform fallbacks, then Windows ``win10toast``.

    Every backend returns ``bool`` and swallows its own errors. A notification is a courtesy - the
    caller is usually a download completing - and a failure here must not become an exception in
    the middle of that.
    """
    global _notification_handler
    if _notification_handler is not None:
        try:
            if _notification_handler(title, message, duration, icon_path):
                return True
        except Exception as exc:
            log.warning("Custom notification handler failed: %s", exc)

    key = "win32" if sys.platform == "win32" else ("darwin" if sys.platform == "darwin" else "default")
    for backend in _PLATFORM_FALLBACKS[key]:
        try:
            if backend(title, message, duration, icon_path):
                return True
        except Exception as exc:
            log.debug("Notification backend %s failed: %s", backend.__name__, exc)

    if not _HAS_WIN10TOAST or _toaster is None:
        log.debug("No notification backend available for %s", sys.platform)
        return False

    try:
        _toaster.show_toast(
            title,
            message,
            duration=duration,
            icon_path=icon_path,
            threaded=True
        )
        return True
    except Exception as e:
        log.warning("Failed to show Windows notification: %s", e)
        return False


def notify_browser_download_caught(filename: str, url: str = "") -> bool:
    """Show notification when a download is caught from browser extension."""
    title = "Download Captured"
    if filename:
        message = f"Added: {filename}"
    else:
        message = "Download added from browser"
    if url:
        message += f"\n{url[:80]}{'...' if len(url) > 80 else ''}"
    return show_notification(title, message, duration=5)


def notify_download_complete(filename: str) -> bool:
    """Show notification when a download completes."""
    title = "Download Complete"
    message = f"Finished: {filename}"
    return show_notification(title, message, duration=8)


def notify_download_error(filename: str, error: str) -> bool:
    """Show notification when a download fails."""
    title = "Download Failed"
    message = f"Failed: {filename}\n{error[:100]}"
    return show_notification(title, message, duration=10)


def notify_clipboard_download_captured(
    filenames: list[str], skipped: int = 0
) -> bool:
    """Show notification when clipboard capture adds one or more downloads.

    Silent capture is the failure mode worth designing against: the clipboard monitor adds a
    download with no visible sign, and a user who does not realise that is what happened has no
    way to connect the new row to the thing they copied. The browser equivalent
    (:func:`notify_browser_download_caught`) already notifies for the same reason.

    A batch of one is named, because a lone filename is actionable; a batch of many is counted,
    because a wall of filenames is not.
    """
    if not filenames:
        return False
    title = "Captured from Clipboard"
    if len(filenames) == 1:
        message = f"Added: {filenames[0]}"
    else:
        message = f"Added {len(filenames)} downloads from the clipboard"
    if skipped:
        message += f"\n{skipped} over the per-copy limit were skipped"
    return show_notification(title, message, duration=5)
