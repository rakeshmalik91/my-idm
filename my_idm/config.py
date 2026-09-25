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


@dataclass
class GeneralConfig:
    """Stores user preferences for downloads and application behavior."""

    default_save_path: str = DEFAULT_DOWNLOADS_DIR
    remember_last_save_path: bool = False
    last_save_path: str = ""
    default_segments: int = 8
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
        settings.endGroup()

        return cls(
            default_save_path=str(default_save_path),
            remember_last_save_path=bool(remember_last_save_path),
            last_save_path=str(last_save_path),
            default_segments=int(default_segments),
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
    """Stores configuration for external scrapers and tools (e.g. AnimePahe)."""

    animepahe_repo_path: str = ""
    animepahe_launch_on_startup: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "animepahe_repo_path": self.animepahe_repo_path,
            "animepahe_launch_on_startup": self.animepahe_launch_on_startup,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExternalToolsConfig:
        return cls(
            animepahe_repo_path=str(data.get("animepahe_repo_path", "")),
            animepahe_launch_on_startup=bool(data.get("animepahe_launch_on_startup", False)),
        )

    def save(self, settings: Optional[QSettings] = None):
        """Persists external tools preferences into QSettings."""
        if settings is None:
            settings = QSettings("MyIDM", "My-IDM")
        settings.beginGroup("ExternalTools")
        settings.setValue("animepahe_repo_path", self.animepahe_repo_path)
        settings.setValue("animepahe_launch_on_startup", self.animepahe_launch_on_startup)
        settings.endGroup()

    @classmethod
    def load(cls, settings: Optional[QSettings] = None) -> ExternalToolsConfig:
        """Loads external tools preferences from QSettings."""
        if settings is None:
            settings = QSettings("MyIDM", "My-IDM")
        settings.beginGroup("ExternalTools")
        animepahe_repo_path = settings.value("animepahe_repo_path", "", type=str)
        animepahe_launch_on_startup = settings.value("animepahe_launch_on_startup", False, type=bool)
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

        return cls(
            animepahe_repo_path=str(animepahe_repo_path or ""),
            animepahe_launch_on_startup=bool(animepahe_launch_on_startup),
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

