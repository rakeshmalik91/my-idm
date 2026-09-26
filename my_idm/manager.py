"""Central download manager orchestrating engines, database, and GUI signals."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union, Any
from urllib.parse import parse_qs, unquote, urlparse

from PySide6.QtCore import QObject, Signal, QTimer

from my_idm.database import Database, DownloadEntry, SegmentEntry, _now_iso
from my_idm.http_engine import HTTPEngine
from my_idm.config import (
    GeneralConfig,
    TorConfig,
    TorrentConfig,
    ExternalToolsConfig,
    BrowserIntegrationConfig,
    is_tor_reachable,
    DEFAULT_DOWNLOADS_DIR,
)
from my_idm.notifications import notify_browser_download_caught
from my_idm.browser_server import BrowserServer
from my_idm.external_tools import launch_animepahe_cli, launch_animepahe_gui
from my_idm.tor_service import TorServiceManager, find_tor_executable
from my_idm.network import NetworkConfig, is_interface_active
from my_idm.security import (
    SecurityConfig,
    check_url_safety,
    scan_file,
    quarantine_or_delete_file,
)
from my_idm.torrent_engine import TorrentEngine
from my_idm.utils import get_unique_filename, normalize_path, robust_move_download_files, send_to_trash, to_int, unlock_path

log = logging.getLogger(__name__)

DEFAULT_SAVE_PATH = DEFAULT_DOWNLOADS_DIR


class ParsedBacklogEntry(tuple):
    """Backwards-compatible 3-tuple (url, save_path, new_dir) with filename and headers attributes."""
    def __new__(cls, url: Optional[str], save_path: str, new_dir: Optional[str],
                filename: str = "", headers: Optional[dict[str, str]] = None):
        return super().__new__(cls, (url, save_path, new_dir))

    def __init__(self, url: Optional[str], save_path: str, new_dir: Optional[str],
                 filename: str = "", headers: Optional[dict[str, str]] = None):
        self.url = url
        self.save_path = save_path
        self.new_dir = new_dir
        self.filename = filename
        self.headers = headers or {}


def parse_backlog_entry(
    raw_line: str,
    active_save_path: str = "",
    last_comment: str = "",
) -> ParsedBacklogEntry:
    """Parse a single backlog file line.

    Returns a backwards-compatible 3-tuple (url, save_path, new_dir) with
    .filename and .headers attributes:
    - If line is empty or comment: (None, "", None).
    - If line sets directory directive (# dir: ..., dir=..., [path]): (None, "", directive_path).
    - If line contains a download URL/magnet: (url, save_path, None, filename, headers).
    """
    line = raw_line.strip()
    if not line:
        return ParsedBacklogEntry(None, "", None)

    # Check for comment directive: e.g. # dir: /path or # save_path: /path
    m_comment_dir = re.match(r'^#+\s*(?:dir|save_path|path)\s*[:=]\s*(.+)$', line, re.IGNORECASE)
    if m_comment_dir:
        p = m_comment_dir.group(1).strip().strip('"\'')
        p = os.path.expandvars(os.path.expanduser(p))
        return ParsedBacklogEntry(None, "", normalize_path(p))

    # General comments
    if line.startswith("#") or line.startswith("//"):
        return ParsedBacklogEntry(None, "", None)

    # Section directive: e.g. [D:\Downloads\Music]
    m_section = re.match(r'^\[(.+)\]$', line)
    if m_section:
        p = m_section.group(1).strip().strip('"\'')
        p = os.path.expandvars(os.path.expanduser(p))
        return ParsedBacklogEntry(None, "", normalize_path(p))

    # Directive without leading comment: e.g. dir = D:\Path or save_path = D:\Path
    m_dir = re.match(r'^(?:dir|save_path|path)\s*[:=]\s*(.+)$', line, re.IGNORECASE)
    if m_dir:
        p = m_dir.group(1).strip().strip('"\'')
        p = os.path.expandvars(os.path.expanduser(p))
        return ParsedBacklogEntry(None, "", normalize_path(p))

    url = ""
    save_path = ""
    filename = ""
    headers: dict[str, str] = {}

    # Extract filename or directives if embedded in line
    if "|" in line:
        parts = [p.strip() for p in line.split("|")]
        url = parts[0]
        for part in parts[1:]:
            if not part:
                continue
            # Check key=value format
            if "=" in part:
                k, _, v = part.partition("=")
                k_clean = k.strip().lower()
                v_clean = v.strip().strip('"\'')
                if k_clean in ("dir", "save_path", "path", "folder"):
                    save_path = v_clean
                elif k_clean in ("filename", "file", "out", "name"):
                    filename = v_clean
                elif k_clean in ("referer", "referrer"):
                    headers["Referer"] = v_clean
                elif k_clean.startswith("header"):
                    # e.g. header=Name: Value
                    if ":" in v_clean:
                        hn, _, hv = v_clean.partition(":")
                        headers[hn.strip()] = hv.strip()
            else:
                # Positional
                if not save_path and not filename:
                    # Check if this part looks like a directory or filename
                    if "." in os.path.basename(part) and not os.path.isdir(part):
                        filename = part
                    else:
                        save_path = part
                elif save_path and not filename:
                    filename = part
                elif not save_path and filename:
                    save_path = part
    elif " -> " in line:
        parts = [p.strip() for p in line.split(" -> ")]
        url = parts[0]
        if len(parts) >= 2:
            save_path = parts[1]
        if len(parts) >= 3:
            filename = parts[2]
    elif "\t" in line:
        parts = [p.strip() for p in line.split("\t") if p.strip()]
        url = parts[0]
        if len(parts) >= 2:
            save_path = parts[1]
        if len(parts) >= 3:
            filename = parts[2]
    elif ";" in line:
        parts = line.split(";", 1)
        if ("://" in parts[0] or parts[0].startswith("magnet:") or parts[0].endswith(".torrent")) and parts[1].strip():
            url = parts[0].strip()
            save_path = parts[1].strip()
        else:
            url = line
    else:
        # Check for aria2-style dir="path" or out="name" or referer="url"
        m_dir_opt = re.search(r'(?:dir|out_dir)\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|(\S+))', line, re.IGNORECASE)
        if m_dir_opt:
            save_path = m_dir_opt.group(1) or m_dir_opt.group(2) or m_dir_opt.group(3)
            line = (line[:m_dir_opt.start()] + " " + line[m_dir_opt.end():]).strip()

        m_out_opt = re.search(r'(?:out|filename)\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|(\S+))', line, re.IGNORECASE)
        if m_out_opt:
            filename = m_out_opt.group(1) or m_out_opt.group(2) or m_out_opt.group(3)
            line = (line[:m_out_opt.start()] + " " + line[m_out_opt.end():]).strip()

        m_ref_opt = re.search(r'referer\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|(\S+))', line, re.IGNORECASE)
        if m_ref_opt:
            headers["Referer"] = m_ref_opt.group(1) or m_ref_opt.group(2) or m_ref_opt.group(3)
            line = (line[:m_ref_opt.start()] + " " + line[m_ref_opt.end():]).strip()

        if " " in line:
            parts = line.split(None, 1)
            if ("://" in parts[0] or parts[0].startswith("magnet:") or parts[0].endswith(".torrent")) and len(parts) == 2:
                url = parts[0].strip()
                if not save_path:
                    save_path = parts[1].strip()
            else:
                url = line
        else:
            url = line

    if save_path:
        save_path = save_path.strip().strip('"\'')
        save_path = os.path.expandvars(os.path.expanduser(save_path))
        save_path = normalize_path(save_path)
    elif active_save_path:
        save_path = active_save_path

    if filename:
        filename = filename.strip().strip('"\'')

    # Fallback: Extract filename from last comment if comment contains '(filename.ext)' or '[filename.ext]'
    if not filename and last_comment:
        m_fn = re.search(r'[\(\[]([^\(\)\[\]]+\.[a-zA-Z0-9]{2,5})[\)\]]', last_comment)
        if m_fn:
            filename = m_fn.group(1).strip()

    # Fallback: Extract filename from URL query params (e.g. ?file=... or ?filename=...)
    if not filename and url and ("?" in url):
        try:
            parsed_u = urlparse(url)
            qs = parse_qs(parsed_u.query)
            for k in ("file", "filename", "name", "title"):
                val = qs.get(k)
                if val and val[0]:
                    cand = unquote(val[0])
                    if "." in cand:
                        filename = Path(cand).name
                        break
        except Exception:
            pass

    # Auto-referer for known video CDNs
    if url and ("owocdn.top" in url or "kwik." in url):
        if "Referer" not in headers:
            headers["Referer"] = "https://kwik.cx/"

    return ParsedBacklogEntry(url, save_path, None, filename=filename, headers=headers)



class DownloadManager(QObject):
    """Coordinates downloads between HTTP/Torrent engines, DB, and GUI."""

    # Signals for the GUI
    progress_updated = Signal(str, int, int, float, float, int, int, float)
    # download_id, downloaded, total, speed, eta, seeds, peers, upload_speed
    status_changed = Signal(str, str, str)
    # download_id, status, error_message
    filename_resolved = Signal(str, str)  # download_id, filename
    download_added = Signal(str)          # download_id
    download_removed = Signal(str)        # download_id
    download_moved = Signal(str)          # download_id
    download_renamed = Signal(str, str)   # download_id, new_filename
    general_config_changed = Signal(object)   # GeneralConfig
    torrent_config_changed = Signal(object)   # TorrentConfig
    network_config_changed = Signal(object)  # NetworkConfig
    security_config_changed = Signal(object)  # SecurityConfig
    tor_config_changed = Signal(object)       # TorConfig
    threat_detected = Signal(str, str)       # download_id, report
    queue_order_changed = Signal()
    tor_status_changed = Signal(str, str)     # status ("connecting"|"connected"|"disconnecting"|"disconnected"|"error"), message
    bandwidth_limits_changed = Signal(int, int)  # download_limit, upload_limit
    external_tools_config_changed = Signal(object)  # ExternalToolsConfig
    animepahe_status_changed = Signal(bool)  # is_running
    animepahe_queue_changed = Signal(int)    # queue_size
    browser_config_changed = Signal(object)  # BrowserIntegrationConfig

    def __init__(self, db: Database, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._db = db
        self._general_config = GeneralConfig.load()
        self._torrent_config = TorrentConfig.load()
        self._network_config = NetworkConfig.load()
        self._security_config = SecurityConfig.load()
        self._tor_config = TorConfig.load()
        self._external_tools_config = ExternalToolsConfig.load()
        self._browser_config = BrowserIntegrationConfig.load()
        self._browser_server = BrowserServer(self, self._browser_config)
        self._animepahe_process: Optional[subprocess.Popen] = None
        self._animepahe_queue: list[dict[str, Any]] = []
        self._animepahe_queue_lock = threading.Lock()
        self._browser_container_hwnd: Optional[int] = None
        # Enforce that Tor is only enabled on startup if auto_start_at_startup is True
        if not self._tor_config.auto_start_at_startup:
            self._tor_config.enabled = False
        self._tor_service = TorServiceManager(self._tor_config)
        self._stopped = False
        self._starting_downloads: set[str] = set()
        self._http = HTTPEngine(db)
        self._torrent = TorrentEngine(db)
        self._http.set_general_config_sync(self._general_config)
        self._http.set_network_config_sync(self._network_config)
        self._http.set_tor_config_sync(self._tor_config)
        self._torrent.set_general_config(self._general_config)
        self._torrent.apply_torrent_config(self._torrent_config)
        self._torrent.apply_network_config(self._network_config)
        self._torrent.apply_tor_config(self._tor_config)
        self._torrent.set_session_limits(self._network_config.download_limit, self._network_config.upload_limit)
        self._http.set_download_limit(self._network_config.download_limit)

        # asyncio event loop runs in a background thread
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

        # Torrent poll timer (runs in Qt main thread)
        self._torrent_timer = QTimer(self)
        self._torrent_timer.setInterval(1000)  # 1 second
        self._torrent_timer.timeout.connect(self._poll_torrents)

        # Retry timer — checks queued items periodically (1s for responsive backoff)
        self._retry_timer = QTimer(self)
        self._retry_timer.setInterval(1000)  # 1 second
        self._retry_timer.timeout.connect(self._process_retry_queue)

        # Backlog poll timer — periodically checks for backlog files
        self._backlog_timer = QTimer(self)
        self._backlog_timer.timeout.connect(self._on_backlog_timer_tick)
        self._apply_backlog_timer_config()

        # AnimePahe periodic scraper timer
        self._animepahe_timer = QTimer(self)
        self._animepahe_timer.timeout.connect(self._on_animepahe_timer_tick)
        self._apply_animepahe_timer_config()

        # Wire engine callbacks
        self._http.set_callbacks(
            self._on_http_progress,
            self._on_http_status,
            self._on_filename_resolved,
        )
        self._torrent.set_callbacks(
            self._on_torrent_progress,
            self._on_torrent_status,
            self._on_filename_resolved,
        )

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        """Start engines and background loop."""
        # Start asyncio loop in thread
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="idm-async"
        )
        self._thread.start()

        # Start HTTP engine
        future = asyncio.run_coroutine_threadsafe(
            self._http.start(), self._loop
        )
        future.result(timeout=10)

        # Start torrent engine
        self._torrent.start()
        self._torrent_timer.start()
        self._retry_timer.start()

        # Start periodic backlog polling if enabled
        if self._general_config.backlog_poll_enabled and self._general_config.backlog_poll_interval > 0:
            self._backlog_timer.start()

        # Check if Tor should be activated at startup
        if self._tor_config.auto_start_at_startup:
            self.toggle_tor(True)
        else:
            if self._tor_config.enabled:
                self._tor_config.enabled = False
                self._http.set_tor_config_sync(self._tor_config)
                self._torrent.apply_tor_config(self._tor_config)

        # Auto-resume queued and interrupted downloads on startup in priority order
        all_entries = self._db.get_all_downloads()
        all_entries.sort(key=lambda e: (e.queue_order if e.queue_order > 0 else 999999, e.added_at or ""))
        for entry in all_entries:
            if entry.status == "queued":
                log.info("Auto-starting queued download on startup: %s (order=%s)", entry.id, entry.queue_order)
                self.resume_download(entry.id)
            elif self._general_config.auto_resume_startup and entry.status in ("downloading", "checking", "fetching_metadata"):
                log.info("Auto-resuming interrupted download on startup: %s (order=%s)", entry.id, entry.queue_order)
                self.resume_download(entry.id)
            elif entry.status == "seeding" and getattr(self._torrent_config, "resume_seeding_on_startup", True):
                log.info("Auto-resuming seeding torrent on startup: %s (order=%s)", entry.id, entry.queue_order)
                self._torrent.add_torrent(entry)

        # Launch external tools (e.g. AnimePahe scraper) if configured
        if self._external_tools_config.animepahe_launch_on_startup:
            self.start_animepahe_scraper()

        # Start periodic AnimePahe scraper if enabled
        self._apply_animepahe_timer_config()

        # Start browser integration loopback server if enabled
        if self._browser_config.enabled:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._browser_server.start(), self._loop
                )
                future.result(timeout=5)
            except Exception as exc:
                log.warning("Failed to start browser integration server: %s", exc)

        log.info("DownloadManager started")

    def stop(self, status_cb=None):
        """Shut down everything cleanly."""
        if getattr(self, "_stopped", False):
            return
        self._stopped = True

        if status_cb:
            status_cb("Stopping background timers...", 15)
        self._torrent_timer.stop()
        self._retry_timer.stop()
        self._backlog_timer.stop()
        self._animepahe_timer.stop()

        # Stop browser integration server
        if getattr(self, "_browser_server", None) and self._browser_server.is_running and self._loop and self._loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._browser_server.stop(), self._loop
                )
                future.result(timeout=3)
            except Exception:
                pass

        # Stop HTTP engine
        if status_cb:
            status_cb("Stopping active HTTP downloads...", 35)
        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self._http.stop(), self._loop
            )
            try:
                future.result(timeout=5)
            except Exception:
                pass

        # Stop torrent engine
        if status_cb:
            status_cb("Saving BitTorrent resume state...", 60)
        self._torrent.stop()

        # Stop Tor background service
        if status_cb:
            status_cb("Stopping Tor network service...", 80)
        self._tor_service.stop()

        # If Tor was active on exit but auto-start is False, ensure enabled is saved as False
        if not self._tor_config.auto_start_at_startup and self._tor_config.enabled:
            self._tor_config.enabled = False
            self._tor_config.save()

        # Stop asyncio loop
        if status_cb:
            status_cb("Closing background threads...", 95)
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=3)

        # Stop AnimePahe background scraper process and clear queue if running
        with self._animepahe_queue_lock:
            self._animepahe_queue.clear()
        if self._animepahe_process and self._animepahe_process.poll() is None:
            try:
                self._animepahe_process.terminate()
            except Exception:
                pass
            self._animepahe_process = None

        if status_cb:
            status_cb("Shutdown complete.", 100)
        log.info("DownloadManager stopped")

    @property
    def db(self) -> Database:
        return self._db

    @property
    def tor_config(self) -> TorConfig:
        return self._tor_config

    @property
    def tor_service(self) -> TorServiceManager:
        return self._tor_service

    @property
    def external_tools_config(self) -> ExternalToolsConfig:
        return self._external_tools_config

    def set_external_tools_config(self, config: ExternalToolsConfig):
        """Update external tools configuration."""
        self._external_tools_config = config
        config.save()
        self._apply_animepahe_timer_config()
        self.external_tools_config_changed.emit(config)

    def _apply_animepahe_timer_config(self):
        """Configures or starts/stops the periodic AnimePahe scraper timer."""
        cfg = self._external_tools_config
        if cfg.animepahe_periodic_run and cfg.animepahe_interval_hours > 0:
            interval_ms = int(cfg.animepahe_interval_hours * 3600 * 1000)
            self._animepahe_timer.setInterval(interval_ms)
            if not getattr(self, "_stopped", False) and not self._animepahe_timer.isActive():
                self._animepahe_timer.start()
        else:
            self._animepahe_timer.stop()

    def _on_animepahe_timer_tick(self):
        """Periodically triggers AnimePahe scraper in background CLI mode."""
        if getattr(self, "_stopped", False):
            return
        cfg = self._external_tools_config
        if not cfg.animepahe_periodic_run:
            self._animepahe_timer.stop()
            return
        log.info(
            "Triggering scheduled AnimePahe scraper run (interval: every %s hours)",
            cfg.animepahe_interval_hours,
        )
        self.start_animepahe_scraper()

    def start_animepahe_scraper(
        self,
        url: Optional[str] = None,
        episodes: Optional[str] = None,
        quality: Optional[str] = None,
        lang: Optional[str] = None,
    ) -> tuple[bool, str]:
        """Launch the AnimePahe scraper in CLI mode in the background, or queue if already running."""
        with self._animepahe_queue_lock:
            if self._animepahe_process and self._animepahe_process.poll() is None:
                task = {
                    "url": url,
                    "episodes": episodes,
                    "quality": quality,
                    "lang": lang,
                }
                self._animepahe_queue.append(task)
                q_pos = len(self._animepahe_queue)
                self.animepahe_queue_changed.emit(q_pos)
                target_str = f" for '{url}'" if url else ""
                return True, f"AnimePahe task queued at position #{q_pos}{target_str}."

            return self._launch_animepahe_task(url=url, episodes=episodes, quality=quality, lang=lang)

    def _launch_animepahe_task(
        self,
        url: Optional[str] = None,
        episodes: Optional[str] = None,
        quality: Optional[str] = None,
        lang: Optional[str] = None,
    ) -> tuple[bool, str]:
        ok, msg, proc = launch_animepahe_cli(
            self._external_tools_config,
            my_idm_dir=str(Path(__file__).resolve().parent.parent),
            container_hwnd=self._browser_container_hwnd,
            url=url,
            episodes=episodes,
            quality=quality,
            lang=lang,
        )
        if ok and proc:
            self._animepahe_process = proc
            self.animepahe_status_changed.emit(True)

            def _monitor():
                try:
                    proc.wait()
                except Exception:
                    pass

                try:
                    self.process_backlogs()
                except Exception:
                    pass

                next_task = None
                with self._animepahe_queue_lock:
                    if self._animepahe_queue:
                        next_task = self._animepahe_queue.pop(0)
                        self.animepahe_queue_changed.emit(len(self._animepahe_queue))

                if next_task and not self._stopped:
                    self._launch_animepahe_task(
                        url=next_task.get("url"),
                        episodes=next_task.get("episodes"),
                        quality=next_task.get("quality"),
                        lang=next_task.get("lang"),
                    )
                else:
                    with self._animepahe_queue_lock:
                        if self._animepahe_process == proc:
                            self._animepahe_process = None
                    self.animepahe_status_changed.emit(False)

            threading.Thread(target=_monitor, daemon=True, name="animepahe-monitor").start()
        return ok, msg

    def stop_animepahe_scraper(self) -> tuple[bool, str]:
        """Stop running AnimePahe background scraper process and clear any queued tasks."""
        with self._animepahe_queue_lock:
            q_cleared = len(self._animepahe_queue)
            self._animepahe_queue.clear()
            self.animepahe_queue_changed.emit(0)

        if not self._animepahe_process or self._animepahe_process.poll() is not None:
            self._animepahe_process = None
            self.animepahe_status_changed.emit(False)
            if q_cleared > 0:
                return True, f"Cleared {q_cleared} queued AnimePahe task(s)."
            return True, "AnimePahe scraper is not running."

        try:
            self._animepahe_process.terminate()
            self._animepahe_process = None
            self.animepahe_status_changed.emit(False)
            extra = f" (and cleared {q_cleared} queued task{'s' if q_cleared != 1 else ''})" if q_cleared > 0 else ""
            return True, f"Stopped AnimePahe scraper{extra}."
        except Exception as exc:
            return False, f"Failed to stop AnimePahe scraper: {exc}"

    def is_animepahe_running(self) -> bool:
        return self._animepahe_process is not None and self._animepahe_process.poll() is None

    def get_animepahe_queue_length(self) -> int:
        with self._animepahe_queue_lock:
            return len(self._animepahe_queue)

    def get_animepahe_queue(self) -> list[dict[str, Any]]:
        with self._animepahe_queue_lock:
            return list(self._animepahe_queue)

    @property
    def animepahe_process(self) -> Optional[subprocess.Popen]:
        return self._animepahe_process

    @property
    def browser_container_hwnd(self) -> Optional[int]:
        return self._browser_container_hwnd

    def set_browser_container_hwnd(self, hwnd: Optional[int]):
        self._browser_container_hwnd = hwnd

    @property
    def browser_config(self) -> BrowserIntegrationConfig:
        return self._browser_config

    @property
    def browser_server(self) -> BrowserServer:
        return self._browser_server

    def set_browser_config(self, config: BrowserIntegrationConfig):
        """Update browser extension integration configuration."""
        old_enabled = self._browser_config.enabled
        old_port = self._browser_config.port
        self._browser_config = config
        self._browser_server.set_config(config)
        config.save()
        self.browser_config_changed.emit(config)

        # Restart or stop/start browser server if loop is running
        if self._loop and self._loop.is_running():
            if not config.enabled and self._browser_server.is_running:
                asyncio.run_coroutine_threadsafe(self._browser_server.stop(), self._loop)
            elif config.enabled and (not self._browser_server.is_running or old_port != config.port):
                async def _restart_server():
                    await self._browser_server.stop()
                    await self._browser_server.start()
                asyncio.run_coroutine_threadsafe(_restart_server(), self._loop)

    def set_tor_config(self, config: TorConfig):
        """Update Tor routing and SOCKS5 proxy configuration."""
        self._tor_config = config
        self._tor_service._config = config
        config.save()

        if self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._http.set_tor_config(config), self._loop
                )
            except Exception as e:
                log.warning("Failed to update HTTP engine Tor config: %s", e)
        else:
            self._http.set_tor_config_sync(config)

        self._torrent.apply_tor_config(config)
        self.tor_config_changed.emit(config)

    def toggle_tor(self, enable: Optional[bool] = None) -> tuple[bool, str]:
        """Toggle or set Tor activation status, pausing active downloads beforehand and resuming after."""
        target = (not self._tor_config.enabled) if enable is None else bool(enable)

        # 1. Identify ongoing downloads to pause before Tor transition
        active_ids = []
        try:
            if self._db and getattr(self._db, "_conn", None) is not None:
                active_ids = [
                    e.id for e in self._db.get_all_downloads()
                    if e.status in ("downloading", "fetching_metadata", "stalled")
                ]
        except Exception:
            active_ids = []

        if active_ids:
            log.info("Pausing %d ongoing download(s) before Tor state change: %s", len(active_ids), active_ids)
            for did in active_ids:
                self.pause_download(did)

        # 2. Emit connecting / disconnecting status
        self.tor_status_changed.emit(
            "connecting" if target else "disconnecting",
            "Connecting..." if target else "Disconnecting..."
        )

        # 3. Perform Tor service start / stop
        if target:
            success, msg = self._tor_service.start(timeout=15.0)
            if not success:
                self._tor_config.enabled = False
                self.set_tor_config(self._tor_config)
                self.tor_status_changed.emit("error", msg)
                # Resume previously active downloads with direct routing
                if active_ids:
                    log.info("Resuming %d download(s) after Tor start failure", len(active_ids))
                    for did in active_ids:
                        self.resume_download(did)
                return False, msg

            self._tor_config.enabled = True
            self.set_tor_config(self._tor_config)
            self.tor_status_changed.emit("connected", msg)
        else:
            self._tor_service.stop()
            self._tor_config.enabled = False
            self.set_tor_config(self._tor_config)
            self.tor_status_changed.emit("disconnected", "Tor deactivated")
            msg = "Tor deactivated"

        # 4. Resume previously active downloads with the updated Tor routing
        if active_ids:
            log.info("Resuming %d ongoing download(s) after Tor state change", len(active_ids))
            for did in active_ids:
                self.resume_download(did)

        return True, msg

    @property
    def network_config(self) -> NetworkConfig:
        return self._network_config

    def set_network_config(self, config: NetworkConfig):
        """Update network interface binding, VPN kill switch, or proxy settings."""
        self._network_config = config
        config.save()

        if self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._http.set_network_config(config), self._loop
                )
            except Exception as e:
                log.warning("Failed to update HTTP engine network config: %s", e)
        else:
            self._http.set_network_config_sync(config)

        self._torrent.apply_network_config(config)
        self._torrent.set_session_limits(config.download_limit, config.upload_limit)
        self._http.set_download_limit(config.download_limit)
        self.network_config_changed.emit(config)

    def set_bandwidth_limits(self, download_limit: int, upload_limit: int):
        """Set global download and upload speed limits in bytes/second (0 = unlimited)."""
        self._network_config.download_limit = download_limit
        self._network_config.upload_limit = upload_limit
        self._network_config.save()
        self._torrent.set_session_limits(download_limit, upload_limit)
        self._http.set_download_limit(download_limit)
        self.bandwidth_limits_changed.emit(download_limit, upload_limit)

    def set_download_bandwidth_allocation(self, download_id: str, allocation: str):
        """Set bandwidth allocation ('low', 'medium', 'high', 'max') for a specific download."""
        entry = self._db.get_download(download_id)
        if not entry:
            return
        if not entry.metadata:
            entry.metadata = {}
        entry.metadata["bandwidth_allocation"] = allocation
        self._db.update_download(entry)
        if entry.download_type == "torrent":
            self._torrent.set_torrent_bandwidth_allocation(download_id, allocation)
        else:
            self._http.set_download_bandwidth_allocation(download_id, allocation)

    def get_download_bandwidth_allocation(self, download_id: str) -> str:
        """Get bandwidth allocation ('low', 'medium', 'high', 'max') for a download."""
        entry = self._db.get_download(download_id)
        if not entry or not entry.metadata:
            return "max"
        return entry.metadata.get("bandwidth_allocation", "max")

    @property
    def security_config(self) -> SecurityConfig:
        return self._security_config

    def set_security_config(self, config: SecurityConfig):
        """Update antivirus and security configuration."""
        self._security_config = config
        config.save()
        self.security_config_changed.emit(config)

    @property
    def general_config(self) -> GeneralConfig:
        return self._general_config

    def set_general_config(self, config: GeneralConfig):
        """Update general download and application preferences."""
        self._general_config = config
        self._http.set_general_config_sync(config)
        config.save()
        self._apply_backlog_timer_config()
        self.general_config_changed.emit(config)
        self._process_queue()

    @property
    def torrent_config(self) -> TorrentConfig:
        return self._torrent_config

    def set_torrent_config(self, config: TorrentConfig):
        """Update BitTorrent engine preferences and seeding configuration."""
        self._torrent_config = config
        self._torrent.apply_torrent_config(config)
        config.save()
        self.torrent_config_changed.emit(config)

    def _apply_backlog_timer_config(self):
        interval_ms = max(1, self._general_config.backlog_poll_interval) * 1000
        self._backlog_timer.setInterval(interval_ms)
        if getattr(self, "_thread", None) and self._thread.is_alive():
            if self._general_config.backlog_poll_enabled and self._general_config.backlog_poll_interval > 0:
                if not self._backlog_timer.isActive():
                    self._backlog_timer.start()
            else:
                self._backlog_timer.stop()

    def _on_backlog_timer_tick(self):
        try:
            count = self.process_backlogs()
            if count > 0:
                log.info("Periodic backlog poll added %d download(s)", count)
        except Exception as exc:
            log.error("Error in periodic backlog poll: %s", exc)

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # -- add downloads -------------------------------------------------------

    def add_download(self, url: str, save_path: str = "",
                     num_segments: int = 8,
                     filename: str = "",
                     headers: Optional[dict] = None,
                     metadata: Optional[dict] = None) -> Optional[str]:
        """Add a new download. Returns download_id or None if duplicate resumed."""
        url = url.strip()
        if not url:
            return None

        # Pre-download security check
        is_safe, risk_level, sec_details = check_url_safety(url, self._security_config)
        if not is_safe and self._security_config.block_dangerous_urls:
            log.warning(
                "Download blocked by pre-download security policy: %s (%s)",
                url, sec_details,
            )
            return None

        if not save_path:
            save_path = self._general_config.get_effective_save_path()

        if num_segments <= 0:
            num_segments = self._general_config.default_segments

        download_type = self._detect_type(url)

        # Deduplication
        existing = self._db.find_by_url(url)
        if existing:
            if existing.status in ("completed", "seeding"):
                log.info("URL already completed: %s", url)
                return None  # Already done
            if existing.status in ("queued", "paused", "error", "stopped"):
                log.info("Resuming existing download: %s", existing.id)
                self.resume_download(existing.id)
                return existing.id
            # Already downloading
            return existing.id

        explicit_fn = bool(filename and filename.strip())

        # Extract initial filename if not explicitly provided
        if not filename:
            if download_type == "torrent":
                if os.path.isfile(url):
                    filename = Path(url).stem
                elif url.startswith("magnet:?"):
                    try:
                        parsed_qs = parse_qs(urlparse(url).query)
                        dns = parsed_qs.get("dn", [])
                        if dns and dns[0]:
                            filename = unquote(dns[0])
                    except Exception:
                        pass
            elif download_type == "http":
                try:
                    parsed = urlparse(url)
                    parsed_path = parsed.path
                    if parsed_path:
                        name = unquote(Path(parsed_path).name)
                        if name and "." in name:
                            filename = name
                    # Fallback to query params if path has no extension
                    if not filename:
                        qs = parse_qs(parsed.query)
                        for k in ("file", "filename", "name", "title"):
                            val = qs.get(k)
                            if val and val[0]:
                                cand = unquote(val[0])
                                if "." in cand:
                                    filename = Path(cand).name
                                    break
                    if not filename and parsed_path:
                        filename = unquote(Path(parsed_path).name)
                except Exception:
                    pass

        # Auto-number filename if already taken on disk or by another download in DB
        if filename:
            existing_downloads = self._db.get_all_downloads()
            reserved = {
                d.filename for d in existing_downloads
                if d.save_path == save_path and d.filename
            }
            if download_type == "torrent":
                # For torrents, do not auto-number based on existing on-disk files;
                # only avoid collision with another active/known download in DB.
                # This ensures torrent files that already exist on disk can be rechecked
                # and resumed rather than silently renamed to 'filename (1)'.
                if filename.lower() in {r.lower() for r in reserved}:
                    filename = get_unique_filename(save_path, filename, reserved_names=reserved)
            else:
                filename = get_unique_filename(save_path, filename, reserved_names=reserved)

        save_path = normalize_path(save_path)
        entry_metadata = metadata.copy() if metadata else {}
        if explicit_fn:
            entry_metadata["explicit_filename"] = True
        if filename and not entry_metadata.get("original_name"):
            entry_metadata["original_name"] = filename
        if risk_level != "clean":
            entry_metadata["security_warning"] = sec_details
        if headers:
            entry_metadata["headers"] = headers

        # Create new entry
        entry = DownloadEntry(
            id=str(uuid.uuid4()),
            url=url,
            save_path=save_path,
            filename=filename,
            file_path=normalize_path(Path(save_path) / filename) if filename else "",
            download_type=download_type,
            num_segments=num_segments,
            added_at=_now_iso(),
            status="queued",
        )
        if entry_metadata:
            entry.metadata = entry_metadata

        self._db.add_download(entry)
        self.download_added.emit(entry.id)

        # Start if within concurrent limit; otherwise stays queued
        max_concurrent = self._general_config.max_concurrent_downloads
        if max_concurrent <= 0:
            max_concurrent = 3
        if self._get_active_download_count() < max_concurrent:
            self._start_entry(entry)
        else:
            entry.status = "queued"
            self._db.update_download(entry)
            self.status_changed.emit(entry.id, "queued", "")

        return entry.id

    def add_download_from_browser(
        self,
        url: str,
        filename: str = "",
        save_path: str = "",
        headers: Optional[dict] = None,
        cookies: Optional[Union[str, dict]] = None,
        referrer: str = "",
        user_agent: str = "",
    ) -> Optional[str]:
        """Add a download initiated from the browser extension."""
        combined_headers = {}
        if headers and isinstance(headers, dict):
            combined_headers.update(headers)
        if referrer:
            combined_headers["Referer"] = referrer
        if user_agent:
            combined_headers["User-Agent"] = user_agent
        if cookies:
            if isinstance(cookies, dict):
                combined_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
            elif isinstance(cookies, str) and cookies.strip():
                combined_headers["Cookie"] = cookies.strip()

        meta: dict[str, Any] = {"source": "browser_extension"}
        if referrer:
            meta["referer"] = referrer
        if cookies:
            meta["cookies"] = cookies
        if user_agent:
            meta["user_agent"] = user_agent

        download_id = self.add_download(
            url=url,
            save_path=save_path or self._general_config.get_effective_save_path(),
            num_segments=self._general_config.default_segments,
            filename=filename,
            headers=combined_headers,
            metadata=meta,
        )

        if download_id:
            entry = self._db.get_download(download_id)
            if entry:
                notify_browser_download_caught(entry.filename or entry.id, url)

        return download_id

    def _get_active_download_count(self) -> int:
        """Count downloads currently in active transferring states or starting."""
        entries = self._db.get_all_downloads()
        active_ids = {
            e.id for e in entries
            if e.status in ("downloading", "checking", "fetching_metadata", "stalled")
        }
        active_ids.update(self._starting_downloads)
        return len(active_ids)

    def _process_queue(self):
        """Start queued downloads up to the concurrent limit, ordered by priority."""
        max_concurrent = self._general_config.max_concurrent_downloads
        if max_concurrent <= 0:
            max_concurrent = 3

        active = self._get_active_download_count()
        available = max_concurrent - active
        if available <= 0:
            return

        now = time.time()
        queued = [
            e for e in self._db.get_all_downloads()
            if e.status == "queued"
        ]
        eligible = []
        for e in queued:
            if e.retry_count > 0:
                next_retry_at = e.metadata.get("next_retry_at", 0) if e.metadata else 0
                if now < next_retry_at or e.retry_count >= e.max_retries:
                    continue
            eligible.append(e)

        eligible.sort(key=lambda e: (e.queue_order if e.queue_order > 0 else 999999, e.added_at or ""))

        started = 0
        for entry in eligible:
            if started >= available:
                break
            if self._get_active_download_count() >= max_concurrent:
                break
            if entry.retry_count > 0:
                log.info(
                    "Auto-retrying queued download %s (attempt %d/%d, order=%s)",
                    entry.id, entry.retry_count + 1, entry.max_retries, entry.queue_order,
                )
            else:
                log.info("Starting queued download %s (order=%s)", entry.id, entry.queue_order)
            self._start_entry(entry)
            started += 1

    def _start_entry(self, entry: DownloadEntry):
        """Dispatch download to the right engine."""
        self._starting_downloads.add(entry.id)
        # Kill switch check
        if (
            self._network_config
            and self._network_config.kill_switch
            and self._network_config.is_interface_bound
        ):
            if not is_interface_active(
                self._network_config.interface_name,
                self._network_config.interface_ip,
            ):
                self._starting_downloads.discard(entry.id)
                err = (
                    f"VPN / Bound interface '{self._network_config.interface_name}' "
                    "disconnected (Kill switch active)"
                )
                log.warning(err)
                self._db.update_status(entry.id, "error", err)
                self.status_changed.emit(entry.id, "error", err)
                return

        if entry.download_type == "http":
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._http.add(entry), self._loop
                )
        elif entry.download_type == "torrent":
            if not self._torrent.available:
                self._starting_downloads.discard(entry.id)
                self._db.update_status(
                    entry.id, "error",
                    "libtorrent not installed — torrent support disabled",
                )
                self.status_changed.emit(
                    entry.id, "error",
                    "libtorrent not installed — torrent support disabled",
                )
                return
            if not self._torrent.add_torrent(entry):
                self._starting_downloads.discard(entry.id)
                err = f"Failed to add torrent — invalid source or parse error: {entry.url}"
                log.warning(err)
                self._db.update_status(entry.id, "error", err)
                self.status_changed.emit(entry.id, "error", err)

    # -- pause / resume / delete ---------------------------------------------

    def pause_download(self, download_id: str):
        entry = self._db.get_download(download_id)
        if not entry:
            return

        if entry.status == "completed":
            return

        if entry.status == "seeding":
            self._starting_downloads.discard(download_id)
            if entry.download_type == "torrent":
                self._torrent.pause(download_id)
            self._db.update_status(download_id, "completed")
            self.status_changed.emit(download_id, "completed", "")
            self.progress_updated.emit(
                download_id, entry.downloaded_size, entry.total_size, 0.0, 0.0, 0, 0, 0.0
            )
            self._process_queue()
            return

        self._starting_downloads.discard(download_id)
        self._db.update_status(download_id, "paused")
        self._db.update_queue_order(download_id, 0)
        self.status_changed.emit(download_id, "paused", "")

        if entry.download_type == "http":
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._http.pause(download_id), self._loop
                )
        elif entry.download_type == "torrent":
            self._torrent.pause(download_id)

        self.progress_updated.emit(
            download_id, entry.downloaded_size, entry.total_size, 0.0, 0.0, 0, 0, 0.0
        )
        self._process_queue()

    def stop_download(self, download_id: str):
        """Permanently stop a download. It will never be auto-retried or auto-resumed.

        Only an explicit resume_download() or force_start_download() will restart it.
        The download is removed from the active queue (queue_order set to 0).
        """
        entry = self._db.get_download(download_id)
        if not entry:
            return

        if entry.status == "completed":
            return

        if entry.status == "seeding":
            self._starting_downloads.discard(download_id)
            if entry.download_type == "torrent":
                self._torrent.pause(download_id)
            entry.status = "completed"
            entry.queue_order = 0
            self._db.update_download(entry)
            self.status_changed.emit(download_id, "completed", "")
            self.progress_updated.emit(
                download_id, entry.downloaded_size, entry.total_size, 0.0, 0.0, 0, 0, 0.0
            )
            self._process_queue()
            return

        self._starting_downloads.discard(download_id)

        # Halt the transfer in the engine
        if entry.download_type == "http":
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._http.pause(download_id), self._loop
                )
        elif entry.download_type == "torrent":
            self._torrent.pause(download_id)

        # Update DB: stopped status, clear queue position
        entry.status = "stopped"
        entry.queue_order = 0
        self._db.update_download(entry)

        self.status_changed.emit(download_id, "stopped", "")
        self.progress_updated.emit(
            download_id, entry.downloaded_size, entry.total_size, 0.0, 0.0, 0, 0, 0.0
        )
        self._process_queue()

    def start_seeding(self, download_id: str):
        """Start or resume seeding for a completed or stopped torrent."""
        entry = self._db.get_download(download_id)
        if not entry or entry.download_type != "torrent":
            return
        if self._torrent.start_seeding(download_id):
            self.status_changed.emit(download_id, "seeding", "")

    def stop_all_seeding(self) -> int:
        """Stop all torrents that are currently in the 'seeding' state.

        Transitions each seeding torrent to 'completed' and pauses its engine handle.
        Returns the number of stopped seeding torrents.
        """
        seeding_entries = [
            e for e in self._db.get_all_downloads()
            if e.download_type == "torrent" and e.status == "seeding"
        ]
        count = 0
        for entry in seeding_entries:
            self.stop_download(entry.id)
            count += 1
        return count

    def pause_all_downloads(self) -> int:
        """Pause all ongoing and queued downloads.

        Targets transfers in 'downloading', 'queued', 'checking', 'fetching_metadata',
        and 'stalled' states, transitioning each to 'paused'.
        Returns the number of paused downloads.
        """
        pausable = [
            e for e in self._db.get_all_downloads()
            if e.status in ("downloading", "queued", "checking", "fetching_metadata", "stalled")
        ]
        count = 0
        for entry in pausable:
            self.pause_download(entry.id)
            count += 1
        return count

    def resume_all_downloads(self) -> int:
        """Resume all paused or stopped downloads.

        Targets transfers in 'paused' or 'stopped' states, transitioning each to 'queued'
        and processing queue.
        Returns the number of resumed downloads.
        """
        resumable = [
            e for e in self._db.get_all_downloads()
            if e.status in ("paused", "stopped")
        ]
        count = 0
        for entry in resumable:
            self.resume_download(entry.id)
            count += 1
        return count

    def resume_download(self, download_id: str):
        entry = self._db.get_download(download_id)
        if not entry:
            return

        if entry.status == "completed":
            return

        if download_id in self._starting_downloads:
            log.warning("Download %s is already starting, skipping duplicate resume", download_id)
            return

        # Reset retries and error state so manual or auto-resume always gets fresh attempts
        # If download was already fetching_metadata, keep its timer; if suspended/stopped/paused, reset it
        if entry.status != "fetching_metadata":
            entry.fetching_metadata_since = ""
        entry.status = "queued"
        entry.retry_count = 0
        entry.error_message = ""
        entry.last_tried_at = _now_iso()
        if entry.queue_order <= 0:
            entry.queue_order = self._db.get_next_queue_order()
        if not entry.file_path and entry.filename and entry.save_path:
            entry.file_path = str(Path(entry.save_path) / entry.filename)
        self._db.update_download(entry)
        self.status_changed.emit(download_id, "queued", "")

        max_concurrent = self._general_config.max_concurrent_downloads
        if max_concurrent <= 0:
            max_concurrent = 3
        if self._get_active_download_count() >= max_concurrent:
            return

        self._start_entry(entry)

    def force_start_download(self, download_id: str):
        """Immediately force start a download, resetting retries/errors and bypassing paused/queued limits."""
        entry = self._db.get_download(download_id)
        if not entry or entry.status == "completed":
            return

        entry.status = "downloading"
        entry.retry_count = 0
        entry.error_message = ""
        entry.last_tried_at = _now_iso()
        # Clear fetching_metadata_since when force starting (resets the timer)
        entry.fetching_metadata_since = ""
        if not entry.file_path and entry.filename and entry.save_path:
            entry.file_path = str(Path(entry.save_path) / entry.filename)
        self._db.update_download(entry)

        if entry.download_type == "http":
            if not self._http.is_active(download_id):
                if self._loop:
                    asyncio.run_coroutine_threadsafe(
                        self._http.add(entry), self._loop
                    )
        elif entry.download_type == "torrent":
            if download_id in self._torrent._handles:
                self._torrent.force_start(download_id)
            else:
                self._torrent.add_torrent(entry)

        target_status = "downloading"
        if entry.download_type == "torrent" and not entry.total_size:
            target_status = "fetching_metadata"
            self._db.update_status(download_id, target_status)

        self.status_changed.emit(download_id, target_status, "")

    def delete_download(self, download_id: str, delete_files: bool = False):
        entry = self._db.get_download(download_id)
        if not entry:
            return

        # Stop active download
        if entry.download_type == "http":
            if self._loop and self._loop.is_running():
                fut = asyncio.run_coroutine_threadsafe(
                    self._http.cancel(download_id), self._loop
                )
                try:
                    fut.result(timeout=5.0)
                except Exception:
                    pass
        elif entry.download_type == "torrent":
            self._torrent.remove(download_id, delete_files=False)

        # Move files to trash if requested
        if delete_files:
            target_path = entry.file_path
            if not target_path and entry.save_path and entry.filename:
                target_path = normalize_path(Path(entry.save_path) / entry.filename)
            if target_path:
                fp = Path(target_path)
                if fp.exists():
                    success = send_to_trash(fp)
                    if not success and fp.exists():
                        log.warning("Failed to move file to trash: %s", fp)

        # Remove from DB
        self._db.delete_segments(download_id)
        self._db.delete_download(download_id)
        self.download_removed.emit(download_id)
        self._process_queue()

    def delete_download_file(self, download_id: str):
        """Delete downloaded files from disk while keeping the entry in DB paused at 0%."""
        entry = self._db.get_download(download_id)
        if not entry:
            return

        # 1. Stop active download
        if entry.download_type == "http":
            if self._loop and self._loop.is_running():
                fut = asyncio.run_coroutine_threadsafe(
                    self._http.cancel(download_id), self._loop
                )
                try:
                    fut.result(timeout=5.0)
                except Exception:
                    pass
        elif entry.download_type == "torrent":
            self._torrent.remove(download_id, delete_files=False)

        # 2. Move file / directory to trash
        target_path = entry.file_path
        if not target_path and entry.save_path and entry.filename:
            target_path = normalize_path(Path(entry.save_path) / entry.filename)
        if target_path:
            fp = Path(target_path)
            if fp.exists():
                success = send_to_trash(fp)
                if not success and fp.exists():
                    log.warning("Failed to move file to trash: %s", fp)

        # 3. Clean up HTTP segment records in DB
        self._db.delete_segments(download_id)

        # 4. Reset entry progress, speed, and status to paused
        entry.downloaded_size = 0
        entry.status = "paused"
        entry.speed = 0.0
        entry.upload_speed = 0.0
        entry.eta_seconds = 0.0
        self._db.update_download(entry)

        # 5. Emit status and progress signals
        self.status_changed.emit(download_id, "paused", "")
        self.progress_updated.emit(
            download_id, 0, entry.total_size, 0.0, 0.0, 0, 0, 0.0
        )
        self._process_queue()

    # -- move ----------------------------------------------------------------

    def move_download(self, download_id: str, new_save_path: str):
        entry = self._db.get_download(download_id)
        if not entry:
            return

        new_save_path = normalize_path(new_save_path)
        old_file_path = (
            Path(entry.file_path)
            if entry.file_path
            else (Path(entry.save_path) / (entry.filename or ""))
        )
        new_file_path = normalize_path(Path(new_save_path) / old_file_path.name)

        if entry.download_type == "torrent":
            handle = self._torrent._handles.get(download_id)
            was_seeding = (entry.status == "seeding")
            was_active = False
            if handle:
                try:
                    s = handle.status()
                    was_active = not s.is_paused
                except Exception:
                    pass
                try:
                    handle.pause()
                    handle.flush_cache()
                except Exception:
                    pass
                time.sleep(0.1)

            # Move files robustly (handles partial moves, unlocking permissions, sharing violations)
            if old_file_path.exists() or Path(new_file_path).exists():
                success, err = robust_move_download_files(old_file_path, new_file_path)
                if not success:
                    log.error("Failed to move torrent files for %s: %s", download_id, err)
                    if was_active and handle:
                        try:
                            handle.resume()
                        except Exception:
                            pass
                    return

            self._torrent.move_storage(download_id, new_save_path)
            self._db.move_download(
                download_id, new_save_path, new_file_path
            )

            if was_active and handle:
                try:
                    handle.resume()
                    if was_seeding:
                        self._torrent._apply_seeding_limit_to_handle(handle)
                except Exception:
                    pass
        else:
            # For HTTP: pause → move → update → resume
            was_active = self._http.is_active(download_id)
            if was_active:
                if self._loop:
                    future = asyncio.run_coroutine_threadsafe(
                        self._http.pause(download_id), self._loop
                    )
                    try:
                        future.result(timeout=10)
                    except Exception:
                        pass

            # Move the file robustly
            if old_file_path.exists() or Path(new_file_path).exists():
                success, err = robust_move_download_files(old_file_path, new_file_path)
                if not success:
                    log.error("Failed to move HTTP file for %s: %s", download_id, err)
                    return

            self._db.move_download(
                download_id, new_save_path, new_file_path
            )

            # Resume if it was active
            if was_active:
                entry = self._db.get_download(download_id)
                if entry and self._loop:
                    asyncio.run_coroutine_threadsafe(
                        self._http.add(entry), self._loop
                    )

        self.download_moved.emit(download_id)

    def rename_download(self, download_id: str, new_filename: str) -> tuple[bool, str]:
        """Rename the root file or folder of an HTTP or Torrent download.

        Can be invoked at any point of time (before start, during download, or after completion).
        Returns (success: bool, message: str).
        """
        entry = self._db.get_download(download_id)
        if not entry:
            return False, "Download not found."

        new_name = new_filename.strip()
        if not new_name:
            return False, "Filename cannot be empty."

        # Disallow filesystem-illegal characters
        invalid_chars = set(r'<>:"/\|?*' + "".join(chr(i) for i in range(32)))
        if any(c in invalid_chars for c in new_name):
            return False, "Filename contains invalid characters (< > : \" / \\ | ? *)."

        old_filename = entry.filename or ""
        if new_name == old_filename:
            return True, "Filename is unchanged."

        save_path = entry.save_path or self._general_config.get_effective_save_path()
        old_file_path = (
            Path(entry.file_path)
            if entry.file_path
            else (Path(save_path) / old_filename if old_filename else None)
        )
        new_file_path = normalize_path(Path(save_path) / new_name)

        # Check collision with existing file/folder
        if Path(new_file_path).exists():
            if not old_file_path or normalize_path(old_file_path) != new_file_path:
                other_downloads = [
                    d for d in self._db.get_all_downloads()
                    if d.id != download_id and (
                        d.file_path == new_file_path or (
                            d.save_path == save_path and d.filename == new_name
                        )
                    )
                ]
                if other_downloads:
                    return False, f"A file or folder named '{new_name}' already exists in the save folder."

                # If it's a torrent and new_file_path is a directory, check if it's a previous partial move
                is_partial_torrent_move = False
                if entry.download_type == "torrent" and Path(new_file_path).is_dir():
                    torrent_files = []
                    if entry.metadata and "files" in entry.metadata and isinstance(entry.metadata["files"], list):
                        torrent_files = [Path(f.get("path", "")).name for f in entry.metadata["files"] if f.get("path")]
                    if not torrent_files and old_file_path and Path(old_file_path).is_dir():
                        torrent_files = [p.name for p in Path(old_file_path).iterdir() if p.is_file()]

                    dst_files = [p.name for p in Path(new_file_path).iterdir() if p.is_file()]
                    if dst_files and any(f in torrent_files for f in dst_files):
                        is_partial_torrent_move = True

                if not is_partial_torrent_move:
                    return False, f"A file or folder named '{new_name}' already exists in the save folder."

        if not entry.metadata:
            entry.metadata = {}
        if not entry.metadata.get("original_name"):
            from my_idm.download_model import DownloadTableModel
            entry.metadata["original_name"] = DownloadTableModel.get_original_name(entry)

        if entry.download_type == "torrent":
            success = self._torrent.rename_root(download_id, new_name)
            if not success:
                return False, "Failed to rename torrent root file/folder."

            entry.filename = new_name
            entry.file_path = new_file_path
            if not entry.metadata:
                entry.metadata = {}
            entry.metadata["explicit_filename"] = True
            files = self._torrent.get_torrent_files(download_id)
            if files:
                entry.metadata["files"] = files
            self._db.update_download(entry)
        else:
            # HTTP download
            was_active = self._http.is_active(download_id)
            if was_active:
                if self._loop and self._loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        self._http.pause(download_id), self._loop
                    )
                    try:
                        future.result(timeout=10)
                    except Exception as exc:
                        log.warning("Timeout or error pausing %s for rename: %s", download_id, exc)

            # Move file on disk if it exists
            if old_file_path and old_file_path.exists() and normalize_path(old_file_path) != new_file_path:
                try:
                    Path(new_file_path).parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(old_file_path), str(new_file_path))
                except Exception as exc:
                    log.error("Failed to rename disk file from %s to %s: %s", old_file_path, new_file_path, exc)
                    # If was active, resume
                    if was_active and self._loop and self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(self._http.add(entry), self._loop)
                    return False, f"Could not rename file on disk: {exc}"

            entry.filename = new_name
            entry.file_path = new_file_path
            if not entry.metadata:
                entry.metadata = {}
            entry.metadata["explicit_filename"] = True
            self._db.update_download(entry)

            # Resume if active
            if was_active and self._loop and self._loop.is_running():
                refreshed_entry = self._db.get_download(download_id)
                if refreshed_entry:
                    asyncio.run_coroutine_threadsafe(
                        self._http.add(refreshed_entry), self._loop
                    )

        self.download_renamed.emit(download_id, new_name)
        log.info("Renamed download %s to '%s'", download_id, new_name)
        return True, ""

    def mark_file_not_found(self, download_id: str):
        """Mark download status as file_not_found when missing on disk."""
        entry = self._db.get_download(download_id)
        if not entry:
            return
        entry.status = "file_not_found"
        entry.error_message = "File not found on disk"
        self._db.update_status(download_id, "file_not_found", "File not found on disk")
        self.status_changed.emit(download_id, "file_not_found", "File not found on disk")

    def move_queue_up(self, download_id: str) -> bool:
        """Move download up in queue order."""
        downloads = self._db.get_all_downloads()
        downloads.sort(key=lambda d: d.queue_order if d.queue_order > 0 else 999999)
        idx = next((i for i, d in enumerate(downloads) if d.id == download_id), -1)
        if idx > 0:
            target = downloads[idx - 1]
            curr = downloads[idx]
            curr_order = curr.queue_order if curr.queue_order > 0 else (idx + 1)
            target_order = target.queue_order if target.queue_order > 0 else idx
            self._db.update_queue_order(curr.id, target_order)
            self._db.update_queue_order(target.id, curr_order)
            self.queue_order_changed.emit()
            return True
        return False

    def move_queue_down(self, download_id: str) -> bool:
        """Move download down in queue order."""
        downloads = self._db.get_all_downloads()
        downloads.sort(key=lambda d: d.queue_order if d.queue_order > 0 else 999999)
        idx = next((i for i, d in enumerate(downloads) if d.id == download_id), -1)
        if idx != -1 and idx < len(downloads) - 1:
            target = downloads[idx + 1]
            curr = downloads[idx]
            curr_order = curr.queue_order if curr.queue_order > 0 else (idx + 1)
            target_order = target.queue_order if target.queue_order > 0 else (idx + 2)
            self._db.update_queue_order(curr.id, target_order)
            self._db.update_queue_order(target.id, curr_order)
            self.queue_order_changed.emit()
            return True
        return False

    # -- recheck -------------------------------------------------------------

    def recheck_download(self, download_id: str):
        entry = self._db.get_download(download_id)
        if not entry:
            return

        if entry.download_type == "torrent":
            if download_id not in self._torrent._handles:
                self._torrent.add_torrent(entry)
            self._db.update_status(download_id, "checking")
            self._torrent.recheck(download_id)
            self.status_changed.emit(download_id, "checking", "")
        else:
            # HTTP: the engine pre-allocates the full file via truncate(), so
            # st_size is always == total_size even when the download is partial.
            # Use the segment downloaded_bytes sum (or entry.downloaded_size from
            if not entry.file_path and entry.filename and entry.save_path:
                entry.file_path = str(Path(entry.save_path) / entry.filename)
            fp = Path(entry.file_path) if entry.file_path else None
            if not fp or not fp.exists():
                # File doesn't exist at all — full reset
                entry.downloaded_size = 0
                entry.status = "queued"
                self._db.update_download(entry)
                self._db.delete_segments(download_id)
                self.status_changed.emit(download_id, "queued", "File not found")
                self.progress_updated.emit(
                    download_id, 0, entry.total_size,
                    0.0, 0.0, 0, 0, 0.0,
                )
            else:
                # File exists — use actual written bytes from segment records
                segments = self._db.get_segments(download_id)
                if segments:
                    actual_downloaded = sum(s.downloaded_bytes for s in segments)
                    all_complete = all(s.status == "completed" for s in segments)
                else:
                    # No segment records (e.g. single-stream / non-segmented);
                    # fall back to disk file size or entry.downloaded_size stored in the DB.
                    disk_size = fp.stat().st_size if fp and fp.exists() and fp.is_file() else 0
                    actual_downloaded = max(entry.downloaded_size, disk_size)
                    if entry.total_size <= 0 and disk_size > 0:
                        entry.total_size = disk_size
                    all_complete = (
                        entry.total_size > 0
                        and actual_downloaded >= entry.total_size
                    )

                if all_complete or (
                    entry.total_size > 0
                    and actual_downloaded >= entry.total_size
                ):
                    # Fully written — confirm completed
                    self._db.update_status(download_id, "completed")
                    self._db.update_progress(download_id, actual_downloaded)
                    self.status_changed.emit(download_id, "completed", "")
                    self.progress_updated.emit(
                        download_id, actual_downloaded, entry.total_size,
                        0.0, 0.0, 0, 0, 0.0,
                    )
                    self._process_queue()
                else:
                    # Partial — update size and reset to paused
                    entry.downloaded_size = actual_downloaded
                    if entry.status in ("completed", "downloading"):
                        entry.status = "paused"
                    self._db.update_download(entry)
                    self.status_changed.emit(
                        download_id, entry.status,
                        f"Downloaded: {actual_downloaded} / {entry.total_size}",
                    )
                    self.progress_updated.emit(
                        download_id, actual_downloaded, entry.total_size,
                        0.0, 0.0, 0, 0, 0.0,
                    )
                    self._process_queue()

    # -- backlog -------------------------------------------------------------

    def load_backlog(self, filepath: str) -> int:
        """Load URLs from a backlog file. Supports per-line and section download locations.
        If clear_backlog_after_load is enabled, successfully processed entries are cleared.
        Returns count of newly added downloads."""
        p = Path(filepath)
        if not p.is_file():
            log.warning("Backlog file not found: %s", filepath)
            return 0

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as exc:
            log.error("Failed to read backlog %s: %s", filepath, exc)
            return 0

        count = 0
        active_save_path = ""
        entries_info: list[tuple[int, str, bool, bool]] = []
        total_download_lines = 0
        successful_download_lines = 0

        last_comment = ""
        for idx, raw_line in enumerate(lines):
            parsed = parse_backlog_entry(raw_line, active_save_path, last_comment=last_comment)
            url, save_path, new_dir = parsed[0], parsed[1], parsed[2]
            entry_filename = getattr(parsed, "filename", "")
            entry_headers = getattr(parsed, "headers", {})

            if new_dir:
                active_save_path = new_dir
                entries_info.append((idx, raw_line, False, True))
                continue

            line_str = raw_line.strip()
            if line_str.startswith("#") or line_str.startswith("//"):
                last_comment = line_str
                entries_info.append((idx, raw_line, False, True))
                continue

            if not url:
                entries_info.append((idx, raw_line, False, True))
                continue

            # Clear last_comment after being consumed by a download line
            last_comment = ""

            total_download_lines += 1
            success = False
            try:
                existing = self._db.find_by_url(url)
                if existing:
                    if existing.status in ("queued", "paused", "error"):
                        self.resume_download(existing.id)
                    success = True
                else:
                    res = self.add_download(
                        url,
                        save_path=save_path,
                        filename=entry_filename,
                        headers=entry_headers,
                    )
                    if res:
                        count += 1
                        success = True
                    else:
                        success = False
            except Exception as exc:
                log.error("Failed to add download from backlog line '%s': %s", raw_line.strip(), exc)
                success = False

            if success:
                successful_download_lines += 1
            entries_info.append((idx, raw_line, True, success))

        log.info(
            "Backlog %s: processed %d/%d entries (newly added: %d)",
            filepath, successful_download_lines, total_download_lines, count,
        )

        # Clear processed entries if enabled
        if self._general_config.clear_backlog_after_load and total_download_lines > 0:
            try:
                if successful_download_lines == total_download_lines:
                    # All download entries processed successfully; empty the file
                    with open(filepath, "w", encoding="utf-8") as f:
                        pass
                    log.info("Backlog file cleared completely: %s", filepath)
                elif successful_download_lines > 0:
                    # Some download entries succeeded, some failed; retain failed lines and comments
                    remaining_lines = [
                        raw_line for _, raw_line, is_download, success in entries_info
                        if not (is_download and success)
                    ]
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.writelines(remaining_lines)
                    log.info("Backlog file updated: removed %d successful entries from %s", successful_download_lines, filepath)
            except Exception as exc:
                log.error("Failed to clear / update backlog file %s: %s", filepath, exc)

        return count

    def process_backlogs(self, extra_filepath: Optional[str] = None) -> int:
        """Scan configured locations (project root, user home, app dir) and extra_filepath,
        loading and auto-clearing any discovered backlog files. Returns total added downloads."""
        total_added = 0
        candidate_paths: list[Path] = []

        if extra_filepath:
            candidate_paths.append(Path(extra_filepath))

        for loc in self._general_config.get_effective_backlog_locations():
            p = Path(loc)
            if p.is_dir():
                candidate_paths.append(p / "backlog.txt")
            else:
                candidate_paths.append(p)

        processed_files: set[str] = set()
        for candidate in candidate_paths:
            try:
                if not candidate.is_file():
                    continue
                canonical = str(candidate.resolve()).lower()
                if canonical in processed_files:
                    continue
                processed_files.add(canonical)

                log.info("Processing backlog file: %s", candidate)
                count = self.load_backlog(str(candidate))
                total_added += count
            except Exception as exc:
                log.error("Error processing backlog file %s: %s", candidate, exc)

        return total_added

    # -- query ---------------------------------------------------------------

    def get_all_entries(self) -> list[DownloadEntry]:
        return self._db.get_all_downloads()

    def get_entry(self, download_id: str) -> Optional[DownloadEntry]:
        entry = self._db.get_download(download_id)
        if not entry:
            return None
        if entry.download_type == "torrent":
            status = self._torrent.get_status(download_id)
            if status:
                entry.total_size = status["total_size"] or entry.total_size
                if entry.status in ("completed", "seeding"):
                    if status["downloaded"] > 0:
                        entry.downloaded_size = max(status["downloaded"], entry.downloaded_size)
                    elif entry.total_size > 0:
                        entry.downloaded_size = entry.total_size
                else:
                    entry.downloaded_size = status["downloaded"]
                entry.speed = status["speed"]
                entry.upload_speed = status["upload_speed"]
                entry.seeds = to_int(status["seeds"])
                entry.peers = to_int(status["peers"])
                entry.total_seeds = to_int(status.get("total_seeds", status["seeds"]))
                entry.total_peers = to_int(status.get("total_peers", status["peers"]))
                entry.eta_seconds = status["eta"]
            elif entry.metadata:
                entry.seeds = to_int(entry.metadata.get("seeds", 0))
                entry.peers = to_int(entry.metadata.get("peers", 0))
                entry.total_seeds = to_int(entry.metadata.get("total_seeds", 0))
                entry.total_peers = to_int(entry.metadata.get("total_peers", 0))
        if entry.status in ("completed", "seeding") and entry.total_size > 0:
            if entry.downloaded_size < entry.total_size:
                entry.downloaded_size = entry.total_size
        return entry

    # -- engine callbacks (called from async / background threads) -----------

    def _on_http_progress(self, download_id: str, downloaded: int,
                          total: int, speed: float, eta: float):
        entry = self._db.get_download(download_id)
        if entry and entry.status in ("paused", "stopped", "suspended"):
            return
        self.progress_updated.emit(
            download_id, downloaded, total, speed, eta, 0, 0, 0.0
        )

    def _on_http_status(self, download_id: str, status: str,
                          error_msg: str):
        self._starting_downloads.discard(download_id)
        current = self._db.get_download(download_id)
        if current and current.status in ("paused", "stopped", "suspended") and status in ("queued", "downloading"):
            log.debug("Ignoring status %s for %s download %s", status, current.status, download_id)
            return
        if current and current.status != status and status in ("completed", "paused", "stopped", "error"):
            self._db.update_status(download_id, status)
        if (
            status == "completed"
            and self._security_config.scan_after_download
            and self._security_config.scan_timing == "after_complete"
        ):
            self._handle_completed_scan(download_id)
        else:
            self.status_changed.emit(download_id, status, error_msg)
        if status in ("completed", "paused", "stopped", "error"):
            self._process_queue()

    def _on_torrent_progress(self, download_id: str, downloaded: int,
                             total: int, speed: float, eta: float,
                             seeds: int, peers: int, upload_speed: float):
        entry = self._db.get_download(download_id)
        if entry and entry.status in ("paused", "stopped", "suspended"):
            return
        self.progress_updated.emit(
            download_id, downloaded, total, speed, eta,
            seeds, peers, upload_speed,
        )

    def _on_torrent_status(self, download_id: str, status: str,
                           error_msg: str):
        self._starting_downloads.discard(download_id)
        current = self._db.get_download(download_id)
        if current and current.status in ("paused", "stopped", "suspended") and status in ("queued", "downloading", "fetching_metadata"):
            log.debug("Ignoring status %s for %s torrent %s", status, current.status, download_id)
            return
        if (
            status in ("finished", "seeding")
            and self._security_config.scan_after_download
            and self._security_config.scan_timing == "after_complete"
        ):
            entry = self._db.get_download(download_id)
            if entry and not entry.metadata.get("antivirus_scanned"):
                self._handle_completed_scan(download_id, is_torrent=True)
                return
        self.status_changed.emit(download_id, status, error_msg)
        if status in ("completed", "seeding", "paused", "stopped", "error", "suspended"):
            self._process_queue()

    def _handle_completed_scan(self, download_id: str, is_torrent: bool = False):
        entry = self._db.get_download(download_id)
        target_seeding = bool(self._torrent_config and self._torrent_config.seeding_after_complete) if is_torrent else False
        if not entry or not entry.file_path:
            final_status = "seeding" if target_seeding else "completed"
            self.status_changed.emit(download_id, final_status, "")
            return

        self._db.update_status(download_id, "scanning")
        self.status_changed.emit(download_id, "scanning", "Scanning file for malware...")

        def _do_scan():
            try:
                is_clean, report = scan_file(entry.file_path, self._security_config)
                meta = entry.metadata
                meta["antivirus_scanned"] = True
                meta["antivirus_report"] = report

                if is_clean:
                    final_status = "seeding" if target_seeding else "completed"
                    entry.metadata = meta
                    self._db.update_download(entry)
                    self._db.update_status(download_id, final_status)
                    if final_status == "seeding":
                        h = self._torrent._handles.get(download_id)
                        if h:
                            self._torrent._apply_seeding_limit_to_handle(h)
                    self.status_changed.emit(download_id, final_status, report)
                else:
                    meta["threat_detected"] = True
                    if self._security_config.action_on_threat == "delete":
                        quarantine_or_delete_file(entry.file_path)
                        report += " (Infected file deleted)"
                    entry.metadata = meta
                    self._db.update_download(entry)
                    self._db.update_status(download_id, "threat_detected", report)
                    self.status_changed.emit(download_id, "threat_detected", report)
                    self.threat_detected.emit(download_id, report)
            except Exception as exc:
                log.debug("Antivirus scan background task error for %s: %s", download_id, exc)

        threading.Thread(
            target=_do_scan, daemon=True, name=f"scan-{download_id}"
        ).start()

    def scan_download_file(self, download_id: str):
        """Perform on-demand antivirus scan of a downloaded file."""
        entry = self._db.get_download(download_id)
        if not entry or not entry.file_path:
            return

        original_status = entry.status
        self._db.update_status(download_id, "scanning")
        self.status_changed.emit(download_id, "scanning", "Scanning file for malware...")

        def _do_scan():
            is_clean, report = scan_file(entry.file_path, self._security_config)
            meta = entry.metadata
            meta["antivirus_scanned"] = True
            meta["antivirus_report"] = report

            if is_clean:
                entry.metadata = meta
                self._db.update_download(entry)
                target_status = original_status if original_status in ("paused", "queued", "downloading", "seeding") else "completed"
                if entry.total_size > 0 and entry.downloaded_size < entry.total_size and target_status == "completed":
                    target_status = "paused"
                self._db.update_status(download_id, target_status)
                self.status_changed.emit(download_id, target_status, report)
            else:
                meta["threat_detected"] = True
                if self._security_config.action_on_threat == "delete":
                    quarantine_or_delete_file(entry.file_path)
                    report += " (Infected file deleted)"
                entry.metadata = meta
                self._db.update_download(entry)
                self._db.update_status(download_id, "threat_detected", report)
                self.status_changed.emit(download_id, "threat_detected", report)
                self.threat_detected.emit(download_id, report)

        threading.Thread(
            target=_do_scan, daemon=True, name=f"scan-ondemand-{download_id}"
        ).start()

    # -- inspection & details queries ---------------------------------------

    def get_download_files(self, download_id: str) -> list[dict]:
        """Return files for a download. For torrents, queries TorrentEngine (falling back to cached metadata in DB). For HTTP, returns single target."""
        entry = self._db.get_download(download_id)
        if not entry:
            return []
        if entry.download_type == "torrent":
            files = self._torrent.get_torrent_files(download_id)
            if files:
                if entry.metadata.get("files") != files:
                    entry.metadata["files"] = files
                    self._db.update_download(entry)
                return files
            cached = entry.metadata.get("files", [])
            return cached if isinstance(cached, list) else []
        # HTTP single file representation
        name = entry.filename or os.path.basename(entry.file_path) if entry.file_path else "file"
        pct = (entry.downloaded_size / entry.total_size) if entry.total_size > 0 else 0.0
        return [{
            "index": 0,
            "path": name,
            "size": entry.total_size,
            "downloaded": entry.downloaded_size,
            "progress": pct,
            "priority": 4,
            "priority_label": "Normal",
            "status": entry.status,
        }]

    def set_torrent_file_priority(self, download_id: str, file_index: int, priority: int) -> bool:
        """Update file download priority for a torrent."""
        return self._torrent.set_torrent_file_priority(download_id, file_index, priority)

    def get_torrent_peers(self, download_id: str) -> list[dict]:
        """Return active swarm peers for a torrent, falling back to cached metadata in DB."""
        entry = self._db.get_download(download_id)
        if not entry:
            return []
        peers = self._torrent.get_torrent_peers(download_id)
        if peers:
            cached = entry.metadata.get("peer_list")
            if not isinstance(cached, list) or len(cached) != len(peers) or [c.get("ip") for c in cached] != [p.get("ip") for p in peers]:
                entry.metadata["peer_list"] = peers
                self._db.update_download(entry)
            return peers
        cached = entry.metadata.get("peer_list")
        if isinstance(cached, list):
            return cached
        # Fallback to legacy "peers" key only if it was stored as a list of dicts
        legacy = entry.metadata.get("peers")
        if isinstance(legacy, list):
            return legacy
        return []

    def get_torrent_trackers(self, download_id: str) -> list[dict]:
        """Return trackers and their status for a torrent, falling back to cached metadata in DB."""
        entry = self._db.get_download(download_id)
        if not entry:
            return []
        trackers = self._torrent.get_torrent_trackers(download_id)
        if trackers:
            if entry.metadata.get("trackers") != trackers:
                entry.metadata["trackers"] = trackers
                self._db.update_download(entry)
            return trackers
        cached = entry.metadata.get("trackers", [])
        return cached if isinstance(cached, list) else []

    def get_download_segments(self, download_id: str) -> list[SegmentEntry]:
        """Return segmented download chunks, preferring live in-memory segments if actively downloading."""
        live = self._http.get_live_segments(download_id)
        if live:
            return list(live)
        return self._db.get_segments(download_id)

    def save_ui_state(self, state: dict):
        """Persist window geometry, position, maximized state, and column widths to database."""
        self._db.save_window_state(state)

    def get_ui_state(self) -> dict:
        """Retrieve persisted window geometry, position, maximized state, and column widths from database."""
        return self._db.get_window_state()

    def save_preferences_window_size(self, width: int, height: int):
        """Persist preferences dialog window dimensions to database."""
        self._db.save_preferences_window_size(width, height)

    def get_preferences_window_size(self) -> dict:
        """Retrieve persisted preferences dialog window dimensions from database."""
        return self._db.get_preferences_window_size()

    def _on_filename_resolved(self, download_id: str, filename: str):
        self.filename_resolved.emit(download_id, filename)

    # -- periodic callbacks --------------------------------------------------

    def _poll_torrents(self):
        self._torrent.poll_all()

    def _process_retry_queue(self):
        """Re-start any downloads that are queued for retry."""
        if (
            self._network_config
            and self._network_config.kill_switch
            and self._network_config.is_interface_bound
        ):
            if not is_interface_active(
                self._network_config.interface_name,
                self._network_config.interface_ip,
            ):
                log.debug("Skipping retry queue: VPN/interface is disconnected")
                return

        self._process_queue()

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _detect_type(url: str) -> str:
        url_lower = url.lower().strip()
        if url_lower.startswith("magnet:"):
            return "torrent"
        if url_lower.endswith(".torrent") and os.path.isfile(url):
            return "torrent"
        try:
            parsed = urlparse(url)
            if parsed.scheme in ("http", "https", "ftp"):
                path_lower = parsed.path.lower()
                if path_lower.endswith(".torrent") or ".torrent" in path_lower:
                    return "torrent"
                if ".torrent" in parsed.query.lower():
                    return "torrent"
        except Exception:
            pass
        return "http"
