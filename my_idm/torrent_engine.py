"""Torrent download engine wrapping libtorrent."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional, Callable

from my_idm.database import Database, DownloadEntry

log = logging.getLogger(__name__)

# Try to import libtorrent; provide a clear error if missing
try:
    import libtorrent as lt

    _HAS_LIBTORRENT = True
except ImportError:
    _HAS_LIBTORRENT = False
    lt = None  # type: ignore

FASTRESUME_DIR = Path.home() / ".my-idm" / "fastresume"

ProgressCallback = Callable[
    [str, int, int, float, float, int, int, float],
    # download_id, downloaded, total, speed, eta, seeds, peers, upload_speed
    None
]
StatusCallback = Callable[[str, str, str], None]  # id, status, error


# Map libtorrent state enum to human-readable strings
_STATE_NAMES = {
    0: "queued_for_checking",
    1: "checking_files",
    2: "downloading_metadata",
    3: "downloading",
    4: "finished",
    5: "seeding",
    6: "allocating",
}


class TorrentEngine:
    """Manages torrent downloads via libtorrent."""

    def __init__(self, db: Database):
        self._db = db
        self._session: Optional[object] = None  # lt.session
        self._handles: dict[str, object] = {}   # download_id → lt.torrent_handle
        self._progress_cb: Optional[ProgressCallback] = None
        self._status_cb: Optional[StatusCallback] = None
        self._filename_cb: Optional[Callable[[str, str], None]] = None
        self._running = False

    @property
    def available(self) -> bool:
        return _HAS_LIBTORRENT

    def set_callbacks(self, progress_cb: ProgressCallback,
                      status_cb: StatusCallback,
                      filename_cb: Optional[Callable[[str, str], None]] = None):
        self._progress_cb = progress_cb
        self._status_cb = status_cb
        self._filename_cb = filename_cb

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        if not _HAS_LIBTORRENT:
            log.warning("libtorrent not installed — torrent support disabled")
            return

        params = lt.session_params()
        params.settings = lt.default_settings()
        params.settings["alert_mask"] = (
            lt.alert_category.status
            | lt.alert_category.error
            | lt.alert_category.storage
        )
        self._session = lt.session(params)

        FASTRESUME_DIR.mkdir(parents=True, exist_ok=True)
        self._running = True
        log.info("Torrent engine started")

    def stop(self):
        self._running = False
        if not self._session:
            return

        # Save resume data for all handles
        for did, handle in list(self._handles.items()):
            self._save_resume_data(did, handle)

        del self._session
        self._session = None
        self._handles.clear()
        log.info("Torrent engine stopped")

    # -- public API ----------------------------------------------------------

    def add_torrent(self, entry: DownloadEntry) -> bool:
        """Add a torrent by magnet URI or .torrent file path.

        Returns True on success, False if libtorrent unavailable.
        """
        if not _HAS_LIBTORRENT or not self._session:
            log.error("Cannot add torrent — libtorrent not available")
            return False

        url = entry.url
        save_path = entry.save_path

        params = lt.add_torrent_params()
        params.save_path = save_path

        # Load fastresume if available
        resume_path = FASTRESUME_DIR / f"{entry.id}.fastresume"
        if resume_path.exists():
            try:
                with open(resume_path, "rb") as f:
                    params.resume_data = list(f.read())
                log.info("Loaded fastresume for %s", entry.id)
            except Exception as exc:
                log.warning("Failed to load fastresume: %s", exc)

        if url.startswith("magnet:"):
            params = lt.parse_magnet_uri(url)
            params.save_path = save_path
            # Re-apply fastresume after magnet parse
            if resume_path.exists():
                try:
                    with open(resume_path, "rb") as f:
                        params.resume_data = list(f.read())
                except Exception:
                    pass
        elif os.path.isfile(url):
            ti = lt.torrent_info(url)
            params.ti = ti
        else:
            log.error("Invalid torrent source: %s", url)
            return False

        handle = self._session.add_torrent(params)
        self._handles[entry.id] = handle

        # Extract info hash
        try:
            info_hash = str(handle.info_hash())
            if info_hash and not entry.torrent_info_hash:
                entry.torrent_info_hash = info_hash
                self._db.update_download(entry)
        except Exception:
            pass

        self._db.update_status(entry.id, "downloading")
        if self._status_cb:
            self._status_cb(entry.id, "downloading", "")

        log.info("Added torrent: %s", entry.filename or url)
        return True

    def pause(self, download_id: str):
        handle = self._handles.get(download_id)
        if handle:
            handle.pause()
            self._save_resume_data(download_id, handle)
            self._db.update_status(download_id, "paused")
            if self._status_cb:
                self._status_cb(download_id, "paused", "")

    def resume(self, download_id: str):
        handle = self._handles.get(download_id)
        if handle:
            handle.resume()
            self._db.update_status(download_id, "downloading")
            if self._status_cb:
                self._status_cb(download_id, "downloading", "")

    def remove(self, download_id: str, delete_files: bool = False):
        handle = self._handles.pop(download_id, None)
        if handle and self._session:
            if delete_files:
                self._session.remove_torrent(handle, lt.options_t.delete_files)
            else:
                self._session.remove_torrent(handle)
        # Clean up fastresume
        resume_path = FASTRESUME_DIR / f"{download_id}.fastresume"
        if resume_path.exists():
            resume_path.unlink(missing_ok=True)

    def recheck(self, download_id: str):
        handle = self._handles.get(download_id)
        if handle:
            handle.force_recheck()
            log.info("Rechecking torrent %s", download_id)

    def move_storage(self, download_id: str, new_path: str):
        handle = self._handles.get(download_id)
        if handle:
            handle.move_storage(new_path)
            log.info("Moving torrent %s storage to %s", download_id, new_path)

    def get_status(self, download_id: str) -> Optional[dict]:
        """Get current torrent status for polling."""
        handle = self._handles.get(download_id)
        if not handle:
            return None

        try:
            s = handle.status()
            total_size = s.total_wanted
            downloaded = s.total_wanted_done
            progress = s.progress * 100
            state_idx = int(s.state)
            state_name = _STATE_NAMES.get(state_idx, str(s.state))
            speed = s.download_rate
            upload_speed = s.upload_rate
            seeds = s.num_seeds
            peers = s.num_peers
            eta = (
                (total_size - downloaded) / speed
                if speed > 0 else 0
            )

            # Get name from torrent info if available
            name = ""
            if s.has_metadata:
                ti = handle.get_torrent_info()
                if ti:
                    name = ti.name()

            return {
                "total_size": total_size,
                "downloaded": downloaded,
                "progress": progress,
                "state": state_name,
                "speed": speed,
                "upload_speed": upload_speed,
                "seeds": seeds,
                "peers": peers,
                "eta": eta,
                "name": name,
            }
        except Exception as exc:
            log.debug("Failed to get status for %s: %s", download_id, exc)
            return None

    def poll_all(self):
        """Poll all active torrents and emit progress/status callbacks.

        This should be called periodically from a timer.
        """
        if not self._session or not self._running:
            return

        for download_id, handle in list(self._handles.items()):
            status = self.get_status(download_id)
            if not status:
                continue

            entry = self._db.get_download(download_id)
            if not entry:
                continue

            # Update DB
            entry.total_size = status["total_size"]
            entry.downloaded_size = status["downloaded"]
            resolved_name = status.get("name")
            if resolved_name and resolved_name != entry.filename:
                entry.filename = resolved_name
                entry.file_path = str(
                    Path(entry.save_path) / entry.filename
                )
                self._db.update_download(entry)
                if self._filename_cb:
                    self._filename_cb(download_id, entry.filename)
            else:
                self._db.update_download(entry)

            # Emit progress
            if self._progress_cb:
                self._progress_cb(
                    download_id,
                    status["downloaded"],
                    status["total_size"],
                    status["speed"],
                    status["eta"],
                    status["seeds"],
                    status["peers"],
                    status["upload_speed"],
                )

            # Check for completion
            state = status["state"]
            if state in ("finished", "seeding"):
                if entry.status != "completed" and entry.status != "seeding":
                    self._db.update_status(download_id, "completed")
                    if self._status_cb:
                        self._status_cb(download_id, "completed", "")
                    self._save_resume_data(download_id, handle)

    # -- internal ------------------------------------------------------------

    def _save_resume_data(self, download_id: str, handle):
        try:
            if not handle.is_valid():
                return
            handle.save_resume_data()
            # Poll alerts to get the resume data
            alerts = self._session.pop_alerts()
            for alert in alerts:
                if isinstance(alert, lt.save_resume_data_alert):
                    resume_path = FASTRESUME_DIR / f"{download_id}.fastresume"
                    with open(resume_path, "wb") as f:
                        f.write(lt.write_resume_data_buf(alert.params))
                    log.debug("Saved resume data for %s", download_id)
        except Exception as exc:
            log.debug("Failed to save resume data for %s: %s",
                      download_id, exc)
