"""Central download manager orchestrating engines, database, and GUI signals."""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import queue
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union, Any
from urllib.parse import parse_qs, unquote, urlparse

from PySide6.QtCore import QObject, Signal, QTimer, Slot

from my_idm.database import (
    ALL_QUEUES,
    DEFAULT_QUEUE_ID,
    DEFAULT_QUEUE_NAME,
    SOURCE_QUEUES,
    Database,
    DownloadEntry,
    QueueInfo,
    SegmentEntry,
    _now_iso,
)

#: source_key -> queue id, for the two seeded source queues. Built from the table so the ids
#: cannot drift between the seeder and the router.
_SOURCE_QUEUE_BY_KEY = {key: queue_id for queue_id, _name, key in SOURCE_QUEUES}
from my_idm.http_engine import HTTPEngine
from my_idm.config import (
    GeneralConfig,
    TorConfig,
    TorrentConfig,
    ExternalToolsConfig,
    BrowserIntegrationConfig,
    BandwidthLimitConfig,
    SchedulerConfig,
    is_within_schedule_window,
    is_tor_reachable,
    DEFAULT_DOWNLOADS_DIR,
)
from my_idm.notifications import notify_browser_download_caught
from my_idm.browser_server import BrowserServer
from my_idm.external_tools import launch_animepahe_cli, launch_animepahe_gui
from my_idm import youtube_tool
from my_idm.tor_service import TorServiceManager, find_tor_executable
from my_idm.network import NetworkConfig, is_interface_active
from my_idm.security import (
    SecurityConfig,
    check_url_safety,
    scan_file,
    quarantine_or_delete_file,
)
from my_idm.torrent_engine import TorrentEngine
from my_idm.torrent_sources import (
    TorrentFolderWatcher,
    canonical_torrent_path,
    is_torrent_path,
)
from my_idm.utils import get_unique_filename, normalize_path, robust_move_download_files, send_to_trash, to_int, unlock_path, split_extension

log = logging.getLogger(__name__)

DEFAULT_SAVE_PATH = DEFAULT_DOWNLOADS_DIR


def _log_coro_failure(future) -> None:
    """Report a background coroutine that ended in an exception.

    ``run_coro_threadsafe`` hands the work to a caller that has already returned, so nothing
    would ever re-raise this. Consuming the exception here also stops asyncio logging the same
    failure a second time as an unretrieved future.
    """
    try:
        exc = future.exception()
    except (asyncio.CancelledError, Exception):
        return
    if exc is not None:
        log.warning("Background coroutine failed: %s", exc)


def _pick_format(metadata: Any, format_selector: str):
    """Resolve a format selector against extracted metadata.

    Accepts a concrete format_id, a yt-dlp selector expression, or an empty
    string. Returns the matching format object when it can be identified.
    """
    if metadata is None:
        return None
    formats = getattr(metadata, "formats", None) or []
    if not formats:
        return None

    selector = (format_selector or "").strip()
    if not selector:
        return None

    for fmt in formats:
        if getattr(fmt, "format_id", "") == selector:
            return fmt

    if "+" in selector or "/" in selector or "[" in selector:
        for part in selector.replace("+", "/").split("/"):
            candidate = part.split("[")[0].strip()
            if not candidate or candidate == "best":
                continue
            for fmt in formats:
                if getattr(fmt, "format_id", "") == candidate:
                    return fmt
    return None


def _resolve_format_id(metadata: Any, format_selector: str) -> str:
    """Return a concrete format_id usable for Mode A, or an empty string.

    Mode A needs a single self-contained file, so video-only formats (which
    require an ffmpeg merge) are rejected.
    """
    fmt = _pick_format(metadata, format_selector)
    if fmt is not None and getattr(fmt, "direct_capable", False) and not getattr(fmt, "is_video_only", False):
        return getattr(fmt, "format_id", "")
    return ""


def _can_use_mode_a(metadata: Any, format_selector: str) -> bool:
    """True when the selected format can be downloaded without an ffmpeg merge."""
    return bool(_resolve_format_id(metadata, format_selector))


def _expected_total_size(metadata: Any, format_selector: str) -> int:
    """Best-effort total byte size for a selector, used before yt-dlp reports it.

    yt-dlp's progress hook often omits ``total_bytes`` while fragments download,
    so a pre-computed estimate keeps the progress bar meaningful.
    """
    if metadata is None:
        return 0
    formats = getattr(metadata, "formats", None) or []
    if not formats:
        return 0

    wanted = [p.strip() for p in (format_selector or "").replace("+", "/").split("/")]
    wanted = [p.split("[")[0].strip() for p in wanted if p and p not in ("best", "worst")]

    if not wanted:
        return 0

    total = 0
    matched = 0
    for fmt in formats:
        if fmt.format_id in wanted and fmt.filesize:
            total += fmt.filesize
            matched += 1

    if matched == len(wanted):
        return total
    if matched == 1:
        return total
    return 0


class ParsedBacklogEntry(tuple):
    """Backwards-compatible 3-tuple (url, save_path, new_dir) with extra attributes."""
    def __new__(cls, url: Optional[str], save_path: str, new_dir: Optional[str],
                filename: str = "", headers: Optional[dict[str, str]] = None,
                queue: str = "", queue_directive: str = ""):
        return super().__new__(cls, (url, save_path, new_dir))

    def __init__(self, url: Optional[str], save_path: str, new_dir: Optional[str],
                 filename: str = "", headers: Optional[dict[str, str]] = None,
                 queue: str = "", queue_directive: str = ""):
        self.url = url
        self.save_path = save_path
        self.new_dir = new_dir
        self.filename = filename
        self.headers = headers or {}
        #: Queue named on *this* download line, overriding the sticky directive.
        self.queue = queue
        #: Queue named by a sticky ``queue:`` directive on this line. Empty when the line is not
        #: a directive. Kept separate from ``queue`` so the caller can tell "this line set the
        #: queue for what follows" from "this line's download goes to a queue".
        self.queue_directive = queue_directive


def parse_backlog_entry(
    raw_line: str,
    active_save_path: str = "",
    last_comment: str = "",
    active_queue: str = "",
) -> ParsedBacklogEntry:
    """Parse a single backlog file line.

    Returns a backwards-compatible 3-tuple (url, save_path, new_dir) with
    .filename, .headers, .queue and .queue_directive attributes:
    - If line is empty or comment: (None, "", None).
    - If line sets directory directive (# dir: ..., dir=..., [path]): (None, "", directive_path).
    - If line sets a queue directive (# queue: ..., queue=...): ``queue_directive`` is the name.
    - If line contains a download URL/magnet: (url, save_path, None, filename, headers, queue).

    ``queue=`` on a download line names that line's queue; a ``queue:`` directive on its own line
    applies to every download after it, the same way ``dir:`` does. A per-line value wins over
    the sticky one. An unknown name is *not* created here - :meth:`queue_id_for_name` decides.
    """
    line = raw_line.strip()
    if not line:
        return ParsedBacklogEntry(None, "", None)

    # Queue directive, checked before the directory directive because both use the same
    # "name: value" shape. Returns no new_dir: a queue is not a path.
    m_comment_queue = re.match(r'^#+\s*queue\s*[:=]\s*(.+)$', line, re.IGNORECASE)
    if m_comment_queue:
        return ParsedBacklogEntry(
            None, "", None,
            queue_directive=m_comment_queue.group(1).strip().strip('"\''),
        )

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
    m_queue = re.match(r'^queue\s*[:=]\s*(.+)$', line, re.IGNORECASE)
    if m_queue:
        return ParsedBacklogEntry(
            None, "", None, queue_directive=m_queue.group(1).strip().strip('"\''),
        )

    m_dir = re.match(r'^(?:dir|save_path|path)\s*[:=]\s*(.+)$', line, re.IGNORECASE)
    if m_dir:
        p = m_dir.group(1).strip().strip('"\'')
        p = os.path.expandvars(os.path.expanduser(p))
        return ParsedBacklogEntry(None, "", normalize_path(p))

    url = ""
    save_path = ""
    filename = ""
    queue = ""
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
                elif k_clean == "queue":
                    queue = v_clean
                elif k_clean in ("referer", "referrer"):
                    headers["Referer"] = v_clean
                elif k_clean.startswith("header"):
                    # e.g. header=Name: Value
                    if ":" in v_clean:
                        hn, _, hv = v_clean.partition(":")
                        headers[hn.strip()] = hv.strip()
                elif k_clean in ("anime_url", "anime_title"):
                    # Written by animepahe-downloader/modules/my_idm.py as trailing
                    # key=value columns so My-IDM can surface them in the details panel.
                    # Without this branch they silently fall through and the panel
                    # shows "—" even for freshly queued downloads.
                    headers[k_clean] = v_clean
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

        # aria2-style queue="Name". Taken before the space-splitting fallback so a queue name
        # containing a space cannot swallow the rest of the URL.
        m_queue_opt = re.search(r'\bqueue\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|(\S+))', line, re.IGNORECASE)
        if m_queue_opt:
            queue = (m_queue_opt.group(1) or m_queue_opt.group(2)
                     or m_queue_opt.group(3)).strip()
            line = (line[:m_queue_opt.start()] + " " + line[m_queue_opt.end():]).strip()

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
    if url and any(cdn in url for cdn in ("owocdn.top", "uwucdn.top", "kwik.")):
        if "Referer" not in headers:
            headers["Referer"] = "https://kwik.cx/"

    if queue:
        queue = queue.strip().strip('"\'')

    return ParsedBacklogEntry(
        url, save_path, None, filename=filename, headers=headers,
        queue=queue or active_queue,
    )

