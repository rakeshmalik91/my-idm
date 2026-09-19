"""Central download manager orchestrating engines, database, and GUI signals."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal, QTimer

from my_idm.database import Database, DownloadEntry, _now_iso
from my_idm.http_engine import HTTPEngine
from my_idm.torrent_engine import TorrentEngine

log = logging.getLogger(__name__)

DEFAULT_SAVE_PATH = str(Path.home() / "Downloads")


class DownloadManager(QObject):
    """Coordinates downloads between HTTP/Torrent engines, DB, and GUI."""

    # Signals for the GUI
    progress_updated = Signal(str, int, int, float, float, int, int, float)
    # download_id, downloaded, total, speed, eta, seeds, peers, upload_speed
    status_changed = Signal(str, str, str)
    # download_id, status, error_message
    download_added = Signal(str)    # download_id
    download_removed = Signal(str)  # download_id
    download_moved = Signal(str)    # download_id

    def __init__(self, db: Database, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._db = db
        self._http = HTTPEngine(db)
        self._torrent = TorrentEngine(db)

        # asyncio event loop runs in a background thread
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

        # Torrent poll timer (runs in Qt main thread)
        self._torrent_timer = QTimer(self)
        self._torrent_timer.setInterval(1000)  # 1 second
        self._torrent_timer.timeout.connect(self._poll_torrents)

        # Retry timer — checks queued items periodically
        self._retry_timer = QTimer(self)
        self._retry_timer.setInterval(10_000)  # 10 seconds
        self._retry_timer.timeout.connect(self._process_retry_queue)

        # Wire engine callbacks
        self._http.set_callbacks(
            self._on_http_progress, self._on_http_status,
        )
        self._torrent.set_callbacks(
            self._on_torrent_progress, self._on_torrent_status,
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

        log.info("DownloadManager started")

    def stop(self):
        """Shut down everything cleanly."""
        self._torrent_timer.stop()
        self._retry_timer.stop()

        # Stop HTTP engine
        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self._http.stop(), self._loop
            )
            try:
                future.result(timeout=10)
            except Exception:
                pass

        # Stop torrent engine
        self._torrent.stop()

        # Stop asyncio loop
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

        log.info("DownloadManager stopped")

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # -- add downloads -------------------------------------------------------

    def add_download(self, url: str, save_path: str = "",
                     num_segments: int = 8) -> Optional[str]:
        """Add a new download. Returns download_id or None if duplicate resumed."""
        url = url.strip()
        if not url:
            return None

        if not save_path:
            save_path = DEFAULT_SAVE_PATH

        download_type = self._detect_type(url)

        # Deduplication
        existing = self._db.find_by_url(url)
        if existing:
            if existing.status in ("completed", "seeding"):
                log.info("URL already completed: %s", url)
                return None  # Already done
            if existing.status in ("queued", "paused", "error"):
                log.info("Resuming existing download: %s", existing.id)
                self.resume_download(existing.id)
                return existing.id
            # Already downloading
            return existing.id

        # Create new entry
        entry = DownloadEntry(
            id=str(uuid.uuid4()),
            url=url,
            save_path=save_path,
            download_type=download_type,
            num_segments=num_segments,
            added_at=_now_iso(),
            status="queued",
        )

        # For .torrent file paths, extract filename
        if download_type == "torrent" and os.path.isfile(url):
            entry.filename = Path(url).stem

        self._db.add_download(entry)
        self.download_added.emit(entry.id)

        # Start it
        self._start_entry(entry)

        return entry.id

    def _start_entry(self, entry: DownloadEntry):
        """Dispatch download to the right engine."""
        if entry.download_type == "http":
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._http.add(entry), self._loop
                )
        elif entry.download_type == "torrent":
            if not self._torrent.available:
                self._db.update_status(
                    entry.id, "error",
                    "libtorrent not installed — torrent support disabled",
                )
                self.status_changed.emit(
                    entry.id, "error",
                    "libtorrent not installed — torrent support disabled",
                )
                return
            self._torrent.add_torrent(entry)

    # -- pause / resume / delete ---------------------------------------------

    def pause_download(self, download_id: str):
        entry = self._db.get_download(download_id)
        if not entry:
            return

        if entry.download_type == "http":
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._http.pause(download_id), self._loop
                )
        elif entry.download_type == "torrent":
            self._torrent.pause(download_id)

        self._db.update_status(download_id, "paused")
        self.status_changed.emit(download_id, "paused", "")

    def resume_download(self, download_id: str):
        entry = self._db.get_download(download_id)
        if not entry:
            return

        if entry.status == "completed":
            return

        entry.status = "queued"
        entry.last_tried_at = _now_iso()
        self._db.update_download(entry)

        if entry.download_type == "http":
            if not self._http.is_active(download_id):
                if self._loop:
                    asyncio.run_coroutine_threadsafe(
                        self._http.add(entry), self._loop
                    )
        elif entry.download_type == "torrent":
            if download_id in self._torrent._handles:
                self._torrent.resume(download_id)
            else:
                self._torrent.add_torrent(entry)

        self.status_changed.emit(download_id, "downloading", "")

    def delete_download(self, download_id: str, delete_files: bool = False):
        entry = self._db.get_download(download_id)
        if not entry:
            return

        # Stop active download
        if entry.download_type == "http":
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._http.cancel(download_id), self._loop
                )
        elif entry.download_type == "torrent":
            self._torrent.remove(download_id, delete_files)

        # Delete files if requested (for HTTP or if torrent didn't handle it)
        if delete_files and entry.download_type == "http":
            fp = Path(entry.file_path)
            if fp.exists():
                try:
                    if fp.is_dir():
                        shutil.rmtree(fp)
                    else:
                        fp.unlink()
                except OSError as exc:
                    log.warning("Failed to delete file %s: %s", fp, exc)

        # Remove from DB
        self._db.delete_segments(download_id)
        self._db.delete_download(download_id)
        self.download_removed.emit(download_id)

    # -- move ----------------------------------------------------------------

    def move_download(self, download_id: str, new_save_path: str):
        entry = self._db.get_download(download_id)
        if not entry:
            return

        old_file_path = Path(entry.file_path)
        new_file_path = Path(new_save_path) / old_file_path.name

        if entry.download_type == "torrent":
            # libtorrent handles this natively
            self._torrent.move_storage(download_id, new_save_path)
            self._db.move_download(
                download_id, new_save_path, str(new_file_path)
            )
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

            # Move the file
            if old_file_path.exists():
                new_file_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_file_path), str(new_file_path))

            self._db.move_download(
                download_id, new_save_path, str(new_file_path)
            )

            # Resume if it was active
            if was_active:
                entry = self._db.get_download(download_id)
                if entry:
                    if self._loop:
                        asyncio.run_coroutine_threadsafe(
                            self._http.add(entry), self._loop
                        )

        self.download_moved.emit(download_id)

    # -- recheck -------------------------------------------------------------

    def recheck_download(self, download_id: str):
        entry = self._db.get_download(download_id)
        if not entry:
            return

        if entry.download_type == "torrent":
            self._torrent.recheck(download_id)
            self.status_changed.emit(download_id, "checking", "")
        else:
            # HTTP: compare file size vs expected
            fp = Path(entry.file_path)
            if fp.exists():
                actual_size = fp.stat().st_size
                if entry.total_size > 0 and actual_size >= entry.total_size:
                    self._db.update_status(download_id, "completed")
                    self._db.update_progress(download_id, actual_size)
                    self.status_changed.emit(download_id, "completed", "")
                else:
                    entry.downloaded_size = actual_size
                    self._db.update_download(entry)
                    self.status_changed.emit(
                        download_id, entry.status,
                        f"File size: {actual_size} / {entry.total_size}",
                    )
            else:
                # File doesn't exist — reset progress
                entry.downloaded_size = 0
                entry.status = "queued"
                self._db.update_download(entry)
                self._db.delete_segments(download_id)
                self.status_changed.emit(download_id, "queued", "File not found")

    # -- backlog -------------------------------------------------------------

    def load_backlog(self, filepath: str) -> int:
        """Load URLs from a backlog file. Returns count of newly added."""
        count = 0
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    result = self.add_download(line)
                    if result:
                        count += 1
        except FileNotFoundError:
            log.warning("Backlog file not found: %s", filepath)
        except Exception as exc:
            log.error("Failed to load backlog %s: %s", filepath, exc)
        log.info("Loaded %d downloads from backlog %s", count, filepath)
        return count

    # -- query ---------------------------------------------------------------

    def get_all_entries(self) -> list[DownloadEntry]:
        return self._db.get_all_downloads()

    def get_entry(self, download_id: str) -> Optional[DownloadEntry]:
        return self._db.get_download(download_id)

    # -- engine callbacks (called from async / background threads) -----------

    def _on_http_progress(self, download_id: str, downloaded: int,
                          total: int, speed: float, eta: float):
        self.progress_updated.emit(
            download_id, downloaded, total, speed, eta, 0, 0, 0.0
        )

    def _on_http_status(self, download_id: str, status: str,
                        error_msg: str):
        self.status_changed.emit(download_id, status, error_msg)

    def _on_torrent_progress(self, download_id: str, downloaded: int,
                             total: int, speed: float, eta: float,
                             seeds: int, peers: int, upload_speed: float):
        self.progress_updated.emit(
            download_id, downloaded, total, speed, eta,
            seeds, peers, upload_speed,
        )

    def _on_torrent_status(self, download_id: str, status: str,
                           error_msg: str):
        self.status_changed.emit(download_id, status, error_msg)

    # -- periodic callbacks --------------------------------------------------

    def _poll_torrents(self):
        self._torrent.poll_all()

    def _process_retry_queue(self):
        """Re-start any downloads that are queued for retry."""
        for entry in self._db.get_all_downloads():
            if entry.status == "queued" and entry.retry_count > 0:
                if entry.retry_count < entry.max_retries:
                    log.info(
                        "Auto-retrying %s (attempt %d/%d)",
                        entry.id, entry.retry_count + 1, entry.max_retries,
                    )
                    self._start_entry(entry)

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _detect_type(url: str) -> str:
        url_lower = url.lower().strip()
        if url_lower.startswith("magnet:"):
            return "torrent"
        if url_lower.endswith(".torrent") and os.path.isfile(url):
            return "torrent"
        return "http"
