"""General configuration and preferences for My-IDM."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QSettings

DEFAULT_DOWNLOADS_DIR = str(Path.home() / "Downloads")


@dataclass
class GeneralConfig:
    """Stores user preferences for downloads and application behavior."""

    default_save_path: str = DEFAULT_DOWNLOADS_DIR
    remember_last_save_path: bool = False
    last_save_path: str = ""
    default_segments: int = 8
    max_concurrent_downloads: int = 3
    max_retries: int = 5
    auto_resume_startup: bool = True
    notify_on_completion: bool = True

    def get_effective_save_path(self) -> str:
        """Returns the directory to prefill for a new download."""
        if self.remember_last_save_path and self.last_save_path and os.path.isdir(self.last_save_path):
            return self.last_save_path
        if self.default_save_path and os.path.isdir(self.default_save_path):
            return self.default_save_path
        return DEFAULT_DOWNLOADS_DIR

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_save_path": self.default_save_path,
            "remember_last_save_path": self.remember_last_save_path,
            "last_save_path": self.last_save_path,
            "default_segments": self.default_segments,
            "max_concurrent_downloads": self.max_concurrent_downloads,
            "max_retries": self.max_retries,
            "auto_resume_startup": self.auto_resume_startup,
            "notify_on_completion": self.notify_on_completion,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GeneralConfig:
        return cls(
            default_save_path=str(data.get("default_save_path", DEFAULT_DOWNLOADS_DIR)),
            remember_last_save_path=bool(data.get("remember_last_save_path", False)),
            last_save_path=str(data.get("last_save_path", "")),
            default_segments=int(data.get("default_segments", 8)),
            max_concurrent_downloads=int(data.get("max_concurrent_downloads", 3)),
            max_retries=int(data.get("max_retries", 5)),
            auto_resume_startup=bool(data.get("auto_resume_startup", True)),
            notify_on_completion=bool(data.get("notify_on_completion", True)),
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
        settings.setValue("auto_resume_startup", self.auto_resume_startup)
        settings.setValue("notify_on_completion", self.notify_on_completion)
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
        auto_resume_startup = settings.value("auto_resume_startup", True, type=bool)
        notify_on_completion = settings.value("notify_on_completion", True, type=bool)
        settings.endGroup()

        return cls(
            default_save_path=str(default_save_path),
            remember_last_save_path=bool(remember_last_save_path),
            last_save_path=str(last_save_path),
            default_segments=int(default_segments),
            max_concurrent_downloads=int(max_concurrent_downloads),
            max_retries=int(max_retries),
            auto_resume_startup=bool(auto_resume_startup),
            notify_on_completion=bool(notify_on_completion),
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
