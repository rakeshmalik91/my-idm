"""General configuration and preferences for My-IDM."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QSettings

from my_idm.database import APP_DIR
from my_idm.utils import normalize_path

DEFAULT_DOWNLOADS_DIR = normalize_path(Path.home() / "Downloads")

# Default yt-dlp format selector: best video up to 1080p + best audio, falling back to best.
DEFAULT_YTDLP_FORMAT = "bestvideo[height<=1080]+bestaudio/best"

# Playlist/channel entries listed per YouTube analysis.
DEFAULT_YTDLP_PLAYLIST_LIMIT = 10
MIN_YTDLP_PLAYLIST_LIMIT = 1
MAX_YTDLP_PLAYLIST_LIMIT = 500

DEFAULT_CLIPBOARD_MIN_FILE_SIZE_KB = 1024
DEFAULT_CLIPBOARD_IGNORED_EXTENSIONS = [
    ".txt", ".htm", ".html", ".jpg", ".jpeg", ".png", ".gif", ".webp"
]

# Gap inserted between the starts of a segmented download's segment requests, in ms.
# 0 = disabled, which is also the default: the segments then open their connections in one
# burst, the behaviour this app has always had.
DEFAULT_SEGMENT_START_DELAY_MS = 0
# Ceiling for the stored preference. The *total* the stagger may add before the last segment
# starts is capped separately, in `http_engine.MAX_SEGMENT_STAGGER_TOTAL_S`.
MAX_SEGMENT_START_DELAY_MS = 2000


def normalize_extension_list(extensions: Any) -> list[str]:
    """Parse comma/space/semicolon separated string or list of extensions into cleaned list with leading dots."""
    import re

    if isinstance(extensions, str):
        raw_items = [x.strip() for x in re.split(r"[,;\s]+", extensions) if x.strip()]
    elif isinstance(extensions, (list, tuple, set)):
        raw_items = [str(x).strip() for x in extensions if str(x).strip()]
    else:
        raw_items = []

    result = []
    seen = set()
    for item in raw_items:
        ext = item.lower()
        if not ext.startswith("."):
            ext = f".{ext}"
        if ext not in seen:
            seen.add(ext)
            result.append(ext)
    return result


def clamp_ytdlp_playlist_limit(value: Any) -> int:
    """Coerce a stored playlist limit into a usable range.

    ``None`` or an unparseable value falls back to the default; numbers are
    clamped, so a stored ``0`` becomes the minimum rather than the default.
    """
    if value is None:
        return DEFAULT_YTDLP_PLAYLIST_LIMIT
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_YTDLP_PLAYLIST_LIMIT
    return max(MIN_YTDLP_PLAYLIST_LIMIT, min(MAX_YTDLP_PLAYLIST_LIMIT, limit))


def clamp_segment_start_delay(value: Any) -> int:
    """Coerce a stored segment stagger into ``[0, MAX_SEGMENT_START_DELAY_MS]``.

    Unparseable input falls back to the default rather than to zero-by-accident: a corrupt
    preference must not silently turn a stagger the user asked for back off.
    """
    try:
        delay = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SEGMENT_START_DELAY_MS
    return max(0, min(delay, MAX_SEGMENT_START_DELAY_MS))


@dataclass
class GeneralConfig:
    """Stores user preferences for downloads and application behavior."""

    default_save_path: str = DEFAULT_DOWNLOADS_DIR
    remember_last_save_path: bool = False
    last_save_path: str = ""
    default_segments: int = 8
    # Gap between one segment's first request and the previous one's, in ms (0 = no stagger).
    # Off by default: the last of N segments waits (N-1) x this before its first byte, so on a
    # server that does not care about connection bursts it is a certain latency cost for no
    # benefit. Raise it when a host rate-limits them - a 429 the moment a download starts.
    segment_start_delay_ms: int = DEFAULT_SEGMENT_START_DELAY_MS
    max_concurrent_downloads: int = 3
    max_retries: int = 5
    retry_delay: float = 2.0
    retry_backoff_factor: float = 2.0
    retry_max_delay: float = 60.0
    retry_exponential_backoff: bool = True
    auto_resume_startup: bool = True
    notify_on_completion: bool = True
    backlog_locations: list[str] = field(default_factory=list)
    clear_backlog_after_load: bool = True
    backlog_poll_interval: int = 60
    backlog_poll_enabled: bool = True
    metadata_fetch_timeout_days: int = 1
    # Refuse to start a download the volume cannot hold. Off-by-default would let a 40 GB
    # download run for an hour and fail at 95%, so it is on unless the user says otherwise.
    disk_space_check: bool = True
    # Slack required on top of the download size, so a download that exactly fills the
    # volume is refused rather than leaving the disk at 100%.
    disk_space_headroom_mb: int = 256
    enable_system_tray: bool = True
    minimize_to_tray: bool = True
    close_to_tray: bool = True
    start_minimized: bool = False
    # Launch My-IDM when the user logs in. Opt-in for the same reason clipboard capture is: an app
    # that starts itself unbidden is a surprise, and a user surprised by it disables it for good.
    # The OS registration itself lives in `my_idm.autostart`; this flag is the user's intent, and
    # `autostart.reconcile` is called only when the settings checkbox actually moves.
    launch_at_login: bool = False
    # Clipboard capture watches what the user copies, so it is opt-in: reading the clipboard
    # without being asked is surveillance, and a user who is surprised by it turns it off and
    # does not turn it on again.
    clipboard_monitor_enabled: bool = False
    # Ceiling on one copy event. A pasted generated list must not become thousands of rows.
    clipboard_monitor_max_urls: int = 20
    # Minimum file size in KB to capture (default 1024 KB = 1 MB; 0 = no minimum).
    clipboard_min_file_size_kb: int = DEFAULT_CLIPBOARD_MIN_FILE_SIZE_KB
    # File extensions to ignore when capturing URLs from clipboard.
    clipboard_ignored_extensions: list[str] = field(
        default_factory=lambda: list(DEFAULT_CLIPBOARD_IGNORED_EXTENSIONS)
    )
    # A global hotkey claims a chord system-wide, so it is also opt-in and needs a real
    # modifier: a bare key would swallow that key in every other application on the desktop.
    capture_hotkey_enabled: bool = False
    capture_hotkey_sequence: str = "Ctrl+Alt+D"

    def get_retry_delay(self, attempt: int) -> float:
        """Calculate retry delay in seconds for a given attempt index (0-indexed)."""
        if not self.retry_exponential_backoff:
            return max(0.1, float(self.retry_delay))
        factor = max(1.0, float(self.retry_backoff_factor))
        delay = float(self.retry_delay) * (factor ** max(0, attempt))
        return max(0.1, min(delay, float(self.retry_max_delay)))

    def get_effective_save_path(self) -> str:
        """Returns the directory to prefill for a new download."""
        if self.remember_last_save_path and self.last_save_path and os.path.isdir(self.last_save_path):
            return self.last_save_path
        if self.default_save_path and os.path.isdir(self.default_save_path):
            return self.default_save_path
        return DEFAULT_DOWNLOADS_DIR

    @property
    def effective_max_concurrent(self) -> int:
        """The global concurrency ceiling, with the legacy ``<= 0`` meaning resolved.

        ``max_concurrent_downloads`` has always treated 0 as "use the default of 3", but that
        fallback was retyped at four separate call sites and had already drifted once. It is a
        property so there is exactly one answer.

        Note this is a *global* ceiling only. Per-queue ``max_concurrent`` lives on the
        ``queues`` row because a queue's limit is data, not a preference — see
        ``docs/architecture/queues.md``.
        """
        return self.max_concurrent_downloads if self.max_concurrent_downloads > 0 else 3

    def get_effective_backlog_locations(self) -> list[str]:
        """Returns the list of places (folders or files) to scan for backlog files."""
        if self.backlog_locations:
            cleaned = []
            seen = set()
            for loc in self.backlog_locations:
                s = str(loc).strip()
                if not s:
                    continue
                norm = normalize_path(s)
                if norm.lower() not in seen:
                    seen.add(norm.lower())
                    cleaned.append(norm)
            if cleaned:
                return cleaned

        # Default places: Project root (cwd), user app data dir, user home dir
        defaults = [
            normalize_path(Path.cwd()),
            normalize_path(APP_DIR),
            normalize_path(Path.home()),
        ]
        result = []
        seen = set()
        for p in defaults:
            if p.lower() not in seen:
                seen.add(p.lower())
                result.append(p)
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_save_path": self.default_save_path,
            "remember_last_save_path": self.remember_last_save_path,
            "last_save_path": self.last_save_path,
            "default_segments": self.default_segments,
            "segment_start_delay_ms": self.segment_start_delay_ms,
            "max_concurrent_downloads": self.max_concurrent_downloads,
            "max_retries": self.max_retries,
            "retry_delay": self.retry_delay,
            "retry_backoff_factor": self.retry_backoff_factor,
            "retry_max_delay": self.retry_max_delay,
            "retry_exponential_backoff": self.retry_exponential_backoff,
            "auto_resume_startup": self.auto_resume_startup,
            "notify_on_completion": self.notify_on_completion,
            "backlog_locations": list(self.backlog_locations),
            "clear_backlog_after_load": self.clear_backlog_after_load,
            "backlog_poll_interval": self.backlog_poll_interval,
            "backlog_poll_enabled": self.backlog_poll_enabled,
            "metadata_fetch_timeout_days": self.metadata_fetch_timeout_days,
        "disk_space_check": self.disk_space_check,
        "disk_space_headroom_mb": self.disk_space_headroom_mb,
            "enable_system_tray": self.enable_system_tray,
            "minimize_to_tray": self.minimize_to_tray,
            "close_to_tray": self.close_to_tray,
            "start_minimized": self.start_minimized,
            "launch_at_login": self.launch_at_login,
            "clipboard_monitor_enabled": self.clipboard_monitor_enabled,
            "clipboard_monitor_max_urls": self.clipboard_monitor_max_urls,
            "clipboard_min_file_size_kb": self.clipboard_min_file_size_kb,
            "clipboard_ignored_extensions": list(self.clipboard_ignored_extensions),
            "capture_hotkey_enabled": self.capture_hotkey_enabled,
            "capture_hotkey_sequence": self.capture_hotkey_sequence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GeneralConfig:
        raw_locs = data.get("backlog_locations", [])
        if isinstance(raw_locs, list):
            locs = [str(x) for x in raw_locs if str(x).strip()]
        elif isinstance(raw_locs, str) and raw_locs.strip():
            locs = [x.strip() for x in raw_locs.split(";") if x.strip()]
        else:
            locs = []

        return cls(
            default_save_path=str(data.get("default_save_path", DEFAULT_DOWNLOADS_DIR)),
            remember_last_save_path=bool(data.get("remember_last_save_path", False)),
            last_save_path=str(data.get("last_save_path", "")),
            default_segments=int(data.get("default_segments", 8)),
            segment_start_delay_ms=clamp_segment_start_delay(
                data.get("segment_start_delay_ms", DEFAULT_SEGMENT_START_DELAY_MS)
            ),
            max_concurrent_downloads=int(data.get("max_concurrent_downloads", 3)),
            max_retries=int(data.get("max_retries", 5)),
            retry_delay=float(data.get("retry_delay", 2.0)),
            retry_backoff_factor=float(data.get("retry_backoff_factor", 2.0)),
            retry_max_delay=float(data.get("retry_max_delay", 60.0)),
            retry_exponential_backoff=bool(data.get("retry_exponential_backoff", True)),
            auto_resume_startup=bool(data.get("auto_resume_startup", True)),
            notify_on_completion=bool(data.get("notify_on_completion", True)),
            backlog_locations=locs,
            clear_backlog_after_load=bool(data.get("clear_backlog_after_load", True)),
            backlog_poll_interval=int(data.get("backlog_poll_interval", 60)),
            backlog_poll_enabled=bool(data.get("backlog_poll_enabled", True)),
            metadata_fetch_timeout_days=int(data.get("metadata_fetch_timeout_days", 1)),
        disk_space_check=bool(data.get("disk_space_check", True)),
        disk_space_headroom_mb=max(0, int(data.get("disk_space_headroom_mb", 256))),
            enable_system_tray=bool(data.get("enable_system_tray", True)),
            minimize_to_tray=bool(data.get("minimize_to_tray", True)),
            close_to_tray=bool(data.get("close_to_tray", True)),
            start_minimized=bool(data.get("start_minimized", False)),
            launch_at_login=bool(data.get("launch_at_login", False)),
            clipboard_monitor_enabled=bool(data.get("clipboard_monitor_enabled", False)),
            clipboard_monitor_max_urls=max(
                1, int(data.get("clipboard_monitor_max_urls", 20))
            ),
            clipboard_min_file_size_kb=max(
                0, int(data.get("clipboard_min_file_size_kb", DEFAULT_CLIPBOARD_MIN_FILE_SIZE_KB))
            ),
            clipboard_ignored_extensions=normalize_extension_list(
                data.get("clipboard_ignored_extensions", DEFAULT_CLIPBOARD_IGNORED_EXTENSIONS)
            ),
            capture_hotkey_enabled=bool(data.get("capture_hotkey_enabled", False)),
            capture_hotkey_sequence=str(
                data.get("capture_hotkey_sequence", "Ctrl+Alt+D")
            ) or "Ctrl+Alt+D",
        )

    def save(self, settings: Optional[QSettings] = None):
        """Persists general preferences into QSettings."""
        if settings is None:
            settings = QSettings("MyIDM", "My-IDM")
        settings.beginGroup("General")
        settings.setValue("default_save_path", self.default_save_path)
        settings.setValue("remember_last_save_path", self.remember_last_save_path)
        settings.setValue("last_save_path", self.last_save_path)
        settings.setValue("default_segments", self.default_segments)
        settings.setValue("segment_start_delay_ms", self.segment_start_delay_ms)
        settings.setValue("max_concurrent_downloads", self.max_concurrent_downloads)
        settings.setValue("max_retries", self.max_retries)
        settings.setValue("retry_delay", self.retry_delay)
        settings.setValue("retry_backoff_factor", self.retry_backoff_factor)
        settings.setValue("retry_max_delay", self.retry_max_delay)
        settings.setValue("retry_exponential_backoff", self.retry_exponential_backoff)
        settings.setValue("auto_resume_startup", self.auto_resume_startup)
        settings.setValue("notify_on_completion", self.notify_on_completion)
        settings.setValue("backlog_locations", self.backlog_locations)
        settings.setValue("clear_backlog_after_load", self.clear_backlog_after_load)
        settings.setValue("backlog_poll_interval", self.backlog_poll_interval)
        settings.setValue("backlog_poll_enabled", self.backlog_poll_enabled)
        settings.setValue("metadata_fetch_timeout_days", self.metadata_fetch_timeout_days)
        settings.setValue("disk_space_check", self.disk_space_check)
        settings.setValue("disk_space_headroom_mb", self.disk_space_headroom_mb)
        settings.setValue("enable_system_tray", self.enable_system_tray)
        settings.setValue("minimize_to_tray", self.minimize_to_tray)
        settings.setValue("close_to_tray", self.close_to_tray)
        settings.setValue("start_minimized", self.start_minimized)
        settings.setValue("launch_at_login", self.launch_at_login)
        settings.setValue("clipboard_monitor_enabled", self.clipboard_monitor_enabled)
        settings.setValue("clipboard_monitor_max_urls", self.clipboard_monitor_max_urls)
        settings.setValue("clipboard_min_file_size_kb", self.clipboard_min_file_size_kb)
        settings.setValue("clipboard_ignored_extensions", self.clipboard_ignored_extensions)
        settings.setValue("capture_hotkey_enabled", self.capture_hotkey_enabled)
        settings.setValue("capture_hotkey_sequence", self.capture_hotkey_sequence)
        settings.endGroup()

    @classmethod
    def load(cls, settings: Optional[QSettings] = None) -> GeneralConfig:
        """Loads general preferences from QSettings."""
        if settings is None:
            settings = QSettings("MyIDM", "My-IDM")
        settings.beginGroup("General")
        default_save_path = settings.value("default_save_path", DEFAULT_DOWNLOADS_DIR) or DEFAULT_DOWNLOADS_DIR
        remember_last_save_path = settings.value("remember_last_save_path", False, type=bool)
        last_save_path = settings.value("last_save_path", "") or ""
        default_segments = settings.value("default_segments", 8, type=int)
        segment_start_delay_ms = clamp_segment_start_delay(
            settings.value("segment_start_delay_ms",
                           DEFAULT_SEGMENT_START_DELAY_MS, type=int)
        )
        max_concurrent_downloads = settings.value("max_concurrent_downloads", 3, type=int)
        max_retries = settings.value("max_retries", 5, type=int)
        retry_delay = settings.value("retry_delay", 2.0, type=float)
        retry_backoff_factor = settings.value("retry_backoff_factor", 2.0, type=float)
        retry_max_delay = settings.value("retry_max_delay", 60.0, type=float)
        retry_exponential_backoff = settings.value("retry_exponential_backoff", True, type=bool)
        auto_resume_startup = settings.value("auto_resume_startup", True, type=bool)
        notify_on_completion = settings.value("notify_on_completion", True, type=bool)
        raw_locs = settings.value("backlog_locations", [])
        if isinstance(raw_locs, list):
            backlog_locations = [str(x) for x in raw_locs if str(x).strip()]
        elif isinstance(raw_locs, str) and raw_locs.strip():
            backlog_locations = [x.strip() for x in raw_locs.split(";") if x.strip()]
        else:
            backlog_locations = []
        clear_backlog_after_load = settings.value("clear_backlog_after_load", True, type=bool)
        backlog_poll_interval = settings.value("backlog_poll_interval", 60, type=int)
        backlog_poll_enabled = settings.value("backlog_poll_enabled", True, type=bool)
        metadata_fetch_timeout_days = settings.value("metadata_fetch_timeout_days", 1, type=int)
        disk_space_check = settings.value("disk_space_check", True, type=bool)
        disk_space_headroom_mb = max(0, settings.value(
            "disk_space_headroom_mb", 256, type=int))
        enable_system_tray = settings.value("enable_system_tray", True, type=bool)
        minimize_to_tray = settings.value("minimize_to_tray", True, type=bool)
        close_to_tray = settings.value("close_to_tray", True, type=bool)
        start_minimized = settings.value("start_minimized", False, type=bool)
        launch_at_login = settings.value("launch_at_login", False, type=bool)
        clipboard_monitor_enabled = settings.value(
            "clipboard_monitor_enabled", False, type=bool
        )
        clipboard_monitor_max_urls = max(
            1, settings.value("clipboard_monitor_max_urls", 20, type=int)
        )
        clipboard_min_file_size_kb = max(
            0, settings.value("clipboard_min_file_size_kb", DEFAULT_CLIPBOARD_MIN_FILE_SIZE_KB, type=int)
        )
        raw_ignored_exts = settings.value(
            "clipboard_ignored_extensions", DEFAULT_CLIPBOARD_IGNORED_EXTENSIONS
        )
        clipboard_ignored_extensions = normalize_extension_list(raw_ignored_exts)
        capture_hotkey_enabled = settings.value(
            "capture_hotkey_enabled", False, type=bool
        )
        capture_hotkey_sequence = settings.value(
            "capture_hotkey_sequence", "Ctrl+Alt+D", type=str
        )
        settings.endGroup()

        return cls(
            default_save_path=str(default_save_path),
            remember_last_save_path=bool(remember_last_save_path),
            last_save_path=str(last_save_path),
            default_segments=int(default_segments),
            segment_start_delay_ms=int(segment_start_delay_ms),
            max_concurrent_downloads=int(max_concurrent_downloads),
            max_retries=int(max_retries),
            retry_delay=float(retry_delay),
            retry_backoff_factor=float(retry_backoff_factor),
            retry_max_delay=float(retry_max_delay),
            retry_exponential_backoff=bool(retry_exponential_backoff),
            auto_resume_startup=bool(auto_resume_startup),
            notify_on_completion=bool(notify_on_completion),
            backlog_locations=backlog_locations,
            clear_backlog_after_load=bool(clear_backlog_after_load),
            backlog_poll_interval=int(backlog_poll_interval),
            backlog_poll_enabled=bool(backlog_poll_enabled),
            metadata_fetch_timeout_days=int(metadata_fetch_timeout_days),
            disk_space_check=bool(disk_space_check),
            disk_space_headroom_mb=int(disk_space_headroom_mb),
            enable_system_tray=bool(enable_system_tray),
            minimize_to_tray=bool(minimize_to_tray),
            close_to_tray=bool(close_to_tray),
            start_minimized=bool(start_minimized),
            launch_at_login=bool(launch_at_login),
            clipboard_monitor_enabled=bool(clipboard_monitor_enabled),
            clipboard_monitor_max_urls=int(clipboard_monitor_max_urls),
            clipboard_min_file_size_kb=int(clipboard_min_file_size_kb),
            clipboard_ignored_extensions=list(clipboard_ignored_extensions),
            capture_hotkey_enabled=bool(capture_hotkey_enabled),
            capture_hotkey_sequence=str(capture_hotkey_sequence or "Ctrl+Alt+D"),
        )


@dataclass
class TorrentConfig:
    """Stores BitTorrent engine preferences, seeding behavior, and bandwidth limits."""

    seeding_after_complete: bool = True
    max_seeding_speed: int = 200  # in KB/s (0 = unlimited, default 200 KB/s)
    download_to_seeding_ratio: float = 10.0  # ratio of download speed to seeding speed (e.g. 10.0 = 10:1 ratio, seeding is 10% of download speed)
    metadata_fetch_timeout_days: int = 1  # in days (0 = disabled)
    seeding_time_limit_minutes: int = 240  # max seeding duration in minutes (0 = unlimited, default 4 hours / 240 min)
    seeding_ratio_limit: float = 0.0  # max share ratio (total_upload / downloaded) (0.0 = unlimited)
    resume_seeding_on_startup: bool = True  # whether to resume seeding torrents on application startup
    # -- .torrent ingress from the OS (drag-and-drop, file association, watched folder) --
    # Default False for all three: they each act on the user's machine beyond the app's own
    # window, so nothing is opted into until it is asked for. See docs/architecture/torrent.md.
    associate_torrent_files: bool = False  # register My-IDM as a .torrent handler with the OS
    watch_torrent_folder: bool = False  # watch a folder and add .torrent files that appear there
    torrent_watch_folder: str = ""  # "" means "the effective default download folder"
    clean_watched_torrent_files: bool = False  # move .torrent files to trash after adding from watched folder
    torrent_watch_max_age_days: int = 3  # max age in days for .torrent files in watched folder (0 = unlimited)

    def get_effective_seeding_speed_limit(self, download_limit_bytes: int = 0) -> int:
        """Calculate effective upload/seeding speed limit in bytes/sec.

        Uses max_seeding_speed (converted from KB/s to B/s) if > 0.
        If download_to_seeding_ratio > 0 and download_limit_bytes > 0:
            derives limit as download_limit_bytes / download_to_seeding_ratio.
        Returns the lowest non-zero limit in bytes/sec, or 0 for unlimited.
        """
        limits = []
        if self.max_seeding_speed > 0:
            limits.append(int(self.max_seeding_speed * 1024))
        if self.download_to_seeding_ratio > 0 and download_limit_bytes > 0:
            derived = int(download_limit_bytes / self.download_to_seeding_ratio)
            if derived > 0:
                limits.append(derived)
        if limits:
            return min(limits)
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "seeding_after_complete": self.seeding_after_complete,
            "max_seeding_speed": self.max_seeding_speed,
            "download_to_seeding_ratio": self.download_to_seeding_ratio,
            "metadata_fetch_timeout_days": self.metadata_fetch_timeout_days,
            "seeding_time_limit_minutes": self.seeding_time_limit_minutes,
            "seeding_ratio_limit": self.seeding_ratio_limit,
            "resume_seeding_on_startup": self.resume_seeding_on_startup,
            "associate_torrent_files": self.associate_torrent_files,
            "watch_torrent_folder": self.watch_torrent_folder,
            "torrent_watch_folder": self.torrent_watch_folder,
            "clean_watched_torrent_files": self.clean_watched_torrent_files,
            "torrent_watch_max_age_days": self.torrent_watch_max_age_days,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TorrentConfig:
        return cls(
            seeding_after_complete=bool(data.get("seeding_after_complete", True)),
            max_seeding_speed=int(data.get("max_seeding_speed", 200)),
            download_to_seeding_ratio=float(data.get("download_to_seeding_ratio", 10.0)),
            metadata_fetch_timeout_days=int(data.get("metadata_fetch_timeout_days", 1)),
            seeding_time_limit_minutes=int(data.get("seeding_time_limit_minutes", 240)),
            seeding_ratio_limit=float(data.get("seeding_ratio_limit", 0.0)),
            resume_seeding_on_startup=bool(data.get("resume_seeding_on_startup", True)),
            associate_torrent_files=bool(data.get("associate_torrent_files", False)),
            watch_torrent_folder=bool(data.get("watch_torrent_folder", False)),
            torrent_watch_folder=str(data.get("torrent_watch_folder", "")),
            clean_watched_torrent_files=bool(data.get("clean_watched_torrent_files", False)),
            torrent_watch_max_age_days=int(data.get("torrent_watch_max_age_days", 3)),
        )

    def save(self, settings: Optional[QSettings] = None):
        """Persists Torrent preferences into QSettings."""
        if settings is None:
            settings = QSettings("MyIDM", "My-IDM")
        settings.beginGroup("Torrent")
        settings.setValue("seeding_after_complete", self.seeding_after_complete)
        settings.setValue("max_seeding_speed", self.max_seeding_speed)
        settings.setValue("download_to_seeding_ratio", self.download_to_seeding_ratio)
        settings.setValue("metadata_fetch_timeout_days", self.metadata_fetch_timeout_days)
        settings.setValue("seeding_time_limit_minutes", self.seeding_time_limit_minutes)
        settings.setValue("seeding_ratio_limit", self.seeding_ratio_limit)
        settings.setValue("resume_seeding_on_startup", self.resume_seeding_on_startup)
        settings.setValue("associate_torrent_files", self.associate_torrent_files)
        settings.setValue("watch_torrent_folder", self.watch_torrent_folder)
        settings.setValue("torrent_watch_folder", self.torrent_watch_folder)
        settings.setValue("clean_watched_torrent_files", self.clean_watched_torrent_files)
        settings.setValue("torrent_watch_max_age_days", self.torrent_watch_max_age_days)
        settings.endGroup()

    @classmethod
    def load(cls, settings: Optional[QSettings] = None) -> TorrentConfig:
        """Loads Torrent preferences from QSettings."""
        if settings is None:
            settings = QSettings("MyIDM", "My-IDM")
        settings.beginGroup("Torrent")
        seeding_after_complete = settings.value("seeding_after_complete", True, type=bool)
        max_seeding_speed = settings.value("max_seeding_speed", 200, type=int)
        download_to_seeding_ratio = settings.value("download_to_seeding_ratio", 10.0, type=float)
        # Fall back to General/metadata_fetch_timeout_days if not set in Torrent
        metadata_fetch_timeout_days = settings.value("metadata_fetch_timeout_days", None)
        seeding_time_limit_minutes = settings.value("seeding_time_limit_minutes", 240, type=int)
        seeding_ratio_limit = settings.value("seeding_ratio_limit", 0.0, type=float)
        resume_seeding_on_startup = settings.value("resume_seeding_on_startup", True, type=bool)
        associate_torrent_files = settings.value("associate_torrent_files", False, type=bool)
        watch_torrent_folder = settings.value("watch_torrent_folder", False, type=bool)
        torrent_watch_folder = settings.value("torrent_watch_folder", "", type=str) or ""
        clean_watched_torrent_files = settings.value("clean_watched_torrent_files", False, type=bool)
        torrent_watch_max_age_days = settings.value("torrent_watch_max_age_days", 3, type=int)
        settings.endGroup()

        if metadata_fetch_timeout_days is None:
            settings.beginGroup("General")
            metadata_fetch_timeout_days = settings.value("metadata_fetch_timeout_days", 1, type=int)
            settings.endGroup()
        else:
            metadata_fetch_timeout_days = int(metadata_fetch_timeout_days)

        return cls(
            seeding_after_complete=bool(seeding_after_complete),
            max_seeding_speed=int(max_seeding_speed),
            download_to_seeding_ratio=float(download_to_seeding_ratio),
            metadata_fetch_timeout_days=int(metadata_fetch_timeout_days),
            seeding_time_limit_minutes=int(seeding_time_limit_minutes),
            seeding_ratio_limit=float(seeding_ratio_limit),
            resume_seeding_on_startup=bool(resume_seeding_on_startup),
            associate_torrent_files=bool(associate_torrent_files),
            watch_torrent_folder=bool(watch_torrent_folder),
            torrent_watch_folder=str(torrent_watch_folder),
            clean_watched_torrent_files=bool(clean_watched_torrent_files),
            torrent_watch_max_age_days=int(torrent_watch_max_age_days),
        )


def is_tor_reachable(host: str = "127.0.0.1", port: int = 9050, timeout: float = 1.0) -> bool:
    """Check if Tor SOCKS5 proxy is listening on host and port."""
    import socket
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.close()
        return True
    except (OSError, ValueError):
        return False


@dataclass
class TorConfig:
    """Stores Tor network routing and proxy configuration."""

    enabled: bool = False
    auto_start_at_startup: bool = False
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 9050
    route_http: bool = True
    route_torrent: bool = True
    tor_executable_path: str = ""

    @property
    def tor_socks_url(self) -> str:
        return f"socks5://{self.proxy_host}:{self.proxy_port}"

    @property
    def socks5_url(self) -> str:
        return self.tor_socks_url

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "auto_start_at_startup": self.auto_start_at_startup,
            "proxy_host": self.proxy_host,
            "proxy_port": self.proxy_port,
            "route_http": self.route_http,
            "route_torrent": self.route_torrent,
            "tor_executable_path": self.tor_executable_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TorConfig:
        return cls(
            enabled=bool(data.get("enabled", False)),
            auto_start_at_startup=bool(data.get("auto_start_at_startup", False)),
            proxy_host=str(data.get("proxy_host", "127.0.0.1")),
            proxy_port=int(data.get("proxy_port", 9050)),
            route_http=bool(data.get("route_http", True)),
            route_torrent=bool(data.get("route_torrent", True)),
            tor_executable_path=str(data.get("tor_executable_path", "")),
        )

    def save(self, settings: Optional[QSettings] = None):
        """Persists Tor preferences into QSettings."""
        if settings is None:
            settings = QSettings("MyIDM", "My-IDM")
        settings.beginGroup("Tor")
        settings.setValue("enabled", self.enabled)
        settings.setValue("auto_start_at_startup", self.auto_start_at_startup)
        settings.setValue("proxy_host", self.proxy_host)
        settings.setValue("proxy_port", self.proxy_port)
        settings.setValue("route_http", self.route_http)
        settings.setValue("route_torrent", self.route_torrent)
        settings.setValue("tor_executable_path", self.tor_executable_path)
        settings.endGroup()

    @classmethod
    def load(cls, settings: Optional[QSettings] = None) -> TorConfig:
        """Loads Tor preferences from QSettings."""
        if settings is None:
            settings = QSettings("MyIDM", "My-IDM")
        settings.beginGroup("Tor")
        enabled = settings.value("enabled", False, type=bool)
        auto_start = settings.value("auto_start_at_startup", False, type=bool)
        host = settings.value("proxy_host", "127.0.0.1") or "127.0.0.1"
        port = settings.value("proxy_port", 9050, type=int)
        route_http = settings.value("route_http", True, type=bool)
        route_torrent = settings.value("route_torrent", True, type=bool)
        tor_path = settings.value("tor_executable_path", "") or ""
        settings.endGroup()

        # Tor must only be enabled on application start if auto_start_at_startup is True
        effective_enabled = bool(enabled) if bool(auto_start) else False

        return cls(
            enabled=effective_enabled,
            auto_start_at_startup=bool(auto_start),
            proxy_host=str(host),
            proxy_port=int(port),
            route_http=bool(route_http),
            route_torrent=bool(route_torrent),
            tor_executable_path=str(tor_path),
        )


@dataclass
class ExternalToolsConfig:
    """Stores configuration for external scrapers and tools (e.g. AnimePahe, yt-dlp)."""

    animepahe_repo_path: str = ""
    animepahe_launch_on_startup: bool = False
    animepahe_periodic_run: bool = False
    animepahe_interval_hours: int = 6
    animepahe_last_url: str = ""
    animepahe_last_episodes: str = ""
    animepahe_last_quality: str = "Auto"
    animepahe_last_lang: str = "Auto"
    # YouTube / yt-dlp settings
    ytdlp_enabled: bool = True
    ytdlp_path: str = ""                    # yt-dlp binary (empty = use pip-installed module)
    ytdlp_ffmpeg_path: str = ""             # ffmpeg binary (empty = system PATH)
    ytdlp_default_format: str = DEFAULT_YTDLP_FORMAT
    ytdlp_prefer_mode_a: bool = True       # Prefer URL extraction over native download
    ytdlp_embed_thumbnail: bool = True      # Embed thumbnail in downloaded file
    ytdlp_embed_subtitles: bool = False     # Download & embed subtitles
    ytdlp_subtitle_langs: str = "en"        # Comma-separated subtitle language codes
    ytdlp_cookies_browser: str = ""         # Browser to extract cookies from
    ytdlp_extra_args: str = ""              # Additional args passed to yt-dlp
    ytdlp_auto_detect_urls: bool = True     # Auto-detect YouTube URLs in Add Download dialog
    ytdlp_playlist_limit: int = 10          # Max playlist/channel entries listed per analysis
    ytdlp_last_save_path: str = ""          # Last used save directory for YouTube downloads
    ytdlp_last_format: str = ""             # Last selected format string

    def to_dict(self) -> dict[str, Any]:
        return {
            "animepahe_repo_path": self.animepahe_repo_path,
            "animepahe_launch_on_startup": self.animepahe_launch_on_startup,
            "animepahe_periodic_run": self.animepahe_periodic_run,
            "animepahe_interval_hours": self.animepahe_interval_hours,
            "animepahe_last_url": self.animepahe_last_url,
            "animepahe_last_episodes": self.animepahe_last_episodes,
            "animepahe_last_quality": self.animepahe_last_quality,
            "animepahe_last_lang": self.animepahe_last_lang,
            "ytdlp_enabled": self.ytdlp_enabled,
            "ytdlp_path": self.ytdlp_path,
            "ytdlp_ffmpeg_path": self.ytdlp_ffmpeg_path,
            "ytdlp_default_format": self.ytdlp_default_format,
            "ytdlp_prefer_mode_a": self.ytdlp_prefer_mode_a,
            "ytdlp_embed_thumbnail": self.ytdlp_embed_thumbnail,
            "ytdlp_embed_subtitles": self.ytdlp_embed_subtitles,
            "ytdlp_subtitle_langs": self.ytdlp_subtitle_langs,
            "ytdlp_cookies_browser": self.ytdlp_cookies_browser,
            "ytdlp_extra_args": self.ytdlp_extra_args,
            "ytdlp_auto_detect_urls": self.ytdlp_auto_detect_urls,
            "ytdlp_playlist_limit": self.ytdlp_playlist_limit,
            "ytdlp_last_save_path": self.ytdlp_last_save_path,
            "ytdlp_last_format": self.ytdlp_last_format,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExternalToolsConfig:
        return cls(
            animepahe_repo_path=str(data.get("animepahe_repo_path", "")),
            animepahe_launch_on_startup=bool(data.get("animepahe_launch_on_startup", False)),
            animepahe_periodic_run=bool(data.get("animepahe_periodic_run", False)),
            animepahe_interval_hours=max(1, int(data.get("animepahe_interval_hours", 6) or 6)),
            animepahe_last_url=str(data.get("animepahe_last_url", "")),
            animepahe_last_episodes=str(data.get("animepahe_last_episodes", "")),
            animepahe_last_quality=str(data.get("animepahe_last_quality", "Auto")),
            animepahe_last_lang=str(data.get("animepahe_last_lang", "Auto")),
            ytdlp_enabled=bool(data.get("ytdlp_enabled", True)),
            ytdlp_path=str(data.get("ytdlp_path", "")),
            ytdlp_ffmpeg_path=str(data.get("ytdlp_ffmpeg_path", "")),
            ytdlp_default_format=str(data.get("ytdlp_default_format", DEFAULT_YTDLP_FORMAT) or DEFAULT_YTDLP_FORMAT),
            ytdlp_prefer_mode_a=bool(data.get("ytdlp_prefer_mode_a", True)),
            ytdlp_embed_thumbnail=bool(data.get("ytdlp_embed_thumbnail", True)),
            ytdlp_embed_subtitles=bool(data.get("ytdlp_embed_subtitles", False)),
            ytdlp_subtitle_langs=str(data.get("ytdlp_subtitle_langs", "en") or "en"),
            ytdlp_cookies_browser=str(data.get("ytdlp_cookies_browser", "")),
            ytdlp_extra_args=str(data.get("ytdlp_extra_args", "")),
            ytdlp_auto_detect_urls=bool(data.get("ytdlp_auto_detect_urls", True)),
            ytdlp_playlist_limit=clamp_ytdlp_playlist_limit(data.get("ytdlp_playlist_limit")),
            ytdlp_last_save_path=str(data.get("ytdlp_last_save_path", "")),
            ytdlp_last_format=str(data.get("ytdlp_last_format", "")),
        )

    def save(self, settings: Optional[QSettings] = None):
        """Persists external tools preferences into QSettings."""
        if settings is None:
            settings = QSettings("MyIDM", "My-IDM")
        settings.beginGroup("ExternalTools")
        settings.setValue("animepahe_repo_path", self.animepahe_repo_path)
        settings.setValue("animepahe_launch_on_startup", self.animepahe_launch_on_startup)
        settings.setValue("animepahe_periodic_run", self.animepahe_periodic_run)
        settings.setValue("animepahe_interval_hours", self.animepahe_interval_hours)
        # Don't persist last URL and episode range - only for current session
        # settings.setValue("animepahe_last_url", self.animepahe_last_url)
        # settings.setValue("animepahe_last_episodes", self.animepahe_last_episodes)
        settings.setValue("animepahe_last_quality", self.animepahe_last_quality)
        settings.setValue("animepahe_last_lang", self.animepahe_last_lang)
        settings.setValue("ytdlp_enabled", self.ytdlp_enabled)
        settings.setValue("ytdlp_path", self.ytdlp_path)
        settings.setValue("ytdlp_ffmpeg_path", self.ytdlp_ffmpeg_path)
        settings.setValue("ytdlp_default_format", self.ytdlp_default_format)
        settings.setValue("ytdlp_prefer_mode_a", self.ytdlp_prefer_mode_a)
        settings.setValue("ytdlp_embed_thumbnail", self.ytdlp_embed_thumbnail)
        settings.setValue("ytdlp_embed_subtitles", self.ytdlp_embed_subtitles)
        settings.setValue("ytdlp_subtitle_langs", self.ytdlp_subtitle_langs)
        settings.setValue("ytdlp_cookies_browser", self.ytdlp_cookies_browser)
        settings.setValue("ytdlp_extra_args", self.ytdlp_extra_args)
        settings.setValue("ytdlp_auto_detect_urls", self.ytdlp_auto_detect_urls)
        settings.setValue("ytdlp_playlist_limit", self.ytdlp_playlist_limit)
        settings.setValue("ytdlp_last_save_path", self.ytdlp_last_save_path)
        settings.setValue("ytdlp_last_format", self.ytdlp_last_format)
        settings.endGroup()

    @classmethod
    def load(cls, settings: Optional[QSettings] = None) -> ExternalToolsConfig:
        """Loads external tools preferences from QSettings."""
        if settings is None:
            settings = QSettings("MyIDM", "My-IDM")
        settings.beginGroup("ExternalTools")
        animepahe_repo_path = settings.value("animepahe_repo_path", "", type=str)
        animepahe_launch_on_startup = settings.value("animepahe_launch_on_startup", False, type=bool)
        animepahe_periodic_run = settings.value("animepahe_periodic_run", False, type=bool)
        animepahe_interval_hours = settings.value("animepahe_interval_hours", 6, type=int)
        # Don't persist last URL and episode range - only for current session
        # animepahe_last_url = settings.value("animepahe_last_url", "", type=str)
        # animepahe_last_episodes = settings.value("animepahe_last_episodes", "", type=str)
        animepahe_last_url = ""
        animepahe_last_episodes = ""
        animepahe_last_quality = settings.value("animepahe_last_quality", "Auto", type=str)
        animepahe_last_lang = settings.value("animepahe_last_lang", "Auto", type=str)
        ytdlp_enabled = settings.value("ytdlp_enabled", True, type=bool)
        ytdlp_path = settings.value("ytdlp_path", "", type=str)
        ytdlp_ffmpeg_path = settings.value("ytdlp_ffmpeg_path", "", type=str)
        ytdlp_default_format = settings.value("ytdlp_default_format", DEFAULT_YTDLP_FORMAT, type=str)
        ytdlp_prefer_mode_a = settings.value("ytdlp_prefer_mode_a", True, type=bool)
        ytdlp_embed_thumbnail = settings.value("ytdlp_embed_thumbnail", True, type=bool)
        ytdlp_embed_subtitles = settings.value("ytdlp_embed_subtitles", False, type=bool)
        ytdlp_subtitle_langs = settings.value("ytdlp_subtitle_langs", "en", type=str)
        ytdlp_cookies_browser = settings.value("ytdlp_cookies_browser", "", type=str)
        ytdlp_extra_args = settings.value("ytdlp_extra_args", "", type=str)
        ytdlp_auto_detect_urls = settings.value("ytdlp_auto_detect_urls", True, type=bool)
        ytdlp_playlist_limit = settings.value("ytdlp_playlist_limit", 10, type=int)
        ytdlp_last_save_path = settings.value("ytdlp_last_save_path", "", type=str)
        ytdlp_last_format = settings.value("ytdlp_last_format", "", type=str)
        settings.endGroup()

        # Auto-detect default if not explicitly configured
        if not animepahe_repo_path:
            candidates = [
                Path(r"D:\Projects\animepahe-downloader"),
                Path.home() / "Projects" / "animepahe-downloader",
            ]
            for c in candidates:
                if c.is_dir() and (c / "animepahe_download.py").is_file():
                    animepahe_repo_path = normalize_path(str(c))
                    break

        # Auto-detect yt-dlp binary if not explicitly configured
        if not ytdlp_path:
            import shutil
            found = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
            if found:
                ytdlp_path = found

        # Auto-detect ffmpeg if not explicitly configured
        if not ytdlp_ffmpeg_path:
            import shutil
            found = shutil.which("ffmpeg")
            if found:
                ytdlp_ffmpeg_path = found

        return cls(
            animepahe_repo_path=str(animepahe_repo_path or ""),
            animepahe_launch_on_startup=bool(animepahe_launch_on_startup),
            animepahe_periodic_run=bool(animepahe_periodic_run),
            animepahe_interval_hours=max(1, int(animepahe_interval_hours or 6)),
            animepahe_last_url=str(animepahe_last_url or ""),
            animepahe_last_episodes=str(animepahe_last_episodes or ""),
            animepahe_last_quality=str(animepahe_last_quality or "Auto"),
            animepahe_last_lang=str(animepahe_last_lang or "Auto"),
            ytdlp_enabled=bool(ytdlp_enabled),
            ytdlp_path=str(ytdlp_path or ""),
            ytdlp_ffmpeg_path=str(ytdlp_ffmpeg_path or ""),
            ytdlp_default_format=str(ytdlp_default_format or DEFAULT_YTDLP_FORMAT),
            ytdlp_prefer_mode_a=bool(ytdlp_prefer_mode_a),
            ytdlp_embed_thumbnail=bool(ytdlp_embed_thumbnail),
            ytdlp_embed_subtitles=bool(ytdlp_embed_subtitles),
            ytdlp_subtitle_langs=str(ytdlp_subtitle_langs or "en"),
            ytdlp_cookies_browser=str(ytdlp_cookies_browser or ""),
            ytdlp_extra_args=str(ytdlp_extra_args or ""),
            ytdlp_auto_detect_urls=bool(ytdlp_auto_detect_urls),
            ytdlp_playlist_limit=clamp_ytdlp_playlist_limit(ytdlp_playlist_limit),
            ytdlp_last_save_path=str(ytdlp_last_save_path or ""),
            ytdlp_last_format=str(ytdlp_last_format or ""),
        )

    def get_effective_repo_path(self) -> str:
        """Returns the configured or auto-detected path to animepahe-downloader directory."""
        if self.animepahe_repo_path:
            return normalize_path(self.animepahe_repo_path) if os.path.isdir(self.animepahe_repo_path) else ""
        candidates = [
            Path(r"D:\Projects\animepahe-downloader"),
            Path.home() / "Projects" / "animepahe-downloader",
        ]
        for c in candidates:
            if c.is_dir() and (c / "animepahe_download.py").is_file():
                return normalize_path(str(c))
        return ""

    def get_console_log_path(self) -> Path:
        """Returns path to console stdout/stderr log file."""
        repo = self.get_effective_repo_path()
        if repo and os.path.isdir(repo):
            return Path(repo) / "console_log.txt"
        log_dir = APP_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir / "animepahe_console.log"

    def get_debug_log_path(self) -> Path:
        """Returns path to debug_log.txt file."""
        repo = self.get_effective_repo_path()
        if repo and os.path.isdir(repo):
            return Path(repo) / "debug_log.txt"
        log_dir = APP_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir / "animepahe_debug.log"

    def get_effective_ytdlp_path(self) -> str:
        """Returns the configured or auto-detected yt-dlp executable path."""
        if self.ytdlp_path and os.path.isfile(self.ytdlp_path):
            return normalize_path(self.ytdlp_path)
        import shutil
        found = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
        return found or ""

    def get_effective_ffmpeg_path(self) -> str:
        """Returns the configured or auto-detected ffmpeg executable path."""
        if self.ytdlp_ffmpeg_path and os.path.isfile(self.ytdlp_ffmpeg_path):
            return normalize_path(self.ytdlp_ffmpeg_path)
        import shutil
        return shutil.which("ffmpeg") or ""

    def get_effective_ytdlp_subtitle_langs(self) -> list[str]:
        """Returns subtitle language codes as a clean list."""
        raw = self.ytdlp_subtitle_langs or ""
        langs = [x.strip() for x in raw.split(",") if x.strip()]
        return langs or ["en"]

    def get_effective_ytdlp_extra_args(self) -> list[str]:
        """Parses the free-form extra-args field into an argument list."""
        import shlex
        raw = (self.ytdlp_extra_args or "").strip()
        if not raw:
            return []
        try:
            return shlex.split(raw, posix=not os.name == "nt")
        except ValueError:
            return raw.split()


@dataclass
class BrowserIntegrationConfig:
    """Stores configuration for browser extension integration."""

    enabled: bool = True
    port: int = 19582
    host: str = "127.0.0.1"
    intercept_all: bool = True
    intercept_torrent_files: bool = True
    intercept_magnet_links: bool = True
    min_file_size_kb: int = 0  # Minimum file size in KB to intercept (0 = no minimum)
    # What to do when `min_file_size_kb` is set but the download's size cannot be determined -
    # which is the common case, because Chrome reports `totalBytes: 0` at
    # `onDeterminingFilename` time and a chunked or dynamically generated response has no
    # Content-Length to probe for.
    #
    # True  (default): do not capture. A minimum the user configured is a statement about what
    #   they want to see in My-IDM, and capturing something that may be a 4 KB stylesheet only to
    #   drop it in the engine after the probe is worse than not capturing it: the browser download
    #   is cancelled and a row appears and then disappears.
    # False: capture it and defer the check to the engine's own probe
    #   (`HTTPEngine._enforce_browser_min_size`), which keeps the threshold enforced for the
    #   unsizeable majority at the cost of the capture-then-refuse churn.
    skip_unknown_size_downloads: bool = True
    bypassed_extensions: list[str] = field(default_factory=lambda: [".crx"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "port": self.port,
            "host": self.host,
            "intercept_all": self.intercept_all,
            "intercept_torrent_files": self.intercept_torrent_files,
            "intercept_magnet_links": self.intercept_magnet_links,
            "min_file_size_kb": self.min_file_size_kb,
            "skip_unknown_size_downloads": self.skip_unknown_size_downloads,
            "bypassed_extensions": list(self.bypassed_extensions),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BrowserIntegrationConfig:
        return cls(
            enabled=bool(data.get("enabled", True)),
            port=int(data.get("port", 19582)),
            host=str(data.get("host", "127.0.0.1")),
            intercept_all=bool(data.get("intercept_all", True)),
            intercept_torrent_files=bool(data.get("intercept_torrent_files", True)),
            intercept_magnet_links=bool(data.get("intercept_magnet_links", True)),
            min_file_size_kb=int(data.get("min_file_size_kb", 0)),
            skip_unknown_size_downloads=bool(data.get("skip_unknown_size_downloads", True)),
            bypassed_extensions=list(data.get("bypassed_extensions", [".crx"])),
        )

    def save(self, settings: Optional[QSettings] = None):
        """Persists browser integration preferences into QSettings."""
        if settings is None:
            settings = QSettings("MyIDM", "My-IDM")
        settings.beginGroup("BrowserIntegration")
        settings.setValue("enabled", self.enabled)
        settings.setValue("port", self.port)
        settings.setValue("host", self.host)
        settings.setValue("intercept_all", self.intercept_all)
        settings.setValue("intercept_torrent_files", self.intercept_torrent_files)
        settings.setValue("intercept_magnet_links", self.intercept_magnet_links)
        settings.setValue("min_file_size_kb", self.min_file_size_kb)
        settings.setValue("skip_unknown_size_downloads", self.skip_unknown_size_downloads)
        settings.setValue("bypassed_extensions", ",".join(self.bypassed_extensions))
        settings.endGroup()

    @classmethod
    def load(cls, settings: Optional[QSettings] = None) -> BrowserIntegrationConfig:
        """Loads browser integration preferences from QSettings."""
        if settings is None:
            settings = QSettings("MyIDM", "My-IDM")
        settings.beginGroup("BrowserIntegration")
        enabled = settings.value("enabled", True, type=bool)
        port = settings.value("port", 19582, type=int)
        host = settings.value("host", "127.0.0.1", type=str)
        intercept_all = settings.value("intercept_all", True, type=bool)
        intercept_torrent_files = settings.value("intercept_torrent_files", True, type=bool)
        intercept_magnet_links = settings.value("intercept_magnet_links", True, type=bool)
        min_file_size_kb = settings.value("min_file_size_kb", 0, type=int)
        skip_unknown_size_downloads = settings.value(
            "skip_unknown_size_downloads", True, type=bool
        )
        bypassed_raw = settings.value("bypassed_extensions", ".crx", type=str)
        settings.endGroup()

        bypassed = [ext.strip() for ext in bypassed_raw.split(",") if ext.strip()] if bypassed_raw else [".crx"]
        return cls(
            enabled=bool(enabled),
            port=int(port) if port > 0 else 19582,
            host=str(host or "127.0.0.1"),
            intercept_all=bool(intercept_all),
            intercept_torrent_files=bool(intercept_torrent_files),
            intercept_magnet_links=bool(intercept_magnet_links),
            min_file_size_kb=int(min_file_size_kb) if min_file_size_kb >= 0 else 0,
            skip_unknown_size_downloads=bool(skip_unknown_size_downloads),
            bypassed_extensions=bypassed,
        )


@dataclass
class BandwidthLimitConfig:
    """Stores configuration for daily/weekly/monthly bandwidth limits.
    
    Limits are stored in the database (bandwidth_limits table) rather than QSettings
    because they are per-queue and support multiple limit types. This config class
    handles the global defaults and persistence.
    """
    # Global bandwidth limits (0 = unlimited)
    global_download_limit: int = 0  # bytes per period
    global_upload_limit: int = 0    # bytes per period
    global_limit_type: str = "monthly"  # daily, weekly, monthly
    global_enabled: bool = False
    global_warning_percent: int = 80

    def to_dict(self) -> dict[str, Any]:
        return {
            "global_download_limit": self.global_download_limit,
            "global_upload_limit": self.global_upload_limit,
            "global_limit_type": self.global_limit_type,
            "global_enabled": self.global_enabled,
            "global_warning_percent": self.global_warning_percent,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BandwidthLimitConfig:
        return cls(
            global_download_limit=int(data.get("global_download_limit", 0)),
            global_upload_limit=int(data.get("global_upload_limit", 0)),
            global_limit_type=str(data.get("global_limit_type", "monthly")),
            global_enabled=bool(data.get("global_enabled", False)),
            global_warning_percent=int(data.get("global_warning_percent", 80)),
        )

    def save(self, settings: Optional[QSettings] = None):
        """Persists bandwidth limit preferences into QSettings."""
        if settings is None:
            settings = QSettings("MyIDM", "My-IDM")
        settings.beginGroup("BandwidthLimits")
        settings.setValue("global_download_limit", self.global_download_limit)
        settings.setValue("global_upload_limit", self.global_upload_limit)
        settings.setValue("global_limit_type", self.global_limit_type)
        settings.setValue("global_enabled", self.global_enabled)
        settings.setValue("global_warning_percent", self.global_warning_percent)
        settings.endGroup()

    @classmethod
    def load(cls, settings: Optional[QSettings] = None) -> BandwidthLimitConfig:
        """Loads bandwidth limit preferences from QSettings."""
        if settings is None:
            settings = QSettings("MyIDM", "My-IDM")
        settings.beginGroup("BandwidthLimits")
        global_download_limit = settings.value("global_download_limit", 0, type=int)
        global_upload_limit = settings.value("global_upload_limit", 0, type=int)
        global_limit_type = settings.value("global_limit_type", "monthly", type=str) or "monthly"
        global_enabled = settings.value("global_enabled", False, type=bool)
        global_warning_percent = settings.value("global_warning_percent", 80, type=int)
        settings.endGroup()

        return cls(
            global_download_limit=int(global_download_limit or 0),
            global_upload_limit=int(global_upload_limit or 0),
            global_limit_type=str(global_limit_type),
            global_enabled=bool(global_enabled),
            global_warning_percent=int(global_warning_percent or 80),
        )

