"""Windows toast notification utilities."""

import logging
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


def show_notification(title: str, message: str, duration: int = 5, icon_path: Optional[str] = None) -> bool:
    """Show a Windows toast notification.
    
    Args:
        title: Notification title
        message: Notification message body
        duration: Duration in seconds
        icon_path: Optional path to .ico file
    
    Returns:
        True if notification was sent, False otherwise
    """
    global _notification_handler
    if _notification_handler is not None:
        try:
            if _notification_handler(title, message, duration, icon_path):
                return True
        except Exception as exc:
            log.warning("Custom notification handler failed: %s", exc)

    if not _HAS_WIN10TOAST or _toaster is None:
        log.debug("win10toast not available, skipping notification")
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