class StatusJobPool:
    """Worker pool for serializing and pacing status operations (resume, recheck, pause, etc.).

    Prevents UI lockups during bulk operations (such as resuming or rechecking 25 downloads)
    and ensures state changes settle sequentially without thrashing concurrency limits.
    """

    def __init__(self, manager: "DownloadManager", pace_seconds: float = 0.01):
        self._manager = manager
        self._pace_seconds = pace_seconds
        self._queue: queue.Queue = queue.Queue()
        self._stopped = False
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="status-job-pool",
            daemon=True,
        )
        self._worker.start()

    def submit(self, action: str, download_id: str, *args, **kwargs):
        """Submit a single status operation to the pool."""
        if not self._stopped:
            self._queue.put((action, download_id, args, kwargs))

    def submit_batch(self, action: str, download_ids: list[str]):
        """Submit multiple downloads for the given action in FIFO order."""
        for did in download_ids:
            self.submit(action, did)

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Wait until all pending jobs have been executed."""
        start = time.time()
        while not self._queue.empty():
            if time.time() - start > timeout:
                return False
            time.sleep(0.01)
        self._queue.join()
        return True

    def stop(self, timeout: float = 2.0):
        """Stop the worker thread."""
        self._stopped = True
        if self._worker.is_alive():
            self._worker.join(timeout=timeout)

    def _worker_loop(self):
        while not self._stopped:
            try:
                action, download_id, args, kwargs = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                self._dispatch(action, download_id, *args, **kwargs)
            except Exception as exc:
                log.error("StatusJobPool error performing %s on %s: %s", action, download_id, exc)
            finally:
                self._queue.task_done()

            if self._pace_seconds > 0:
                time.sleep(self._pace_seconds)

            # When queue drains, run queue processor once to start any newly eligible downloads
            if self._queue.empty() and not self._stopped:
                try:
                    self._manager._process_queue()
                except Exception as exc:
                    log.error("StatusJobPool _process_queue error: %s", exc)

    def _dispatch(self, action: str, download_id: str, *args, **kwargs):
        if action == "resume":
            self._manager.resume_download(download_id, *args, **kwargs)
        elif action == "recheck":
            self._manager.recheck_download(download_id, *args, **kwargs)
        elif action == "pause":
            self._manager.pause_download(download_id, *args, **kwargs)
        elif action == "stop":
            self._manager.stop_download(download_id, *args, **kwargs)
        elif action == "force_start":
            self._manager.force_start_download(download_id, *args, **kwargs)
        else:
            log.warning("Unknown status job action: %s", action)


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
    download_url_updated = Signal(str, str)  # download_id, new_url
    general_config_changed = Signal(object)   # GeneralConfig
    torrent_config_changed = Signal(object)   # TorrentConfig
    network_config_changed = Signal(object)  # NetworkConfig
    security_config_changed = Signal(object)  # SecurityConfig
    tor_config_changed = Signal(object)       # TorConfig
    threat_detected = Signal(str, str)       # download_id, report
    queue_order_changed = Signal()
    queues_changed = Signal()              # the queue list, its limits or its membership changed
    queue_scope_changed = Signal(str)      # the queue the downloads list is scoped to ("" = all)
    tor_status_changed = Signal(str, str)     # status ("connecting"|"connected"|"disconnecting"|"disconnected"|"error"), message
    tor_availability_changed = Signal(bool)  # a Tor SOCKS5 endpoint is reachable
    _tor_probe_result = Signal(bool)          # private: emitted from the probe thread
    bandwidth_limits_changed = Signal(int, int)  # download_limit, upload_limit
    external_tools_config_changed = Signal(object)  # ExternalToolsConfig
    animepahe_status_changed = Signal(bool)  # is_running
    animepahe_queue_changed = Signal(int)    # queue_size
    browser_config_changed = Signal(object)  # BrowserIntegrationConfig
    process_backlogs_requested = Signal()    # Request to run process_backlogs on main thread
    youtube_error = Signal(str, str)         # source_url, error_message
    bandwidth_warning = Signal(str, str, float, bool)    # queue_id, message, percentage, is_global
    bandwidth_limit_exceeded = Signal(str, str, float, bool)  # queue_id, message, percentage, is_global
    bandwidth_warning_cleared = Signal()
    scheduler_config_changed = Signal(object)  # SchedulerConfig
    #: (paths) — local .torrent files the watched folder yielded, for the UI to report.
    torrent_folder_captured = Signal(list)

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
        self._scheduler_config = SchedulerConfig.load()
        self._was_within_schedule: Optional[bool] = None
        self._browser_server = BrowserServer(self, self._browser_config)
        self._animepahe_process: Optional[subprocess.Popen] = None
        self._animepahe_queue: list[dict[str, Any]] = []
        self._animepahe_queue_lock = threading.RLock()
        self._browser_container_hwnd: Optional[int] = None
        self._window_visible: bool = True
        # Enforce that Tor is only enabled on startup if auto_start_at_startup is True
        if not self._tor_config.auto_start_at_startup:
            self._tor_config.enabled = False
        self._tor_service = TorServiceManager(self._tor_config)
        # Tor availability is probed on a background thread and cached. Probing
        # inline blocks for the socket timeout (1 s on Windows), which froze the
        # GUI on every context-menu open and on every row repaint.
        self._tor_available_cache: bool = False
        self._tor_probe_in_flight = False
        self._tor_probe_result.connect(self._on_tor_probe_result)
        self._stopped = False
        self._starting_downloads: set[str] = set()
        self._last_progress_bytes: dict[str, int] = {}
        self._ytdlp_jobs: dict[str, dict[str, Any]] = {}
        self._ytdlp_lock = threading.RLock()
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
        # Seed the engines with the queues' own ceilings before the first download starts, so a
        # limited queue is limited from its very first chunk rather than from the first edit.
        self._push_queue_limits()

        # asyncio event loop runs in a background thread
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

        # QTimer.stop() cannot cancel a callback that is already executing, so a
        # poll that is mid-flight when stop() runs can still reach the database
        # afterwards (this is what made DownloadManager::stop() race a caller
        # that tears the database down). Every timer slot therefore runs inside
        # _timer_slot(), and stop() waits for the in-flight ones to finish.
        self._timer_slots_cond = threading.Condition()
        self._timer_slots_inflight = 0

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

        # Watched-folder scanner for .torrent files. Armed by _apply_torrent_watch_config, which
        # is the only thing that should ever start it — same invariant as the backlog timer, so a
        # manager that is constructed but never started cannot scan the user's disk.
        self._torrent_watcher = TorrentFolderWatcher(self)
        self._torrent_watcher.torrents_found.connect(self._on_watched_folder_torrents)
        #: Paths the watcher has already offered this session. An optimisation only:
        #: `add_torrent_files(only_new=True)` re-checks the database, which is what actually
        #: stops a paused torrent being restarted.
        self._torrent_watch_seen: set[str] = set()
        self._apply_torrent_watch_config()

        # AnimePahe periodic scraper timer
        self._animepahe_timer = QTimer(self)
        self._animepahe_timer.timeout.connect(self._on_animepahe_timer_tick)
        self._apply_animepahe_timer_config()

        # Completed downloads verification timer — periodically checks completed files on disk
        self._verify_completed_timer = QTimer(self)
        self._verify_completed_timer.setInterval(60_000)  # 60 seconds
        self._verify_completed_timer.timeout.connect(self._on_verify_completed_timer_tick)

        # Bandwidth limit check timer — periodically checks bandwidth usage against limits
        self._bandwidth_timer = QTimer(self)
        self._bandwidth_timer.setInterval(5_000)  # 5 seconds
        self._bandwidth_timer.timeout.connect(self._check_bandwidth_limits)

        # Scheduler check timer — checks off-peak window transitions
        self._scheduler_timer = QTimer(self)
        self._scheduler_timer.setInterval(2_000)  # 2 seconds
        self._scheduler_timer.timeout.connect(self._check_scheduler)

        # Connect process_backlogs_requested signal to run process_backlogs on main thread
        self.process_backlogs_requested.connect(self.process_backlogs)

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

        # Status operations worker pool
        self._status_job_pool = StatusJobPool(self)

    # -- lifecycle -----------------------------------------------------------

    @contextmanager
    def _timer_slot(self):
        """Mark a periodic Qt callback as in-flight so ``stop()`` can wait for it.

        ``QTimer.stop()`` only stops *future* emissions: a slot that is already
        running keeps going, and a poll that has reached ``Database.update_status``
        can write after ``stop()`` has returned. The counter is incremented and
        decremented under a short-lived lock (never held across the body) so
        ``stop()`` can block until the last in-flight slot has returned without
        ever deadlocking against the slot itself.
        """
        with self._timer_slots_cond:
            self._timer_slots_inflight += 1
        try:
            yield
        finally:
            with self._timer_slots_cond:
                self._timer_slots_inflight -= 1
                if self._timer_slots_inflight <= 0:
                    self._timer_slots_cond.notify_all()

    def _await_timer_slots(self, timeout: float = 5.0) -> bool:
        """Block until no timer slot is in flight, or *timeout* elapses.

        Returns True when the manager is quiescent. Almost always instantaneous,
        because timer slots and ``stop()`` both live on the Qt thread; it only
        blocks when ``stop()`` is driven from another thread while a poll is
        already running.
        """
        deadline = time.monotonic() + timeout
        with self._timer_slots_cond:
            while self._timer_slots_inflight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.warning(
                        "DownloadManager.stop(): %d timer callback(s) still running "
                        "after %.0fs", self._timer_slots_inflight, timeout,
                    )
                    return False
                self._timer_slots_cond.wait(remaining)
        return True

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

        # The asyncio thread is alive from here on, which is the condition the watcher has been
        # waiting for. Re-applied rather than started directly so there is one place that decides
        # whether it should be running.
        self._apply_torrent_watch_config()

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
        # resume_download enforces the per-queue budget, so walking the list in priority order
        # means each queue fills to its own limit and the overflow stays queued for the 1 Hz
        # tick rather than being started here and then blocked.
        for entry in all_entries:
            if entry.status == "queued":
                log.info("Auto-starting queued download on startup: %s (order=%s, queue=%s)",
                         entry.id, entry.queue_order, entry.queue_id)
                self.resume_download(entry.id)
            elif self._general_config.auto_resume_startup and entry.status in ("downloading", "checking", "fetching_metadata"):
                log.info("Auto-resuming interrupted download on startup: %s (order=%s, queue=%s)",
                         entry.id, entry.queue_order, entry.queue_id)
                self.resume_download(entry.id)
            elif entry.status == "seeding" and getattr(self._torrent_config, "resume_seeding_on_startup", True):
                log.info("Auto-resuming seeding torrent on startup: %s (order=%s, queue=%s)",
                         entry.id, entry.queue_order, entry.queue_id)
                self._torrent.add_torrent(entry)

        # Launch external tools (e.g. AnimePahe scraper) if configured
        if self._external_tools_config.animepahe_launch_on_startup:
            self.start_animepahe_scraper()

        # Start periodic AnimePahe scraper if enabled
        self._apply_animepahe_timer_config()

        # Verify completed downloads at startup and arm periodic verification
        self.verify_completed_downloads()
        self._verify_completed_timer.start()

        # Start bandwidth limit check timer
        self._bandwidth_timer.start()

        # Start scheduler check timer
        self._scheduler_timer.start()

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
        self._verify_completed_timer.stop()
        self._bandwidth_timer.stop()
        self._scheduler_timer.stop()
        # Before the drain below, like the other timers: the watcher's own scan adds rows through
        # the database, so it must be finished before the caller can close it.
        self._torrent_watcher.stop()
        self._status_job_pool.stop()

        # A timer callback that was already executing survives .stop(), so a
        # poll can still be mid-write when this method returns. Join it before
        # the caller tears anything down (the database, in particular).
        self._await_timer_slots(timeout=5.0)

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

        # Signal any running yt-dlp (Mode B) workers to abort
        with self._ytdlp_lock:
            jobs = list(self._ytdlp_jobs.values())
        for job in jobs:
            holder = job.get("cancel_holder")
            if holder is not None:
                holder["cancel"] = True
            event = job.get("cancel_event")
            if event is not None:
                event.set()
        if jobs:
            log.info("Signalled %d yt-dlp job(s) to abort", len(jobs))

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

    # -- YouTube / yt-dlp integration ----------------------------------------

    def is_youtube_download(self, download_id: str) -> bool:
        """True when the entry originated from a YouTube / yt-dlp download."""
        entry = self._db.get_download(download_id)
        if not entry or not entry.metadata:
            return False
        return str(entry.metadata.get("source_type", "")).startswith("youtube")

    def is_ytdlp_native_job(self, download_id: str) -> bool:
        """True when a live yt-dlp (Mode B) worker thread owns this download."""
        with self._ytdlp_lock:
            return download_id in self._ytdlp_jobs

    def _is_ytdlp_native_entry(self, entry: DownloadEntry) -> bool:
        """True when *entry* was created for a yt-dlp native (Mode B) download."""
        if not entry or not entry.metadata:
            return False
        return entry.metadata.get("source_type") == "youtube_native"

    def _cancel_ytdlp_job(self, download_id: str) -> bool:
        """Signal a running Mode B worker to abort. Returns True if one was signalled."""
        with self._ytdlp_lock:
            job = self._ytdlp_jobs.get(download_id)
            if not job:
                return False
            holder = job.get("cancel_holder")
            if holder is not None:
                holder["cancel"] = True
            event = job.get("cancel_event")
            if event is not None:
                event.set()
            return True

    def _drop_ytdlp_job(self, download_id: str) -> None:
        with self._ytdlp_lock:
            self._ytdlp_jobs.pop(download_id, None)

    def add_youtube_download(
        self,
        url: str,
        metadata: Any = None,
        save_path: str = "",
        format_selector: str = "",
        mode: str = "auto",
        filename: str = "",
    ) -> Optional[str]:
        """Queue a YouTube download using Mode A (HTTPEngine) or Mode B (yt-dlp).

        *mode* is ``"auto"``, ``"a"`` or ``"b"``. ``auto`` prefers Mode B because
        YouTube serves video and audio as separate streams that must be merged by
        ffmpeg; Mode A can only handle single-file (audio-only) formats.
        """
        url = (url or "").strip()
        if not url:
            return None

        cfg = self._external_tools_config
        if not cfg.ytdlp_enabled:
            log.info("YouTube integration disabled in configuration; ignoring %s", url)
            return None

        title = getattr(metadata, "title", "") or ""
        webpage_url = getattr(metadata, "webpage_url", "") or url

        chosen_mode = (mode or "auto").lower()
        if chosen_mode == "auto":
            chosen_mode = "a" if cfg.ytdlp_prefer_mode_a and _can_use_mode_a(metadata, format_selector) else "b"

        if chosen_mode == "a":
            return self._add_youtube_mode_a(url, metadata, save_path, format_selector, filename, title, webpage_url)
        return self._add_youtube_mode_b(url, metadata, save_path, format_selector, filename, title, webpage_url)

    def _add_youtube_mode_a(
        self, url: str, metadata: Any, save_path: str,
        format_selector: str, filename: str, title: str, webpage_url: str,
    ) -> Optional[str]:
        """Resolve a direct CDN URL and hand it to the standard HTTP pipeline."""
        from my_idm import youtube_tool as ytt

        fmt_id = _resolve_format_id(metadata, format_selector)
        if not fmt_id:
            log.warning("No direct-capable YouTube format selected for %s", url)
            return None

        try:
            direct = ytt.resolve_direct_url(url, fmt_id, self._external_tools_config, title)
        except ytt.YouTubeToolError as exc:
            log.warning("YouTube Mode A resolve failed for %s: %s", url, exc)
            self._emit_youtube_error(url, str(exc))
            return None

        entry_metadata: dict[str, Any] = {
            "source_type": "youtube",
            "source_url": url,
            "original_youtube_url": url,
            "webpage_url": webpage_url,
            "youtube_format_id": direct.format_id,
            "youtube_mode": "a",
        }
        if metadata is not None:
            if getattr(metadata, "id", ""):
                entry_metadata["video_id"] = metadata.id
            if getattr(metadata, "uploader", ""):
                entry_metadata["uploader"] = metadata.uploader
            if getattr(metadata, "thumbnail", ""):
                entry_metadata["thumbnail"] = metadata.thumbnail
            if getattr(metadata, "duration", None):
                entry_metadata["duration"] = metadata.duration
        if direct.expires_at:
            entry_metadata["youtube_url_expires_at"] = direct.expires_at

        return self.add_download(
            url=direct.direct_url,
            save_path=save_path or self._general_config.get_effective_save_path(),
            filename=filename or direct.filename,
            headers=direct.http_headers or None,
            metadata=entry_metadata,
        )

    def _add_youtube_mode_b(
        self, url: str, metadata: Any, save_path: str,
        format_selector: str, filename: str, title: str, webpage_url: str,
    ) -> Optional[str]:
        """Create an entry and run the download inside yt-dlp with ffmpeg merging."""
        from my_idm import youtube_tool as ytt

        cfg = self._external_tools_config
        if not ytt.check_ytdlp_available(cfg):
            self._emit_youtube_error(url, ytt.YTDLP_INSTALL_HINT)
            return None

        resolved_path = save_path or cfg.ytdlp_last_save_path or self._general_config.get_effective_save_path()
        resolved_path = normalize_path(resolved_path)

        title = title or (getattr(metadata, "title", "") if metadata else "") or "video"
        stem = ytt.build_stem(title)
        if not filename:
            fmt = _pick_format(metadata, format_selector)
            ext = (fmt.ext if fmt else "") or "mp4"
            filename = f"{stem}.{ext}"
        else:
            filename = sanitize_filename(filename)
        filename = filename.strip()

        download_id = str(uuid.uuid4())
        entry_metadata: dict[str, Any] = {
            "source_type": "youtube_native",
            "source_url": url,
            "original_youtube_url": url,
            "webpage_url": webpage_url,
            "youtube_mode": "b",
            "youtube_format": format_selector or cfg.ytdlp_default_format,
        }
        if metadata is not None:
            if getattr(metadata, "id", ""):
                entry_metadata["video_id"] = metadata.id
            if getattr(metadata, "uploader", ""):
                entry_metadata["uploader"] = metadata.uploader
            if getattr(metadata, "thumbnail", ""):
                entry_metadata["thumbnail"] = metadata.thumbnail
            if getattr(metadata, "duration", None):
                entry_metadata["duration"] = metadata.duration

        expected = _expected_total_size(metadata, format_selector or cfg.ytdlp_default_format)
        if expected:
            entry_metadata["youtube_expected_size"] = expected
        entry_metadata["youtube_stem"] = stem
        entry_metadata["title"] = title

        try:
            Path(resolved_path).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("Cannot create YouTube save directory %s: %s", resolved_path, exc)
            return None

        entry = DownloadEntry(
            id=download_id,
            url=url,
            save_path=resolved_path,
            filename=filename,
            file_path=str(Path(resolved_path) / filename),
            download_type="http",
            num_segments=1,
            added_at=_now_iso(),
            status="queued",
            total_size=expected or 0,
        )
        entry.metadata = entry_metadata
        self._db.add_download(entry)
        self.download_added.emit(entry.id)

        self._start_ytdlp_native_job(entry, format_selector, resolved_path)
        return entry.id

    def _start_ytdlp_native_job(self, entry: DownloadEntry, format_selector: str, save_dir: str):
        """Launch a yt-dlp worker thread for *entry* and wire its callbacks."""
        from my_idm import youtube_tool as ytt

        download_id = entry.id
        if self.is_ytdlp_native_job(download_id):
            log.info("yt-dlp job already running for %s; not starting another", download_id)
            return

        cfg = self._external_tools_config
        cancel_event = threading.Event()

        # Control the output name so the file yt-dlp writes matches the name
        # recorded in the database (titles often contain path separators).
        stem = entry.metadata.get("youtube_stem") or ""
        if not stem:
            stem, _ext = split_extension(entry.filename or "video")
        stem = ytt.build_stem(stem)
        outtmpl = str(Path(save_dir) / f"{stem}.%(ext)s")
        merge_format = "mp4" if ytt.check_ffmpeg_available(cfg) else ""
        try:
            expected_total = int(entry.metadata.get("youtube_expected_size") or 0)
        except (AttributeError, TypeError, ValueError):
            expected_total = 0

        def _on_progress(status: dict):
            self._on_ytdlp_progress(download_id, status)

        def _on_postprocess(status: dict):
            if status.get("status") == "finished" and status.get("postprocessor"):
                log.debug("yt-dlp postprocessor %s finished for %s", status.get("postprocessor"), download_id)

        try:
            thread, cancel_holder = ytt.start_native_download(
                url=entry.url,
                save_dir=save_dir,
                format_selector=format_selector or cfg.ytdlp_default_format,
                config=cfg,
                progress_cb=_on_progress,
                postprocessor_cb=_on_postprocess,
                done_cb=lambda p: self._on_ytdlp_done(download_id, p),
                error_cb=lambda m: self._on_ytdlp_error(download_id, m),
                cancel_event=cancel_event,
                outtmpl=outtmpl,
                merge_output_format=merge_format,
                expected_total=expected_total,
            )
        except ytt.YouTubeToolError as exc:
            self._on_ytdlp_error(download_id, str(exc))
            return

        with self._ytdlp_lock:
            self._ytdlp_jobs[download_id] = {
                "thread": thread,
                "cancel_holder": cancel_holder,
                "cancel_event": cancel_event,
            }

        self._db.update_status(download_id, "downloading")
        self.status_changed.emit(download_id, "downloading", "")

    def _on_ytdlp_progress(self, download_id: str, status: dict):
        """Relay a yt-dlp progress hook into the download table."""
        entry = self._db.get_download(download_id)
        if entry is None:
            # The entry was deleted while the worker was still finishing; the
            # hook can still fire briefly, and it must not touch the DB.
            return
        if entry.status in ("paused", "stopped", "completed", "cancelled", "file_not_found"):
            return

        state = status.get("status")
        downloaded = int(status.get("downloaded_bytes") or 0)
        total = int(status.get("total_bytes") or status.get("total_bytes_estimate") or 0)
        speed = float(status.get("speed") or 0.0)
        eta = float(status.get("eta") or 0.0)

        if not total:
            try:
                total = int(entry.metadata.get("youtube_expected_size") or 0)
            except (AttributeError, TypeError, ValueError):
                total = 0
            if not total:
                total = entry.total_size or 0

        if state == "downloading" and downloaded > 0:
            self._db.update_progress(download_id, downloaded)
            if total > 0 and entry.total_size != total:
                entry.total_size = total
        elif state == "finished":
            if downloaded <= 0:
                downloaded = entry.downloaded_size
            self._db.update_progress(download_id, downloaded)
            speed = 0.0

        self.progress_updated.emit(download_id, downloaded, total, speed, eta, 0, 0, 0.0)

    def _on_ytdlp_done(self, download_id: str, final_path: str):
        """Mark a Mode B download complete and record the produced file."""
        from my_idm import youtube_tool as ytt

        self._drop_ytdlp_job(download_id)
        entry = self._db.get_download(download_id)
        if not entry:
            return
        if entry.status in ("paused", "stopped", "cancelled"):
            log.debug("yt-dlp finished but entry is %s; not marking complete", entry.status)
            return

        produced = Path(final_path) if final_path and Path(final_path).is_file() else None
        if produced is None:
            stem = entry.metadata.get("youtube_stem") or ""
            if not stem:
                stem, _ext = split_extension(entry.filename or "")
            found = ytt.resolve_produced_file(entry.save_path, ytt.build_stem(stem))
            if found:
                produced = Path(found)

        if produced is not None:
            entry.filename = produced.name
            entry.file_path = str(produced)
            entry.save_path = str(produced.parent)
            try:
                entry.total_size = produced.stat().st_size
            except OSError:
                pass
            entry.downloaded_size = entry.total_size
        elif entry.total_size <= 0:
            entry.downloaded_size = entry.total_size = max(entry.downloaded_size, 0)

        self._db.update_download(entry)
        self._db.update_status(download_id, "completed")
        self.status_changed.emit(download_id, "completed", "")
        self.progress_updated.emit(
            download_id, entry.downloaded_size, entry.total_size, 0.0, 0.0, 0, 0, 0.0
        )
        if self._security_config.scan_after_download and self._security_config.scan_timing == "after_complete":
            self._handle_completed_scan(download_id)
        self._process_queue()

    def _on_ytdlp_error(self, download_id: str, message: str):
        """Mark a Mode B download as errored (or paused when user-cancelled)."""
        self._drop_ytdlp_job(download_id)
        entry = self._db.get_download(download_id)
        if not entry or entry.status in ("paused", "stopped", "cancelled", "completed"):
            return

        if "cancelled" in (message or "").lower():
            self._db.update_status(download_id, "paused")
            self.status_changed.emit(download_id, "paused", "")
        else:
            self._db.update_status(download_id, "error", message)
            self.status_changed.emit(download_id, "error", message)
        self.progress_updated.emit(
            download_id, entry.downloaded_size, entry.total_size, 0.0, 0.0, 0, 0, 0.0
        )
        self._process_queue()

    def _emit_youtube_error(self, url: str, message: str):
        """Surface a pre-download YouTube failure to the UI."""
        log.warning("YouTube download rejected: %s (%s)", url, message)
        self.youtube_error.emit(url, message)

    def _refresh_youtube_url(self, entry: DownloadEntry) -> bool:
        """Re-resolve an expired YouTube CDN URL before resuming (Mode A).

        Returns True when the entry URL was refreshed.
        """
        from my_idm import youtube_tool as ytt

        source_url = entry.metadata.get("original_youtube_url") or entry.metadata.get("source_url")
        format_id = entry.metadata.get("youtube_format_id")
        if not source_url or not format_id:
            return False

        try:
            direct = ytt.resolve_direct_url(
                source_url, format_id, self._external_tools_config, entry.metadata.get("title", "")
            )
        except ytt.YouTubeToolError as exc:
            log.warning("Could not refresh YouTube URL for %s: %s", entry.id, exc)
            return False

        entry.url = direct.direct_url
        if direct.http_headers:
            entry.metadata["headers"] = direct.http_headers
        if direct.expires_at:
            entry.metadata["youtube_url_expires_at"] = direct.expires_at
        self._db.update_download(entry)
        log.info("Refreshed YouTube CDN URL for %s (format %s)", entry.id, format_id)
        return True

    def _maybe_refresh_youtube_url(self, entry: DownloadEntry) -> bool:
        """Refresh the CDN URL when it is close to expiring (Mode A only)."""
        if entry.metadata.get("youtube_mode") != "a":
            return False
        expires_at = entry.metadata.get("youtube_url_expires_at")
        if not expires_at:
            return True
        try:
            remaining = float(expires_at) - time.time()
        except (TypeError, ValueError):
            return True
        if remaining > 300:
            return False
        log.info("YouTube URL for %s expires in %.0fs; re-resolving", entry.id, max(remaining, 0))
        return self._refresh_youtube_url(entry)

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
        with self._timer_slot():
            self._on_animepahe_timer_tick_body()

    def _on_animepahe_timer_tick_body(self):
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
        title: Optional[str] = None,
    ) -> tuple[bool, str]:
        """Launch the AnimePahe scraper in CLI mode in the background, or queue if already running."""
        with self._animepahe_queue_lock:
            if self._animepahe_process and self._animepahe_process.poll() is None:
                task = {
                    "url": url,
                    "episodes": episodes,
                    "quality": quality,
                    "lang": lang,
                    "title": title,
                }
                self._animepahe_queue.append(task)
                q_pos = len(self._animepahe_queue)
                self.animepahe_queue_changed.emit(q_pos)
                target_str = f" for '{title or url}'" if (title or url) else ""
                return True, f"AnimePahe task queued at position #{q_pos}{target_str}."

            return self._launch_animepahe_task(
                url=url, episodes=episodes, quality=quality, lang=lang, title=title
            )

    def _launch_animepahe_task(
        self,
        url: Optional[str] = None,
        episodes: Optional[str] = None,
        quality: Optional[str] = None,
        lang: Optional[str] = None,
        title: Optional[str] = None,
    ) -> tuple[bool, str]:
        ok, msg, proc = launch_animepahe_cli(
            self._external_tools_config,
            my_idm_dir=str(Path(__file__).resolve().parent.parent),
            container_hwnd=self._browser_container_hwnd if self._window_visible else None,
            url=url,
            episodes=episodes,
            quality=quality,
            lang=lang,
            title=title,
        )
        if ok and proc:
            self._animepahe_process = proc
            self.animepahe_status_changed.emit(True)

            def _monitor():
                try:
                    proc.wait()
                except Exception:
                    pass

                # Dispatch process_backlogs to the Qt main event loop via signal to avoid
                # synchronous DB/signal operations on a background daemon thread
                try:
                    self.process_backlogs_requested.emit()
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
                        title=next_task.get("title"),
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

    def set_window_visible(self, visible: bool):
        self._window_visible = visible

    @property
    def is_window_visible(self) -> bool:
        return self._window_visible

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

    def set_capture_enabled(self, enabled: bool) -> tuple[bool, str]:
        """Turn browser capture on or off without stopping the loopback server.

        Flips ``BrowserIntegrationConfig.intercept_all`` rather than ``enabled``, which is the
        difference between a paused capture and a disconnected one: ``enabled=False`` tears the
        REST server down, so the extension can no longer reach the app to be told *why* it was
        declined. ``intercept_all`` is the field the extension already mirrors onto
        ``cfg.interceptDownloads`` and already gates its interception on, so it is the one
        field that means "stop intercepting".

        The browser's own copy of that flag is re-synced on its timer (see
        ``background.js``), so browser-native interception converges within that interval;
        ``BrowserServer._handle_add`` reads the same object directly, so anything sent to the
        app is declined immediately.

        Returns ``(changed, message)`` for the caller's status line.
        """
        current = bool(self._browser_config.intercept_all)
        if current == bool(enabled):
            return False, f"Download capture is already {'on' if current else 'off'}."
        self._browser_config.intercept_all = bool(enabled)
        # set_browser_config persists, re-points the server and emits the change signal; the
        # server stays up because `enabled` is untouched.
        self.set_browser_config(self._browser_config)
        log.info(
            "Download capture %s", "enabled" if enabled else "disabled",
        )
        return True, f"Download capture {'enabled' if enabled else 'disabled'}."

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
            self.refresh_tor_availability()
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

        # The endpoint state changed; re-probe so the cached flag is accurate.
        self.refresh_tor_availability()

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

    # -- per-download Tor routing ---------------------------------------------

    def tor_available(self) -> bool:
        """True when a Tor SOCKS5 endpoint is reachable.

        Returns the **cached** result and never performs I/O, so it is safe to
        call from the GUI thread (row painting, context-menu construction). Use
        :meth:`refresh_tor_availability` to update it.
        """
        return bool(self._tor_available_cache)

    def refresh_tor_availability(self, timeout: float = 0.4) -> bool:
        """Probe the Tor port on a daemon thread and publish the result.

        Returns immediately; the cached value is updated when the probe lands.
        Only one probe runs at a time, so a slow port cannot queue up threads.
        """
        if self._stopped or self._tor_probe_in_flight:
            return False
        self._tor_probe_in_flight = True
        config = self._tor_config

        def _probe() -> None:
            live = False
            try:
                live = bool(is_tor_reachable(config.proxy_host, config.proxy_port, timeout=timeout))
            except Exception as exc:
                log.debug("Tor availability probe failed: %s", exc)
            # Drop the result if the manager was stopped while the socket call was in
            # flight. Emitting here would deliver a queued signal into a MainWindow that
            # is mid-close, and closeEvent calls processEvents() - re-entering a
            # half-torn-down window from a worker thread is what crashed the app on quit.
            if self._stopped:
                log.debug("Tor probe result discarded: manager stopped")
                return
            self._tor_probe_result.emit(live)

        threading.Thread(target=_probe, name="tor-probe", daemon=True).start()
        return True

    @Slot(bool)
    def _on_tor_probe_result(self, live: bool) -> None:
        """GUI-thread slot applying a completed availability probe."""
        self._tor_probe_in_flight = False
        changed = live != self._tor_available_cache
        self._tor_available_cache = live
        if changed:
            self.tor_availability_changed.emit(live)

    def is_download_tor_routed(self, download_id: str) -> bool:
        """True when this individual download is flagged to use Tor."""
        entry = self._db.get_download(download_id)
        if not entry or not entry.metadata:
            return False
        return bool(entry.metadata.get("route_through_tor", False))

    def set_download_tor_route(self, download_id: str, enabled: bool) -> tuple[bool, str]:
        """Route or unroute a single download through Tor.

        Returns ``(ok, message)``. Enabling requires a live Tor proxy; the
        download is restarted when it is already transferring so the new route
        takes effect immediately.
        """
        entry = self._db.get_download(download_id)
        if not entry:
            return False, "Download not found."

        enabled = bool(enabled)
        if enabled and not self.tor_available():
            return False, (
                "Tor is not running. Start Tor from Tools → Tor, or set its path in "
                "Tools → Tor Network Settings, before routing a download through it."
            )

        was_active = entry.status in ("downloading", "fetching_metadata", "stalled", "seeding")
        entry.metadata["route_through_tor"] = enabled
        self._db.update_download(entry)
        log.info("Per-download Tor route %s for %s", "enabled" if enabled else "disabled", download_id)

        if not was_active:
            if enabled:
                self._apply_tor_route_to_engine(entry, True)
            return True, f"Tor routing {'enabled' if enabled else 'disabled'} for this download."

        # Re-route an in-flight download by restarting it on the new path.
        try:
            self.pause_download(download_id)
            self._apply_tor_route_to_engine(entry, enabled)
            self.resume_download(download_id)
        except Exception as exc:
            log.warning("Could not restart %s after Tor route change: %s", download_id, exc)
            return False, f"Saved, but could not restart the download: {exc}"
        return True, f"Tor routing {'enabled' if enabled else 'disabled'} for this download."

    def _apply_tor_route_to_engine(self, entry: DownloadEntry, enabled: bool) -> None:
        """Push a per-download Tor route change into the owning engine."""
        if entry.download_type == "torrent":
            self._torrent.set_torrent_tor_route(entry.id, enabled)
        else:
            self._http.set_download_tor_route(entry.id, enabled)

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
        # The torrent engine reads general settings too - the disk-space gate and
        # `metadata_fetch_timeout_days` both live here - and `SettingsDialog` hands back a
        # *copy*, so without this line it kept the object built at startup and kept
        # enforcing the settings the user had just changed. Setting a checker on one engine
        # but not the other is exactly the kind of half-applied preference that is
        # impossible to spot from the UI.
        self._torrent.set_general_config(config)
        config.save()
        self._apply_backlog_timer_config()
        self._apply_torrent_watch_config()
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
        # A Preferences save hands over a *copy*, and the watch folder is one of the fields in
        # it, so the watcher has to be re-pointed from the new object rather than kept on the one
        # it read at startup.
        self._apply_torrent_watch_config()
        self.torrent_config_changed.emit(config)

    def torrent_watch_folder(self) -> str:
        """The folder scanned for new ``.torrent`` files.

        Falls back to the effective default download folder when the preference is blank, which is
        the documented default: a blank setting means "wherever downloads go", not "nowhere".
        Resolved on every call rather than cached, so a change to the download folder is followed
        without having to re-arm the watcher.
        """
        configured = (getattr(self._torrent_config, "torrent_watch_folder", "") or "").strip()
        if configured:
            return configured
        return self._general_config.get_effective_save_path()

    def _apply_torrent_watch_config(self):
        """Point the watcher at the configured folder and arm or disarm it.

        Mirrors :meth:`_apply_backlog_timer_config`, including its most important property: the
        watcher is only ever started when the asyncio thread is alive, so a manager built by a
        test or by a headless import never scans the user's downloads directory. The folder is
        still set, because ``start()`` re-applies this once the thread is up.
        """
        watcher = getattr(self, "_torrent_watcher", None)
        if watcher is None:
            return
        watcher.set_folder(self.torrent_watch_folder())
        max_age = getattr(self._torrent_config, "torrent_watch_max_age_days", 3)
        watcher.set_max_age_days(max_age)
        enabled = bool(getattr(self._torrent_config, "watch_torrent_folder", False))
        running = bool(getattr(self, "_thread", None) and self._thread.is_alive())
        watcher.set_enabled(enabled and running)
        if enabled and not running:
            log.debug(
                "Torrent folder watching is configured but the manager is not running; "
                "it will start with the manager"
            )

    def _on_watched_folder_torrents(self, paths: list):
        """Ingest ``.torrent`` files the watcher reported, and tell the UI.

        ``only_new`` is set: a watcher re-scans on a timer, and a plain re-add of a torrent the
        user has since paused would silently restart it. The session ledger is a cheap pre-filter;
        the database check inside ``add_torrent_files`` is the correctness boundary.
        """
        with self._timer_slot():
            try:
                fresh = [p for p in paths if p not in self._torrent_watch_seen]
                if not fresh:
                    return
                self._torrent_watch_seen.update(fresh)
                added = self.add_torrent_files(fresh, source="watch_folder", only_new=True)
                if added:
                    self.torrent_folder_captured.emit(list(added))
                    if getattr(self._torrent_config, "clean_watched_torrent_files", False):
                        self._clean_watched_torrent_files(added)
            except Exception as exc:
                log.error("Error ingesting watched-folder torrents: %s", exc)

    def _clean_watched_torrent_files(self, paths: list[str]):
        """Move ingested .torrent files from the watched folder to trash."""
        for path in paths:
            try:
                if not os.path.isfile(path):
                    continue
                unlock_path(path)
                success = send_to_trash(path)
                if success:
                    log.info("Cleaned up watched .torrent file to trash: %s", path)
                else:
                    log.warning("Could not move watched .torrent file to trash: %s", path)
            except Exception as exc:
                log.warning("Error cleaning up watched .torrent file %s: %s", path, exc)

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
        with self._timer_slot():
            try:
                count = self.process_backlogs()
                if count > 0:
                    log.info("Periodic backlog poll added %d download(s)", count)
            except Exception as exc:
                log.error("Error in periodic backlog poll: %s", exc)

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run_coro_threadsafe(self, coro) -> Any:
        """Schedule *coro* on the background loop, from any thread. Never blocks.

        Exists so a UI-side caller can start HTTP work without owning a loop. The clipboard
        monitor is the reason: it has to resolve a copied URL over HTTP, but doing that on the
        GUI thread would freeze the window for the probe's timeout.

        Falls back to running the coroutine to completion on a private loop when the manager's
        loop is not up yet, which is the pre-:meth:`start` case — a caller that gets there
        wants the work done more than it wants it scheduled. A coroutine that raises is
        swallowed here: the caller is a Qt slot that has already returned, so an exception has
        nowhere to propagate to and would only be logged by asyncio as an orphan.
        """
        if self._loop and self._loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(coro, self._loop)
                future.add_done_callback(_log_coro_failure)
                return future
            except Exception as exc:
                log.warning("Could not schedule coroutine on the background loop: %s", exc)
                coro.close()
                return None
        try:
            return asyncio.run(coro)
        except Exception as exc:
            log.warning("Background coroutine failed with no loop available: %s", exc)
            return None

    # -- add downloads -------------------------------------------------------

    def add_download(self, url: str, save_path: str = "",
                     num_segments: int = 8,
                     filename: str = "",
                     headers: Optional[dict] = None,
                     metadata: Optional[dict] = None,
                     queue_id: str = "") -> Optional[str]:
        """Add a new download. Returns download_id or None if duplicate resumed.

        ``queue_id`` names the queue the download joins. When it is blank the queue is
        **inferred from the source** (:meth:`_infer_queue_for_source`), so an AnimePahe or
        YouTube download lands in its own queue whichever entry point created it. An explicit
        choice is never overridden — the Add Download dialog and the clipboard monitor pass the
        active queue, and a Move to Queue exists for afterwards.
        """
        url = url.strip()
        if not url:
            return None
        # Whether the caller chose a queue must be captured *before* resolve_queue_id, which
        # turns a blank into the default id and would make "was it blank?" unanswerable.
        explicit_queue = bool(queue_id)
        queue_id = self._db.resolve_queue_id(queue_id)
        if not explicit_queue:
            inferred = self._infer_queue_for_source(url, metadata)
            if inferred:
                queue_id = inferred

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
            if "referer" not in entry_metadata and "referrer" not in entry_metadata:
                ref = headers.get("Referer") or headers.get("referer")
                if ref:
                    entry_metadata["referer"] = ref

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
            queue_id=queue_id,
        )
        if entry_metadata:
            entry.metadata = entry_metadata

        self._db.add_download(entry)
        self.download_added.emit(entry.id)

        # Cache local .torrent file to internal fastresume directory so it survives
        # deletion/cleaning of the source file or temporary media.
        if download_type == "torrent" and os.path.isfile(url):
            try:
                import my_idm.torrent_engine as te
                cache_dir = getattr(te, "FASTRESUME_DIR", None)
                if cache_dir:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    torrent_cache = cache_dir / f"{entry.id}.torrent"
                    if not torrent_cache.exists():
                        shutil.copyfile(url, str(torrent_cache))
            except Exception as exc:
                log.debug("Could not cache .torrent file for %s: %s", entry.id, exc)

        # Start if within the global and per-queue limits; otherwise stays queued.
        counts = self._active_counts_by_queue()
        limits = self._queue_limits()
        if self._may_start(entry, counts, limits, self._general_config.effective_max_concurrent):
            self._start_entry(entry)
        else:
            entry.status = "queued"
            self._db.update_download(entry)
            self.status_changed.emit(entry.id, "queued", "")

        return entry.id

    def add_torrent_files(
        self, paths: list[str], *, source: str = "manual", only_new: bool = False
    ) -> list[str]:
        """Add local ``.torrent`` files. Returns the paths that became downloads.

        The one ingestion policy for every route a ``.torrent`` file takes into the app — the Add
        Torrent button, a drag onto the window, and the watched folder — so they cannot disagree
        about what counts as new. Paths are canonicalised first: ``add_download`` de-duplicates on
        an exact string match of the ``url`` column, and the same file arrives spelled with
        backslashes from a drag and with forward slashes from a directory scan, which would
        otherwise produce two rows and download the torrent twice.

        *only_new* is for the watched folder. ``add_download`` treats a re-add of a paused or
        errored row as a request to **resume** it, which is right for a deliberate Ctrl+V and
        badly wrong for a watcher that rescans every minute: pausing a torrent would silently
        restart it. With *only_new*, a path already in the table is skipped without being
        touched, so a pause sticks.
        """
        added: list[str] = []
        for raw in paths or []:
            path = canonical_torrent_path(raw)
            if not path or not is_torrent_path(path):
                log.debug("Not a usable local .torrent path: %s", raw)
                continue
            if only_new and self._db.find_by_url(path) is not None:
                continue
            try:
                did = self.add_download(
                    path, metadata={"capture_source": f"torrent_{source}"}
                )
            except Exception as exc:
                # One unreadable torrent must not cost the rest of a dropped batch.
                log.warning("Could not add torrent file %s: %s", path, exc)
                continue
            if did:
                added.append(path)
            else:
                # None is ambiguous: add_download also returns it for a completed duplicate and
                # for a rejected URL, so it is deliberately not reported as "added".
                log.info("Torrent file was not added: %s", path)
        if added:
            log.info("Added %d torrent file(s) from %s", len(added), source)
        return added

    def add_download_from_browser(
        self,
        url: str,
        filename: str = "",
        save_path: str = "",
        headers: Optional[dict] = None,
        cookies: Optional[Union[str, dict]] = None,
        referrer: str = "",
        user_agent: str = "",
        pending_min_bytes: int = 0,
    ) -> Optional[str]:
        """Add a download initiated from the browser extension.

        ``pending_min_bytes`` is the caller's minimum-capture threshold when it could not
        determine the file size itself. It is recorded on the entry so the HTTP engine can
        apply the threshold against its own authoritative probe - see
        ``HTTPEngine._enforce_browser_min_size``.
        """
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
        if pending_min_bytes and int(pending_min_bytes) > 0:
            meta["pending_min_bytes"] = int(pending_min_bytes)

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

    def _get_active_download_count(self, queue_id: str = "") -> int:
        """Active transfers across every queue, or just one when *queue_id* is given."""
        counts = self._active_counts_by_queue()
        if queue_id:
            return counts.get(self._db.resolve_queue_id(queue_id), 0)
        return sum(counts.values())

    def _active_counts_by_queue(self) -> dict[str, int]:
        """Active transfers per queue, including ones not yet reported as downloading.

        ``_starting_downloads`` is added in because it covers the window between deciding to
        start a download and the engine reporting it as ``downloading``. Without it a queue's
        first start always overshoots its own budget by exactly this set's size, and the
        overshoot is invisible until it settles.

        One grouped query rather than a full scan per candidate: the grouped form is what makes
        per-queue accounting *cheaper* than the single global count it replaces.
        """
        counts = self._db.get_active_counts_by_queue()
        active_db_statuses = ('downloading', 'checking', 'fetching_metadata', 'stalled')
        for did in self._starting_downloads:
            entry = self._db.get_download(did)
            if entry and entry.status not in active_db_statuses:
                key = self._db.resolve_queue_id(entry.queue_id)
                counts[key] = counts.get(key, 0) + 1
        if hasattr(self, "_http") and hasattr(self._http, "get_active_download_ids"):
            for did in self._http.get_active_download_ids():
                if did not in self._starting_downloads:
                    entry = self._db.get_download(did)
                    if entry and entry.status not in active_db_statuses:
                        key = self._db.resolve_queue_id(entry.queue_id)
                        counts[key] = counts.get(key, 0) + 1
        return counts

    def _queue_limits(self) -> dict[str, int]:
        """Local concurrency ceiling per queue. ``0`` means unlimited within that queue."""
        return {q.id: q.effective_max_concurrent for q in self._db.get_queues()}

    def _may_start(self, entry: DownloadEntry, counts: dict[str, int],
                   limits: dict[str, int], global_max: int,
                   now: Optional[Union[datetime, date]] = None) -> bool:
        """Whether *entry* can start now, under both the global and its own queue's budget.

        The single decision every start path shares. It is a method rather than inlined
        arithmetic because three call sites reach ``_start_entry`` without passing through each
        other - ``_process_queue``, ``add_download`` and ``resume_download`` - and a check
        written into only one of them is a check that will be bypassed.
        """
        qid = self._db.resolve_queue_id(entry.queue_id)
        # Off-peak scheduler check: force start overrides it
        is_force = bool(entry.metadata.get("force_started", False))
        if not is_force and not self.is_within_schedule(now):
            return False

        # Bandwidth limit gate: global overrides per-queue, stops entry from starting if limit exceeded
        allowed, _msg, _pct = self._db.check_bandwidth_limit(qid, 0, False, now)
        if not allowed:
            return False

        if sum(counts.values()) >= global_max:
            return False
        limit = limits.get(qid, 0)
        # limit 0 == unlimited within the queue; only the global ceiling can stop it.
        if limit > 0 and counts.get(qid, 0) >= limit:
            return False
        return True

    def _process_queue(self):
        """Start queued downloads up to the global and per-queue limits, in priority order.

        Ordering is *queue_order within a queue*, and a queue that has budget is considered
        before one that does not. That second part is deliberate: a user who puts a torrent in
        its own queue with ``max_concurrent = 1`` wants it to start first, not to be starved by
        a queue of 500 small files. A saturated queue sinks to the back of the walk rather than
        being dropped, so it is picked up the moment a slot frees.
        """
        global_max = self._general_config.effective_max_concurrent
        counts = self._active_counts_by_queue()
        limits = self._queue_limits()
        if sum(counts.values()) >= global_max:
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

        priority = lambda e: (e.queue_order if e.queue_order > 0 else 999999, e.added_at or "")
        by_queue: dict[str, list[DownloadEntry]] = {}
        for entry in eligible:
            by_queue.setdefault(self._db.resolve_queue_id(entry.queue_id), []).append(entry)
        for group in by_queue.values():
            group.sort(key=priority)

        # Queues that can still start something come first; the rest follow so that a slot
        # opening up in a saturated queue is filled immediately rather than on the next tick.
        # Ties break on the queue switcher's own order (default first, then the user's
        # `position`), never on the raw id - ids are uuids, so an id tiebreaker would make the
        # dispatch order differ between two runs with identical state.
        order_index = {
            queue.id: index for index, queue in enumerate(self._db.get_queues())
        }

        def has_budget(queue_id: str) -> bool:
            limit = limits.get(queue_id, 0)
            used = counts.get(queue_id, 0)
            return limit <= 0 or used < limit

        queue_ids = sorted(
            by_queue, key=lambda q: (not has_budget(q), order_index.get(q, 999))
        )

        for queue_id in queue_ids:
            for entry in by_queue[queue_id]:
                if not self._may_start(entry, counts, limits, global_max):
                    continue
                if entry.retry_count > 0:
                    log.info(
                        "Auto-retrying queued download %s (attempt %d/%d, order=%s, queue=%s)",
                        entry.id, entry.retry_count + 1, entry.max_retries,
                        entry.queue_order, queue_id,
                    )
                else:
                    log.info(
                        "Starting queued download %s (order=%s, queue=%s)",
                        entry.id, entry.queue_order, queue_id,
                    )
                self._start_entry(entry)
                counts[queue_id] = counts.get(queue_id, 0) + 1

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
            if self._is_ytdlp_native_entry(entry):
                self._starting_downloads.discard(entry.id)
                self._start_ytdlp_native_job(
                    entry,
                    entry.metadata.get("youtube_format", ""),
                    entry.save_path,
                )
                return
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

        if entry.metadata.get("force_started"):
            entry.metadata.pop("force_started", None)
            self._db.update_download(entry)

        self._starting_downloads.discard(download_id)
        if self._is_ytdlp_native_entry(entry):
            self._stop_ytdlp_worker(download_id, "paused")

        if entry.download_type == "http":
            segments = self._db.get_segments(download_id)
            if segments:
                seg_dl = sum(s.downloaded_bytes for s in segments)
                if seg_dl > entry.downloaded_size:
                    entry.downloaded_size = seg_dl
                    self._db.update_progress(download_id, seg_dl)
            elif entry.file_path and Path(entry.file_path).exists():
                try:
                    f_size = Path(entry.file_path).stat().st_size
                    if f_size > entry.downloaded_size:
                        entry.downloaded_size = f_size
                        self._db.update_progress(download_id, f_size)
                except OSError:
                    pass
        elif entry.download_type == "torrent":
            status = self._torrent.get_status(download_id)
            if status and status["downloaded"] > entry.downloaded_size:
                entry.downloaded_size = status["downloaded"]
                self._db.update_progress(download_id, entry.downloaded_size)

        self._db.update_status(download_id, "paused")
        self._db.update_queue_order(download_id, 0)
        self.status_changed.emit(download_id, "paused", "")

        if entry.download_type == "http":
            if self._is_ytdlp_native_entry(entry):
                pass
            elif self._loop:
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
        if self._is_ytdlp_native_entry(entry):
            self._stop_ytdlp_worker(download_id, "stopped")
        if entry.download_type == "http":
            if self._is_ytdlp_native_entry(entry):
                pass
            elif self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._http.pause(download_id), self._loop
                )
        elif entry.download_type == "torrent":
            self._torrent.pause(download_id)

        # Sync downloaded_size
        if entry.download_type == "http":
            segments = self._db.get_segments(download_id)
            if segments:
                seg_dl = sum(s.downloaded_bytes for s in segments)
                if seg_dl > entry.downloaded_size:
                    entry.downloaded_size = seg_dl
            elif entry.file_path and Path(entry.file_path).exists():
                try:
                    f_size = Path(entry.file_path).stat().st_size
                    if f_size > entry.downloaded_size:
                        entry.downloaded_size = f_size
                except OSError:
                    pass
        elif entry.download_type == "torrent":
            status = self._torrent.get_status(download_id)
            if status and status["downloaded"] > entry.downloaded_size:
                entry.downloaded_size = status["downloaded"]

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

    def resume_downloads(self, download_ids: list[str]):
        """Queue multiple downloads for resume via the status job pool."""
        self._status_job_pool.submit_batch("resume", download_ids)

    def recheck_downloads(self, download_ids: list[str]):
        """Queue multiple downloads for recheck via the status job pool."""
        self._status_job_pool.submit_batch("recheck", download_ids)

    def pause_downloads(self, download_ids: list[str]):
        """Queue multiple downloads for pause via the status job pool."""
        self._status_job_pool.submit_batch("pause", download_ids)

    def stop_downloads(self, download_ids: list[str]):
        """Queue multiple downloads for stop via the status job pool."""
        self._status_job_pool.submit_batch("stop", download_ids)

    def force_start_downloads(self, download_ids: list[str]):
        """Queue multiple downloads for force start via the status job pool."""
        self._status_job_pool.submit_batch("force_start", download_ids)

    def wait_status_jobs(self, timeout: float = 5.0) -> bool:
        """Wait for all pending status pool jobs to finish."""
        return self._status_job_pool.wait_idle(timeout=timeout)

    def pause_all_downloads(self) -> int:
        """Pause all ongoing and queued downloads.

        Targets transfers in 'downloading', 'queued', 'checking', 'fetching_metadata',
        and 'stalled' states.
        Returns the number of paused downloads.
        """
        pausable = [
            e for e in self._db.get_all_downloads()
            if e.status in ("downloading", "queued", "checking", "fetching_metadata", "stalled")
        ]
        for entry in pausable:
            self.pause_download(entry.id)
        return len(pausable)

    def resume_all_downloads(self) -> int:
        """Resume all paused or stopped downloads.

        Targets transfers in 'paused' or 'stopped' states.
        Returns the number of resumed downloads.
        """
        resumable = [
            e for e in self._db.get_all_downloads()
            if e.status in ("paused", "stopped")
        ]
        for entry in resumable:
            self.resume_download(entry.id)
        return len(resumable)

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
            entry.queue_order = self._db.get_next_queue_order(entry.queue_id)
        if not entry.file_path and entry.filename and entry.save_path:
            entry.file_path = str(Path(entry.save_path) / entry.filename)

        # YouTube CDN URLs expire within hours; re-resolve before resuming.
        if entry.status in ("paused", "error", "stopped", "queued"):
            try:
                self._maybe_refresh_youtube_url(entry)
            except Exception as exc:
                log.debug("YouTube URL refresh during resume failed for %s: %s", download_id, exc)

        self._db.update_download(entry)
        self.status_changed.emit(download_id, "queued", "")

        counts = self._active_counts_by_queue()
        limits = self._queue_limits()
        if not self._may_start(entry, counts, limits, self._general_config.effective_max_concurrent):
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
        entry.metadata["force_started"] = True
        if not entry.file_path and entry.filename and entry.save_path:
            entry.file_path = str(Path(entry.save_path) / entry.filename)
        self._db.update_download(entry)

        if entry.download_type == "http":
            if self._is_ytdlp_native_entry(entry):
                if not self.is_ytdlp_native_job(download_id):
                    self._start_ytdlp_native_job(
                        entry,
                        entry.metadata.get("youtube_format", ""),
                        entry.save_path,
                    )
            elif not self._http.is_active(download_id):
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

    def _stop_ytdlp_worker(self, download_id: str, reason: str, timeout: float = 5.0,
                          force: bool = False) -> bool:
        """Signal a Mode B worker to abort and wait for it to release its files.

        yt-dlp keeps the file handle open while running, so deleting temp files
        before the worker exits leaves them locked on Windows.

        Returns True when the worker exited. With *force* the job is dropped from
        the registry regardless — used when the download is being removed, where
        keeping a stale entry would leak and a later resume must be allowed.
        """
        with self._ytdlp_lock:
            job = self._ytdlp_jobs.get(download_id)
        if not job:
            return False

        holder = job.get("cancel_holder")
        if holder is not None:
            holder["cancel"] = True
        event = job.get("cancel_event")
        if event is not None:
            event.set()

        thread = job.get("thread")
        exited = True
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                exited = False
                log.warning(
                    "yt-dlp worker for %s did not exit within %.0fs (%s); "
                    "its temporary files may stay locked",
                    download_id, timeout, reason,
                )

        if exited or force:
            self._drop_ytdlp_job(download_id)
            log.info("yt-dlp worker for %s stopped (%s)", download_id, reason)
        return exited

    def _purge_youtube_temp_files(self, entry: DownloadEntry) -> list[str]:
        """Delete yt-dlp scratch files (``.part``, ``.ytdl``, ``.fNNN.*``) for an entry.

        yt-dlp deliberately keeps ``.part`` files so a download can resume, and
        merged streams leave per-format fragments behind. Removing only
        ``entry.file_path`` leaves those on disk after a delete.
        """
        removed: list[str] = []
        directory = Path(entry.save_path) if entry.save_path else None
        if directory is None or not directory.is_dir():
            return removed

        stem = entry.metadata.get("youtube_stem") or ""
        if not stem:
            stem, _ext = split_extension(entry.filename or "")
        if not stem:
            return removed
        stem = youtube_tool.build_stem(stem)

        for candidate in directory.glob(f"{glob.escape(stem)}.*"):
            if not candidate.is_file():
                continue
            name = candidate.name
            is_scratch = (
                name.endswith((".part", ".ytdl", ".temp", ".tmp"))
                or re.search(r"\.f\d+", name) is not None
            )
            if not is_scratch:
                continue
            try:
                candidate.unlink()
                removed.append(name)
            except OSError as exc:
                log.warning("Could not remove yt-dlp temp file %s: %s", candidate, exc)
        if removed:
            log.info("Removed %d yt-dlp temp file(s) for %s: %s",
                     len(removed), entry.id, ", ".join(removed))
        return removed

    def delete_download(self, download_id: str, delete_files: bool = False):
        entry = self._db.get_download(download_id)
        if not entry:
            return

        # Stop active download
        is_ytdlp = self._is_ytdlp_native_entry(entry)
        if is_ytdlp:
            self._stop_ytdlp_worker(download_id, "download deleted", force=True)
        if entry.download_type == "http":
            if is_ytdlp:
                pass
            elif self._loop and self._loop.is_running():
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
            if is_ytdlp:
                self._purge_youtube_temp_files(entry)

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
        is_ytdlp = self._is_ytdlp_native_entry(entry)
        if is_ytdlp:
            self._stop_ytdlp_worker(download_id, "file deleted", force=True)
        if entry.download_type == "http":
            if is_ytdlp:
                pass
            elif self._loop and self._loop.is_running():
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
        if is_ytdlp:
            self._purge_youtube_temp_files(entry)

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

    def update_download_url(
        self, download_id: str, new_url: str, resume: bool = False
    ) -> bool:
        """Update the source URL/address for an existing download.

        Used when an HTTP download link has expired (e.g. token-gated CDNs, Google Drive,
        cloud storage answering 403 or 410) without discarding already written bytes or segments.

        Args:
            download_id: The ID of the download entry.
            new_url: The new HTTP/HTTPS URL.
            resume: Whether to resume the download immediately after updating the URL.

        Returns:
            True if updated successfully, False otherwise.
        """
        entry = self._db.get_download(download_id)
        if not entry:
            log.warning("Cannot update URL: download %s not found", download_id)
            return False

        new_url = (new_url or "").strip()
        if not new_url:
            log.warning("Cannot update URL: empty URL provided for %s", download_id)
            return False

        if not (new_url.startswith("http://") or new_url.startswith("https://")):
            log.warning("Cannot update URL: '%s' is not an HTTP/HTTPS URL", new_url)
            return False

        if entry.status in ("downloading", "scanning", "checking"):
            log.warning(
                "Cannot update URL: download %s is currently active (%s)",
                download_id, entry.status,
            )
            return False

        old_url = entry.url
        entry.url = new_url

        # Ensure explicit_filename is set if filename exists so resuming with the new URL
        # does not change the destination filename or file path.
        if entry.filename:
            if not isinstance(entry.metadata, dict):
                entry.metadata = {}
            entry.metadata["explicit_filename"] = True

        # If download was in error state, reset diagnostic and retries
        was_error = entry.status == "error"
        if was_error:
            entry.error_message = ""
            entry.status = "paused"
            entry.retry_count = 0
            self._db.update_status(download_id, "paused", "")

        self._db.update_download_url(download_id, new_url)
        self._db.update_download(entry)

        log.info(
            "Updated URL for download %s from '%s' to '%s'",
            download_id, old_url, new_url,
        )
        self.download_url_updated.emit(download_id, new_url)

        if was_error:
            self.status_changed.emit(download_id, "paused", "")

        if resume:
            self.resume_download(download_id)

        return True

    def _relocate_youtube_file(self, entry: DownloadEntry) -> bool:
        """Try to find a YouTube download whose recorded path is wrong.

        Handles files written under a name that differs from the database entry
        because each side sanitised the video title differently (yt-dlp maps
        ``/`` to ``/``). Returns True when the entry was repaired.
        """
        from my_idm import youtube_tool as ytt

        meta = entry.metadata if entry else {}
        if not meta or not str(meta.get("source_type", "")).startswith("youtube"):
            return False
        if not entry.save_path or not os.path.isdir(entry.save_path):
            return False

        recorded = Path(entry.file_path) if entry.file_path else None
        if recorded and recorded.is_file():
            return False

        def _norm(name: str) -> str:
            # Strip the extension from the raw string: a stored YouTube name can
            # contain "/" (Path would treat that as a directory separator).
            base = str(name or "")
            if "." in base:
                base = base.rsplit(".", 1)[0]
            base = re.sub(r"[^\w]+", "", base, flags=re.UNICODE)
            return base.lower()

        wanted = {
            _norm(entry.filename or ""),
            _norm(meta.get("youtube_stem") or ""),
            _norm(meta.get("title") or ""),
        }
        wanted.discard("")
        if not wanted:
            return False

        try:
            candidates = [p for p in Path(entry.save_path).iterdir() if p.is_file()]
        except OSError:
            return False

        best = None
        for path in candidates:
            if path.name.endswith((".part", ".ytdl", ".temp", ".tmp")):
                continue
            if re.search(r"\.f\d+\.", path.name) or path.name.startswith(".f"):
                continue
            if _norm(path.name) in wanted:
                if best is None or path.stat().st_size > best.stat().st_size:
                    best = path

        if best is None:
            return False

        try:
            entry.filename = best.name
            entry.file_path = str(best)
            entry.save_path = str(best.parent)
            entry.total_size = best.stat().st_size
            entry.downloaded_size = entry.total_size
        except OSError:
            return False

        self._db.update_download(entry)
        self._db.update_status(entry.id, "completed")
        return True

    def mark_file_not_found(self, download_id: str):
        """Mark download status as file_not_found when missing on disk.

        YouTube entries are re-checked against the save folder first: the recorded
        path can disagree with what yt-dlp actually wrote when the title contained
        characters that either side sanitised differently.
        """
        entry = self._db.get_download(download_id)
        if not entry:
            return

        if self._relocate_youtube_file(entry):
            log.info("Recovered YouTube file for %s: %s", download_id, entry.file_path)
            self.status_changed.emit(download_id, "completed", "")
            self.progress_updated.emit(
                download_id, entry.downloaded_size, entry.total_size, 0.0, 0.0, 0, 0, 0.0
            )
            return

        entry.status = "file_not_found"
        entry.error_message = "File not found on disk"
        self._db.update_status(download_id, "file_not_found", "File not found on disk")
        self.status_changed.emit(download_id, "file_not_found", "File not found on disk")

    def verify_completed_downloads(self) -> int:
        """Verify that completed downloads still exist on disk.

        Checks every entry currently in 'completed' status. If the target file
        or directory on disk is missing, transitions the entry to 'file_not_found'
        (giving YouTube entries an opportunity to relocate if sanitised differently).

        Returns:
            The number of entries transitioned to 'file_not_found'.
        """
        if getattr(self, "_stopped", False):
            return 0

        completed_entries = self._db.get_completed_downloads()
        missing_count = 0
        for entry in completed_entries:
            file_path = entry.file_path
            if not file_path and entry.save_path and entry.filename:
                file_path = str(Path(entry.save_path) / entry.filename)

            if file_path:
                try:
                    expanded = os.path.expandvars(os.path.expanduser(file_path))
                    p = Path(expanded)
                    exists = p.exists()
                except Exception:
                    exists = False
            else:
                exists = False

            if not exists:
                self.mark_file_not_found(entry.id)
                updated = self._db.get_download(entry.id)
                if updated and updated.status == "file_not_found":
                    missing_count += 1
            elif not entry.file_path and file_path:
                entry.file_path = normalize_path(file_path)
                self._db.update_download(entry)

        return missing_count

    def _on_verify_completed_timer_tick(self):
        """Timer callback to periodically verify completed downloads on disk."""
        with self._timer_slot():
            try:
                self.verify_completed_downloads()
            except Exception as exc:
                log.error("Error during periodic completed downloads verification: %s", exc)

    def _check_bandwidth_limits(self):
        """Timer callback to check bandwidth limits and update warnings.
        
        Checks if any bandwidth limit has been reached or exceeded, and emits
        warnings or stops downloads as needed.
        """
        with self._timer_slot():
            try:
                self._enforce_bandwidth_limits()
            except Exception as exc:
                log.error("Error checking bandwidth limits: %s", exc)

    def _enforce_bandwidth_limits(self):
        """Enforce bandwidth limits by checking current usage against limits."""
        self.check_all_bandwidth_limits()

    def check_all_bandwidth_limits(self):
        """Evaluate all active bandwidth limits and emit warning/exceeded/cleared signals."""
        # 1. Check global limits first ("global overrides per queue limit")
        allowed, msg, pct = self._db.check_bandwidth_limit("", 0, False)
        if not allowed:
            self._on_bandwidth_limit_exceeded("", msg, pct, is_global=True)
            return
        if msg:
            self._on_bandwidth_warning("", msg, pct, is_global=True)
            return

        # 2. Check all queues
        queues = self._db.get_queues()
        highest_warn_pct = 0.0
        highest_warn_msg = ""
        highest_warn_qid = ""
        for q in queues:
            allowed, msg, pct = self._db.check_bandwidth_limit(q.id, 0, False)
            if not allowed:
                self._on_bandwidth_limit_exceeded(q.id, msg, pct, is_global=False)
                return
            if msg and pct > highest_warn_pct:
                highest_warn_pct = pct
                highest_warn_msg = msg
                highest_warn_qid = q.id

        if highest_warn_msg:
            self._on_bandwidth_warning(highest_warn_qid, highest_warn_msg, highest_warn_pct, is_global=False)
        else:
            self.bandwidth_warning_cleared.emit()

    def record_bandwidth_usage(self, download_id: str, downloaded_bytes: int = 0, uploaded_bytes: int = 0):
        """Record bandwidth usage for a download and check limits."""
        if downloaded_bytes <= 0 and uploaded_bytes <= 0:
            return

        entry = self._db.get_download(download_id)
        if not entry:
            return

        qid = self._db.resolve_queue_id(entry.queue_id)
        allowed, msg, pct = self._db.record_bandwidth(qid, downloaded_bytes, uploaded_bytes)
        if not allowed:
            is_global = "Global" in msg
            self._on_bandwidth_limit_exceeded(qid if not is_global else "", msg, pct, is_global)
        elif msg:
            is_global = "Global" in msg
            self._on_bandwidth_warning(qid if not is_global else "", msg, pct, is_global)

    def _on_bandwidth_warning(self, queue_id: str, message: str, percentage: float, is_global: bool):
        """Handle bandwidth warning - emit signal for UI to show warning badge."""
        self.bandwidth_warning.emit(queue_id, message, percentage, is_global)

    def _on_bandwidth_limit_exceeded(self, queue_id: str, message: str, percentage: float, is_global: bool):
        """Handle bandwidth limit exceeded - stop active downloads/uploads and emit signal."""
        if is_global:
            self._stop_all_downloads_for_bandwidth_limit()
        else:
            self._stop_downloads_for_queue(queue_id)

        self.bandwidth_limit_exceeded.emit(queue_id, message, percentage, is_global)

    def _stop_all_downloads_for_bandwidth_limit(self):
        """Stop all active downloads and uploads due to global bandwidth limit."""
        active = self._db.get_all_downloads()
        for entry in active:
            if entry.status in ("downloading", "checking", "fetching_metadata", "stalled", "seeding"):
                self.pause_download(entry.id)

    def _stop_downloads_for_queue(self, queue_id: str):
        """Stop all active downloads and uploads for a specific queue."""
        resolved = self._db.resolve_queue_id(queue_id)
        active = self._db.get_all_downloads(resolved)
        for entry in active:
            if entry.status in ("downloading", "checking", "fetching_metadata", "stalled", "seeding"):
                self.pause_download(entry.id)

    # -- Scheduler methods ----------------------------------------------------

    @property
    def scheduler_config(self) -> SchedulerConfig:
        return self._scheduler_config

    def set_scheduler_config(self, cfg: SchedulerConfig):
        self._scheduler_config = cfg
        self.scheduler_config_changed.emit(cfg)
        self._enforce_scheduler()
        self._process_queue()

    def is_within_schedule(self, now: Optional[Union[datetime, date]] = None) -> bool:
        """Check if current time is within configured off-peak schedule window."""
        return is_within_schedule_window(self._scheduler_config, now)

    def _check_scheduler(self):
        """Timer callback to check scheduler transitions."""
        with self._timer_slot():
            try:
                self._enforce_scheduler()
            except Exception as exc:
                log.error("Error checking scheduler: %s", exc)

    def _enforce_scheduler(self, now: Optional[Union[datetime, date]] = None):
        """Enforce scheduler off-peak window transitions."""
        if not self._scheduler_config.enabled:
            self._was_within_schedule = None
            return

        is_in = self.is_within_schedule(now)
        prev = self._was_within_schedule
        self._was_within_schedule = is_in

        if prev is False and is_in is True:
            # Entered off-peak window!
            log.info("Entered scheduled off-peak window; processing queue.")
            self._process_queue()
        elif prev is True and is_in is False:
            # Exited off-peak window!
            log.info("Exited scheduled off-peak window.")
            if self._scheduler_config.pause_when_ended:
                self._pause_downloads_for_scheduler()

    def _pause_downloads_for_scheduler(self):
        """Pause running downloads when the off-peak window ends, unless force-started."""
        active = self._db.get_all_downloads()
        for entry in active:
            if entry.status in ("downloading", "checking", "fetching_metadata", "stalled", "seeding"):
                if not entry.metadata.get("force_started"):
                    self.pause_download(entry.id)

    def move_queue_up(self, download_id: str) -> bool:
        """Move a download up in its queue's priority order.

        Scoped to the download's own queue. Sorting every download together made the control
        meaningless across queues: moving a row up in queue A cannot change what happens in
        queue B, and letting it appear to do so was the bug.
        """
        return self._move_within_queue(download_id, -1)

    def move_queue_down(self, download_id: str) -> bool:
        """Move a download down in its queue's priority order."""
        return self._move_within_queue(download_id, +1)

    def _move_within_queue(self, download_id: str, delta: int) -> bool:
        entry = self._db.get_download(download_id)
        if not entry:
            return False
        queue_id = self._db.resolve_queue_id(entry.queue_id)
        siblings = [
            d for d in self._db.get_all_downloads(queue_id)
            if d.queue_order > 0 or d.id == download_id
        ]
        siblings.sort(key=lambda d: (d.queue_order if d.queue_order > 0 else 999999,
                                    d.added_at or ""))
        idx = next((i for i, d in enumerate(siblings) if d.id == download_id), -1)
        if idx == -1:
            return False
        target_idx = idx + delta
        if target_idx < 0 or target_idx >= len(siblings):
            return False

        # Move the entry in the list, then renumber the WHOLE queue densely. Renumbering only the
        # affected run left the rows before it holding values the moved row had just vacated,
        # producing two downloads with the same priority - which is the bug this shape was
        # written to avoid in the first place. Whole-queue renumbering is O(n) writes on one
        # user action, which is not the bottleneck worth optimising.
        moving = siblings.pop(idx)
        siblings.insert(target_idx, moving)
        for position, item in enumerate(siblings, start=1):
            if item.queue_order != position:
                self._db.update_queue_order(item.id, position)
        self.queue_order_changed.emit()
        return True

    # -- named queues ---------------------------------------------------------

    #: Hosts that identify their source by URL rather than by metadata. A YouTube link pasted
    #: into the Add Download dialog or captured from the browser carries no `source_type`, but
    #: it is still obviously a YouTube download.
    _YOUTUBE_HOSTS = ("youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com")

    def _infer_queue_for_source(
        self, url: str, metadata: Optional[dict]
    ) -> str:
        """The queue a download belongs to by virtue of where it came from, or ``""``.

        Matches the ``source_type`` / ``added_by`` markers the YouTube and AnimePahe paths
        already write, and falls back to the URL host so a hand-added YouTube link is routed
        the same way. Returns ``""`` when nothing matches, meaning "use the default queue".
        """
        meta = metadata or {}
        source_type = str(meta.get("source_type", "")).lower()
        added_by = str(meta.get("added_by", "")).lower()
        if "youtube" in source_type or "youtube" in added_by:
            return _SOURCE_QUEUE_BY_KEY.get("youtube", "")
        if "animepahe" in source_type or "animepahe" in added_by or "anime_url" in meta or "anime_title" in meta:
            return _SOURCE_QUEUE_BY_KEY.get("animepahe", "")
        lowered = url.lower()
        if any(host in lowered for host in self._YOUTUBE_HOSTS):
            return _SOURCE_QUEUE_BY_KEY.get("youtube", "")
        if any(cdn in lowered for cdn in ("owocdn.top", "uwucdn.top", "kwik.cx", "kwik.")):
            return _SOURCE_QUEUE_BY_KEY.get("animepahe", "")
        return ""

    def queue_id_for_name(self, name: str) -> str:
        """Resolve a queue *name* (as a backlog file spells it) to its id.

        Falls back to the default queue for an unknown name and says so in the log. Creating
        the queue instead would be worse: backlog files can be machine-generated, and a
        generator plus auto-create is how you end up with "Queue1", "Queue2".
        """
        queue = self._db.get_queue_by_name(name)
        if queue:
            return queue.id
        cleaned = (name or "").strip()
        if cleaned:
            log.warning(
                "Backlog references unknown queue %r; using %s instead. "
                "Create it first if that was not intended.",
                cleaned, DEFAULT_QUEUE_NAME,
            )
        return DEFAULT_QUEUE_ID

    def get_queues(self) -> list[QueueInfo]:
        """All queues, default first."""
        return self._db.get_queues()

    def get_queue(self, queue_id: str) -> Optional[QueueInfo]:
        """One queue by id. Blank or unknown resolves to the default rather than ``None``."""
        return self._db.get_queue(queue_id)

    def get_active_queue(self) -> str:
        """The queue the downloads list is scoped to. ``""`` means all queues."""
        stored = self._db.get_ui_state("active_queue_id", "") or ""
        return stored if stored and self._db.get_queue(stored) else ""

    def set_active_queue(self, queue_id: str):
        """Scope the downloads list to one queue, or to all of them when *queue_id* is blank.

        Blanking is the default and the startup state: history is the product, so a user must
        never find downloads they already had missing because a queue is selected.
        """
        resolved = queue_id if queue_id and self._db.get_queue(queue_id) else ""
        self._db.set_ui_state("active_queue_id", resolved)
        self.queue_scope_changed.emit(resolved)
        self.queues_changed.emit()

    def create_queue(self, name: str, max_concurrent: int = 3, color: str = "",
                       download_limit: int = 0, upload_limit: int = 0) -> tuple[bool, str]:
        ok, message = self._db.create_queue(
            name, max_concurrent, color, download_limit, upload_limit
        )
        if ok:
            self._push_queue_limits()
            self.queues_changed.emit()
        return ok, message

    def _push_queue_limits(self):
        """Hand both engines a snapshot of every queue's bandwidth ceilings.

        A snapshot rather than a database handle: the HTTP chunk pacers read the ceiling once
        per chunk and the torrent engine once per handle refresh, and neither rate changes at
        anything like the frequency that would justify a query. Re-pushed whenever a queue is
        created, deleted or re-limited.
        """
        limits = {
            queue.id: (queue.download_limit, queue.upload_limit)
            for queue in self._db.get_queues()
        }
        self._http.set_queue_limits(limits)
        self._torrent.set_queue_limits(limits)

    def set_queue_limits(self, queue_id: str, download_limit: int, upload_limit: int):
        """Set a queue's bandwidth ceilings, in bytes/sec. ``0`` means "no ceiling of its own".

        Takes effect on the next chunk for HTTP downloads and on the next handle refresh for
        torrents, so a running download slows down rather than restarting.
        """
        self._db.set_queue_limits(queue_id, download_limit, upload_limit)
        self._push_queue_limits()
        self.queues_changed.emit()


    def rename_queue(self, queue_id: str, name: str) -> tuple[bool, str]:
        ok, message = self._db.rename_queue(queue_id, name)
        if ok:
            self.queues_changed.emit()
        return ok, message

    def set_queue_max_concurrent(self, queue_id: str, max_concurrent: int):
        """Change a queue's local concurrency ceiling.

        Takes effect on the next 1 Hz queue tick rather than pre-emptively: pausing a running
        download to free a slot would be a worse surprise than a one-second delay.
        """
        self._db.set_queue_max_concurrent(queue_id, max_concurrent)
        self.queues_changed.emit()
        self._process_queue()

    def set_queue_color(self, queue_id: str, color: str) -> tuple[bool, str]:
        """Change a queue's swatch colour, which every row of that queue redraws."""
        ok, message = self._db.set_queue_color(queue_id, color)
        if ok:
            self.queues_changed.emit()
        return ok, message

    def move_queue_in_list(self, queue_id: str, delta: int):
        self._db.move_queue_position(queue_id, delta)
        self.queues_changed.emit()

    def delete_queue(self, queue_id: str) -> tuple[bool, str]:
        """Delete a queue, moving its downloads to the default one.

        The count of re-homed downloads is folded into the message so the caller can surface
        *how much history just moved* before the view stops showing it.
        """
        moved = len(self._db.get_all_downloads(queue_id))
        ok, message = self._db.delete_queue(queue_id)
        if not ok:
            return ok, message
        # Compare against the *stored* scope, not get_active_queue(). That getter falls back
        # to "" when the stored id no longer resolves - and the queue just deleted is exactly
        # such a case - so asking it "is the deleted queue selected?" always answers no, and
        # the stale id survives in ui_state with the view still filtered to a dead queue.
        stored_scope = self._db.get_ui_state("active_queue_id", "") or ""
        if stored_scope == queue_id:
            self.set_active_queue("")
        self._push_queue_limits()
        self.queues_changed.emit()
        return True, message if not moved else f"{message} ({moved} moved)"

    def move_downloads_to_queue(self, download_ids: list[str], queue_id: str) -> tuple[bool, str]:
        """Re-home downloads into another queue, preserving their relative priority."""
        if not download_ids:
            return False, "No downloads selected."
        target = self._db.get_queue(queue_id)
        if not target:
            return False, "That queue no longer exists."

        # Keep the order the user selected them in, not the order the rows happened to come
        # back from the database.
        order = {did: i for i, did in enumerate(download_ids)}
        existing = [did for did in download_ids if self._db.get_download(did)]
        existing.sort(key=lambda did: order.get(did, 0))

        moved = self._db.reassign_queue(existing, target.id)
        if not moved:
            return False, "None of those downloads still exist."
        self.queues_changed.emit()
        self.queue_order_changed.emit()
        return True, f"Moved {moved} download(s) to '{target.name}'."

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
        # Sticky queue name from a `queue:` directive, mirroring active_save_path. Held as a
        # *name* because that is what the file spells; resolved to an id per download line, so a
        # queue renamed between two lines of the same file still lands correctly.
        active_queue = ""
        entries_info: list[tuple[int, str, bool, bool]] = []
        total_download_lines = 0
        successful_download_lines = 0

        last_comment = ""
        for idx, raw_line in enumerate(lines):
            parsed = parse_backlog_entry(
                raw_line, active_save_path,
                last_comment=last_comment, active_queue=active_queue,
            )
            url, save_path, new_dir = parsed[0], parsed[1], parsed[2]
            entry_filename = getattr(parsed, "filename", "")
            entry_headers = getattr(parsed, "headers", {})
            entry_queue = getattr(parsed, "queue", "")

            if new_dir:
                active_save_path = new_dir
                entries_info.append((idx, raw_line, False, True))
                continue

            # A queue directive has no new_dir, so it is handled after the dir check: it sets
            # the queue for every download that follows, exactly as `dir:` does.
            queue_directive = getattr(parsed, "queue_directive", "")
            if queue_directive:
                active_queue = queue_directive
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

            # The comment above a download line is retained as provenance, so an
            # AnimePahe-generated backlog still identifies its origin. The text is
            # variable ("# AnimePahe Download" vs "# <title> - Episode N"), so match
            # on the substring rather than an exact prefix.
            entry_source: dict[str, str] = {}
            if "animepahe" in last_comment.lower():
                entry_source["added_by"] = "animepahe"
            
            # Also extract anime_url, anime_title, and referer from parsed headers if present
            # (added by AnimePahe downloader as custom key=value in backlog line)
            if hasattr(parsed, "headers") and parsed.headers:
                if "anime_url" in parsed.headers:
                    entry_source["anime_url"] = parsed.headers["anime_url"]
                if "anime_title" in parsed.headers:
                    entry_source["anime_title"] = parsed.headers["anime_title"]
                ref = parsed.headers.get("Referer") or parsed.headers.get("referer")
                if ref:
                    entry_source["referer"] = ref
            if "anime_url" in entry_source or "anime_title" in entry_source:
                entry_source["added_by"] = "animepahe"
            
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
                        metadata=entry_source or None,
                        # Blank when the file named no queue, which lets add_download infer
                        # one from the source instead.
                        queue_id=self.queue_id_for_name(entry_queue) if entry_queue else "",
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
                    removed_lines = [raw_line for _, raw_line, is_download, success in entries_info if is_download and success]
                    self._append_to_backlog_backup(removed_lines)
                    with open(filepath, "w", encoding="utf-8") as f:
                        pass
                    log.info("Backlog file cleared completely: %s", filepath)
                elif successful_download_lines > 0:
                    # Some download entries succeeded, some failed; retain failed lines and comments
                    remaining_lines = [
                        raw_line for _, raw_line, is_download, success in entries_info
                        if not (is_download and success)
                    ]
                    removed_lines = [
                        raw_line for _, raw_line, is_download, success in entries_info
                        if is_download and success
                    ]
                    self._append_to_backlog_backup(removed_lines)
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.writelines(remaining_lines)
                    log.info("Backlog file updated: removed %d successful entries from %s", successful_download_lines, filepath)
            except Exception as exc:
                log.error("Failed to clear / update backlog file %s: %s", filepath, exc)

        return count

    def _append_to_backlog_backup(self, lines: list[str]) -> None:
        """Append removed backlog lines to data_dir/backlog.backup.txt at the top."""
        if not lines:
            return
        if "PYTEST_CURRENT_TEST" in os.environ:
            return
        try:
            from my_idm.paths import data_dir
            backup_path = data_dir() / "backlog.backup.txt"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            # Read existing backup content
            existing_content = ""
            if backup_path.exists():
                with open(backup_path, "r", encoding="utf-8") as f:
                    existing_content = f.read()
            # Prepend new lines (with timestamp marker)
            from datetime import datetime
            timestamp = datetime.now().isoformat()
            new_content = f"# Backed up {timestamp}\n" + "".join(lines)
            if existing_content:
                new_content += "\n" + existing_content
            with open(backup_path, "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception as exc:
            log.debug("Failed to write backlog backup: %s", exc)

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

        if total_added > 0:
            from my_idm.notifications import notify_backlog_downloads_picked
            notify_backlog_downloads_picked(total_added)

        return total_added

    # -- query ---------------------------------------------------------------

    def get_all_entries(self) -> list[DownloadEntry]:
        return self._db.get_all_downloads()

    def find_by_url(self, url: str) -> Optional[DownloadEntry]:
        """The entry for *url*, or ``None``. Unlike :meth:`get_entry` this is a plain lookup
        with no engine status merge, so it is cheap enough to call per row."""
        return self._db.find_by_url(url)

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
        elif entry.download_type == "http":
            segments = self._db.get_segments(download_id)
            if segments:
                seg_dl = sum(s.downloaded_bytes for s in segments)
                if seg_dl > entry.downloaded_size:
                    entry.downloaded_size = seg_dl
            elif entry.file_path and Path(entry.file_path).exists():
                try:
                    f_size = Path(entry.file_path).stat().st_size
                    if f_size > entry.downloaded_size:
                        entry.downloaded_size = f_size
                except OSError:
                    pass
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
        # Record bandwidth delta
        prev = self._last_progress_bytes.get(download_id)
        self._last_progress_bytes[download_id] = downloaded
        delta = (downloaded - prev) if (prev is not None and downloaded > prev) else 0
        if delta > 0:
            self.record_bandwidth_usage(download_id, downloaded_bytes=delta)

    def _on_http_status(self, download_id: str, status: str,
                          error_msg: str):
        self._starting_downloads.discard(download_id)
        current = self._db.get_download(download_id)
        if current and current.status in ("paused", "stopped", "suspended") and status in ("queued", "downloading"):
            log.debug("Ignoring status %s for %s download %s", status, current.status, download_id)
            return
        if current and current.status != status and status in ("downloading", "completed", "paused", "stopped", "error"):
            self._db.update_status(download_id, status)
            if status != "downloading":
                self._last_progress_bytes.pop(download_id, None)
        if (
            status == "completed"
            and self._security_config.scan_after_download
            and self._security_config.scan_timing == "after_complete"
        ):
            self._handle_completed_scan(download_id)
        else:
            self.status_changed.emit(download_id, status, error_msg)
        if status in ("completed", "paused", "stopped", "error"):
            self._last_progress_bytes.pop(download_id, None)
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
        # Record bandwidth delta (download + upload)
        prev_dl = self._last_progress_bytes.get(download_id)
        self._last_progress_bytes[download_id] = downloaded
        delta_dl = (downloaded - prev_dl) if (prev_dl is not None and downloaded > prev_dl) else 0
        delta_ul = int(upload_speed) if upload_speed > 0 else 0
        if delta_dl > 0 or delta_ul > 0:
            self.record_bandwidth_usage(download_id, downloaded_bytes=delta_dl, uploaded_bytes=delta_ul)

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
            self._last_progress_bytes.pop(download_id, None)
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
                verdict, report = scan_file(entry.file_path, self._security_config)
                meta = entry.metadata
                # Tri-state, not boolean: `None` means no scanner could run, which is neither a
                # clean bill of health nor a threat. Recording it as scanned would make the
                # details panel render a green "Clean" for a file nothing inspected.
                meta["antivirus_scanned"] = verdict is not None
                meta["antivirus_report"] = report

                if verdict is False:
                    meta["threat_detected"] = True
                    if self._security_config.action_on_threat == "delete":
                        quarantine_or_delete_file(entry.file_path)
                        report += " (Infected file deleted)"
                    entry.metadata = meta
                    self._db.update_download(entry)
                    self._db.update_status(download_id, "threat_detected", report)
                    self.status_changed.emit(download_id, "threat_detected", report)
                    self.threat_detected.emit(download_id, report)
                    return

                if verdict is None:
                    # Completed, but unscanned. The download still finishes - refusing to finish
                    # because no antivirus happens to be installed would be a worse failure than
                    # the one being guarded against - and the panel reports it as not scanned.
                    meta["antivirus_scan_error"] = report

                final_status = "seeding" if target_seeding else "completed"
                entry.metadata = meta
                self._db.update_download(entry)
                self._db.update_status(download_id, final_status)
                if final_status == "seeding":
                    h = self._torrent._handles.get(download_id)
                    if h:
                        self._torrent._apply_seeding_limit_to_handle(h)
                self.status_changed.emit(download_id, final_status, report)
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
            verdict, report = scan_file(entry.file_path, self._security_config)
            meta = entry.metadata
            # Tri-state: see the sibling rescan path in `_on_download_complete`.
            meta["antivirus_scanned"] = verdict is not None
            meta["antivirus_report"] = report

            if verdict is False:
                meta["threat_detected"] = True
                if self._security_config.action_on_threat == "delete":
                    quarantine_or_delete_file(entry.file_path)
                    report += " (Infected file deleted)"
                entry.metadata = meta
                self._db.update_download(entry)
                self._db.update_status(download_id, "threat_detected", report)
                self.status_changed.emit(download_id, "threat_detected", report)
                self.threat_detected.emit(download_id, report)
                return

            if verdict is None:
                meta["antivirus_scan_error"] = report

            entry.metadata = meta
            self._db.update_download(entry)
            target_status = original_status if original_status in ("paused", "queued", "downloading", "seeding") else "completed"
            if entry.total_size > 0 and entry.downloaded_size < entry.total_size and target_status == "completed":
                target_status = "paused"
            self._db.update_status(download_id, target_status)
            self.status_changed.emit(download_id, target_status, report)

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
        with self._timer_slot():
            self._torrent.poll_all()

    def _process_retry_queue(self):
        """Re-start any downloads that are queued for retry."""
        with self._timer_slot():
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

            self._enforce_scheduler()
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
