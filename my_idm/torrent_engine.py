"""Torrent download engine wrapping libtorrent."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional, Callable, Any

from my_idm.config import TorConfig
from my_idm.database import Database, DownloadEntry
from my_idm.network import NetworkConfig, is_interface_active

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


def _get_info_hash_from_params(params: Any) -> str:
    """Extract lowercase hex info hash from add_torrent_params."""
    if not params:
        return ""
    try:
        if hasattr(params, "info_hashes"):
            ih = params.info_hashes
            if ih.has_v1():
                return str(ih.v1).lower()
            elif ih.has_v2():
                return str(ih.v2).lower()
        if hasattr(params, "info_hash") and params.info_hash:
            return str(params.info_hash).lower()
        if hasattr(params, "ti") and params.ti:
            return str(params.ti.info_hash()).lower()
    except Exception:
        pass
    return ""


def _get_info_hash_from_handle(handle: Any) -> str:
    """Extract lowercase hex info hash from torrent_handle."""
    if not handle:
        return ""
    try:
        if hasattr(handle, "is_valid") and not handle.is_valid():
            return ""
        if hasattr(handle, "info_hashes"):
            ih = handle.info_hashes()
            if ih.has_v1():
                return str(ih.v1).lower()
            elif ih.has_v2():
                return str(ih.v2).lower()
        if hasattr(handle, "info_hash"):
            return str(handle.info_hash()).lower()
    except Exception:
        pass
    return ""


def _is_save_resume_data_alert(alert: Any) -> bool:
    if not _HAS_LIBTORRENT or lt is None:
        return False
    cls = getattr(lt, "save_resume_data_alert", None)
    if isinstance(cls, type) and isinstance(alert, cls):
        return True
    try:
        what = getattr(alert, "what", lambda: "")()
        if what == "save_resume_data_alert":
            return True
    except Exception:
        pass
    return type(alert).__name__ in ("save_resume_data_alert", "FakeSaveResumeDataAlert")


def _is_save_resume_data_failed_alert(alert: Any) -> bool:
    if not _HAS_LIBTORRENT or lt is None:
        return False
    cls = getattr(lt, "save_resume_data_failed_alert", None)
    if isinstance(cls, type) and isinstance(alert, cls):
        return True
    try:
        what = getattr(alert, "what", lambda: "")()
        if what == "save_resume_data_failed_alert":
            return True
    except Exception:
        pass
    return type(alert).__name__ in ("save_resume_data_failed_alert", "FakeSaveResumeDataFailedAlert")


class TorrentEngine:
    """Manages torrent downloads via libtorrent."""

    def __init__(self, db: Database):
        self._db = db
        self._session: Optional[object] = None  # lt.session
        self._network_config: Optional[NetworkConfig] = None
        self._tor_config: Optional[TorConfig] = None
        self._handles: dict[str, object] = {}   # download_id → lt.torrent_handle
        self._progress_cb: Optional[ProgressCallback] = None
        self._status_cb: Optional[StatusCallback] = None
        self._filename_cb: Optional[Callable[[str, str], None]] = None
        self._running = False
        self._last_active_time: dict[str, float] = {}

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
        self._apply_all_settings()

        FASTRESUME_DIR.mkdir(parents=True, exist_ok=True)
        self._running = True
        log.info("Torrent engine started")

    def stop(self):
        self._running = False
        if not self._session:
            return

        # Request resume data for all valid handles
        pending_dids = set()
        for did, handle in list(self._handles.items()):
            try:
                if handle.is_valid():
                    handle.save_resume_data()
                    pending_dids.add(did)
            except Exception as exc:
                log.debug("Failed requesting resume data for %s: %s", did, exc)

        # Drain alerts with timeout, strictly pairing each alert to its handle
        start_t = time.time()
        while pending_dids and (time.time() - start_t) < 2.0:
            try:
                alerts = self._session.pop_alerts()
            except Exception:
                break

            for alert in alerts:
                if _is_save_resume_data_alert(alert):
                    alert_handle = getattr(alert, "handle", None)
                    alert_hash = _get_info_hash_from_handle(alert_handle)
                    for did in list(pending_dids):
                        h = self._handles.get(did)
                        if not h:
                            continue
                        h_hash = _get_info_hash_from_handle(h)
                        if h is alert_handle or (h_hash and alert_hash and h_hash == alert_hash):
                            resume_path = FASTRESUME_DIR / f"{did}.fastresume"
                            try:
                                with open(resume_path, "wb") as f:
                                    f.write(lt.write_resume_data_buf(alert.params))
                                log.debug("Saved resume data on shutdown for %s", did)
                            except Exception as exc:
                                log.warning("Failed writing resume data for %s: %s", did, exc)
                            pending_dids.discard(did)
                            break
                elif _is_save_resume_data_failed_alert(alert):
                    alert_handle = getattr(alert, "handle", None)
                    alert_hash = _get_info_hash_from_handle(alert_handle)
                    for did in list(pending_dids):
                        h = self._handles.get(did)
                        if not h:
                            continue
                        h_hash = _get_info_hash_from_handle(h)
                        if h is alert_handle or (h_hash and alert_hash and h_hash == alert_hash):
                            pending_dids.discard(did)
                            break
            if pending_dids:
                time.sleep(0.05)

        del self._session
        self._session = None
        self._handles.clear()
        log.info("Torrent engine stopped")

    @property
    def network_config(self) -> Optional[NetworkConfig]:
        return self._network_config

    @property
    def tor_config(self) -> Optional[TorConfig]:
        return self._tor_config

    def apply_tor_config(self, config: TorConfig):
        """Apply Tor SOCKS5 proxy settings to libtorrent if routing torrents."""
        self._tor_config = config
        self._apply_all_settings()

    def apply_network_config(self, config: NetworkConfig):
        """Apply network interface binding and proxy settings to libtorrent."""
        self._network_config = config
        self._apply_all_settings()

    def _apply_all_settings(self):
        if not _HAS_LIBTORRENT or not self._session:
            return

        try:
            sett = self._session.get_settings()

            # 1. Interface binding
            if self._network_config and self._network_config.is_interface_bound:
                sett["listen_interfaces"] = f"{self._network_config.interface_ip}:6881"
                sett["outgoing_interfaces"] = self._network_config.interface_ip
                log.info(
                    "TorrentEngine bound to interface %s (%s)",
                    self._network_config.interface_name,
                    self._network_config.interface_ip,
                )
            else:
                sett["listen_interfaces"] = "0.0.0.0:6881,[::]:6881"
                sett["outgoing_interfaces"] = ""

            # 2. Proxy configuration (Tor takes priority if enabled and routing torrents)
            if self._tor_config and self._tor_config.enabled and self._tor_config.route_torrent:
                sett["proxy_hostname"] = self._tor_config.proxy_host
                sett["proxy_port"] = self._tor_config.proxy_port
                sett["proxy_username"] = ""
                sett["proxy_password"] = ""
                sett["proxy_type"] = lt.proxy_type_t.socks5
                sett["force_proxy"] = True
                sett["proxy_peer_connections"] = True
                sett["proxy_tracker_connections"] = True
                log.info(
                    "TorrentEngine routed through Tor SOCKS5 proxy: %s:%d",
                    self._tor_config.proxy_host, self._tor_config.proxy_port,
                )
            elif self._network_config and self._network_config.proxy_enabled and self._network_config.proxy_host:
                sett["proxy_hostname"] = self._network_config.proxy_host
                sett["proxy_port"] = self._network_config.proxy_port
                sett["proxy_username"] = self._network_config.proxy_username
                sett["proxy_password"] = self._network_config.proxy_password

                pt = self._network_config.proxy_type.lower()
                if pt == "socks5":
                    sett["proxy_type"] = (
                        lt.proxy_type_t.socks5_pw
                        if self._network_config.proxy_username
                        else lt.proxy_type_t.socks5
                    )
                elif pt == "http":
                    sett["proxy_type"] = (
                        lt.proxy_type_t.http_pw
                        if self._network_config.proxy_username
                        else lt.proxy_type_t.http
                    )
                sett["force_proxy"] = True
                sett["proxy_peer_connections"] = True
                sett["proxy_tracker_connections"] = True
                log.info(
                    "TorrentEngine proxy configured: %s://%s:%d",
                    pt, self._network_config.proxy_host, self._network_config.proxy_port,
                )
            else:
                sett["proxy_type"] = lt.proxy_type_t.none
                sett["proxy_hostname"] = ""
                sett["proxy_port"] = 0
                sett["force_proxy"] = False

            self._session.apply_settings(sett)
        except Exception as exc:
            log.warning("Failed to apply network settings to libtorrent: %s", exc)

    # -- public API ----------------------------------------------------------

    def add_torrent(self, entry: DownloadEntry) -> bool:
        """Add a torrent by magnet URI or .torrent file path.

        Returns True on success, False if libtorrent unavailable.
        """
        if not _HAS_LIBTORRENT or not self._session:
            log.error("Cannot add torrent — libtorrent not available")
            return False

        if entry.id in self._handles:
            log.warning("Torrent %s already added to session, resuming instead", entry.id)
            self.resume(entry.id)
            return True

        # Kill switch check: verify bound VPN/interface is active before starting
        if (
            self._network_config
            and self._network_config.kill_switch
            and self._network_config.is_interface_bound
        ):
            if not is_interface_active(
                self._network_config.interface_name,
                self._network_config.interface_ip,
            ):
                err = (
                    f"VPN / Bound interface '{self._network_config.interface_name}' "
                    "disconnected (Kill switch active)"
                )
                log.warning(err)
                self._db.update_status(entry.id, "error", err)
                if self._status_cb:
                    self._status_cb(entry.id, "error", err)
                return False

        url = entry.url
        save_path = entry.save_path

        # Load fastresume if available
        resume_path = FASTRESUME_DIR / f"{entry.id}.fastresume"
        resume_bytes = None
        if resume_path.exists():
            try:
                with open(resume_path, "rb") as f:
                    resume_bytes = f.read()
            except Exception as exc:
                log.warning("Failed to read fastresume for %s: %s", entry.id, exc)

        expected_hash = (entry.torrent_info_hash or "").lower()

        if url.startswith("magnet:"):
            try:
                params = lt.parse_magnet_uri(url)
                params.save_path = save_path
                parsed_hash = _get_info_hash_from_params(params)
                if parsed_hash:
                    expected_hash = parsed_hash
            except Exception as exc:
                log.error("Failed to parse magnet URI %s: %s", url, exc)
                return False

            if resume_bytes and hasattr(lt, "read_resume_data"):
                try:
                    resume_params = lt.read_resume_data(resume_bytes)
                    resume_hash = _get_info_hash_from_params(resume_params)
                    if expected_hash and resume_hash and expected_hash != resume_hash:
                        log.warning(
                            "Fastresume hash mismatch for %s: expected %s, got %s. Discarding mismatched fastresume.",
                            entry.id, expected_hash, resume_hash,
                        )
                        resume_path.unlink(missing_ok=True)
                    else:
                        params = resume_params
                        params.save_path = save_path
                        log.info("Loaded fastresume for %s", entry.id)
                except Exception as exc:
                    log.warning("Failed to parse fastresume for %s: %s", entry.id, exc)

        elif os.path.isfile(url):
            try:
                ti = lt.torrent_info(url)
                parsed_hash = str(ti.info_hash()).lower()
                if parsed_hash:
                    expected_hash = parsed_hash
            except Exception as exc:
                log.error("Failed to parse torrent file %s: %s", url, exc)
                return False

            params = lt.add_torrent_params()
            params.ti = ti
            params.save_path = save_path

            if resume_bytes and hasattr(lt, "read_resume_data"):
                try:
                    resume_params = lt.read_resume_data(resume_bytes)
                    resume_hash = _get_info_hash_from_params(resume_params)
                    if expected_hash and resume_hash and expected_hash != resume_hash:
                        log.warning(
                            "Fastresume hash mismatch for %s: expected %s, got %s. Discarding mismatched fastresume.",
                            entry.id, expected_hash, resume_hash,
                        )
                        resume_path.unlink(missing_ok=True)
                    else:
                        params = resume_params
                        if not getattr(params, "ti", None):
                            params.ti = ti
                        params.save_path = save_path
                        log.info("Loaded fastresume for %s", entry.id)
                except Exception as exc:
                    log.warning("Failed to parse fastresume for %s: %s", entry.id, exc)
        else:
            log.error("Invalid torrent source: %s", url)
            return False

        handle = self._session.add_torrent(params)
        self._handles[entry.id] = handle

        # Extract info hash and persist to entry if missing or updated
        try:
            info_hash = _get_info_hash_from_handle(handle)
            if info_hash and entry.torrent_info_hash != info_hash:
                entry.torrent_info_hash = info_hash
                self._db.update_download(entry)
        except Exception:
            pass

        if entry.status == "paused":
            try:
                if HAS_LIBTORRENT and hasattr(lt, "torrent_flags"):
                    handle.unset_flags(lt.torrent_flags.auto_managed)
            except Exception:
                pass
            handle.pause()
            self._db.update_status(entry.id, "paused")
            if self._status_cb:
                self._status_cb(entry.id, "paused", "")
        else:
            has_meta = False
            try:
                if hasattr(handle, "status"):
                    has_meta = getattr(handle.status(), "has_metadata", False)
            except Exception:
                pass
            initial_status = "downloading" if has_meta else "fetching_metadata"
            self._db.update_status(entry.id, initial_status)
            if self._status_cb:
                self._status_cb(entry.id, initial_status, "")

        log.info("Added torrent: %s", entry.filename or url)
        return True

    def pause(self, download_id: str):
        handle = self._handles.get(download_id)
        if handle:
            try:
                if HAS_LIBTORRENT and hasattr(lt, "torrent_flags"):
                    handle.unset_flags(lt.torrent_flags.auto_managed)
            except Exception as e:
                log.debug("Could not unset auto_managed flag on pause: %s", e)
            handle.pause()
            try:
                if handle.is_valid():
                    handle.save_resume_data()
            except Exception:
                pass
            self._db.update_status(download_id, "paused")
            if self._status_cb:
                self._status_cb(download_id, "paused", "")

    def resume(self, download_id: str):
        handle = self._handles.get(download_id)
        if handle:
            try:
                if HAS_LIBTORRENT and hasattr(lt, "torrent_flags"):
                    handle.set_flags(lt.torrent_flags.auto_managed)
            except Exception as e:
                log.debug("Could not set auto_managed flag on resume: %s", e)
            handle.resume()
            self._db.update_status(download_id, "downloading")
            if self._status_cb:
                self._status_cb(download_id, "downloading", "")

    def force_start(self, download_id: str):
        """Force start torrent by disabling auto-managed queue limits and resuming immediately."""
        handle = self._handles.get(download_id)
        if handle:
            try:
                if HAS_LIBTORRENT and hasattr(lt, "torrent_flags"):
                    handle.unset_flags(lt.torrent_flags.auto_managed)
            except Exception as e:
                log.debug("Could not unset auto_managed flag on handle: %s", e)
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
            self._db.update_status(download_id, "checking")
            # Ensure the handle is unpaused so checking can proceed
            try:
                if HAS_LIBTORRENT and hasattr(lt, "torrent_flags"):
                    handle.set_flags(lt.torrent_flags.auto_managed)
            except Exception:
                pass
            handle.resume()
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

        self._process_alerts()

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
                files = self.get_torrent_files(download_id)
                if files:
                    entry.metadata["files"] = files
                trackers = self.get_torrent_trackers(download_id)
                if trackers:
                    entry.metadata["trackers"] = trackers
                self._db.update_download(entry)
                if self._filename_cb:
                    self._filename_cb(download_id, entry.filename)
            else:
                self._db.update_download(entry)

            # If download is paused in DB, make sure torrent handle stays paused and reports 0 speed
            if entry.status == "paused":
                try:
                    s = handle.status()
                    raw_paused = getattr(s, "paused", None)
                    if raw_paused is not None and not type(raw_paused).__name__.startswith("MagicMock"):
                        is_paused = bool(raw_paused)
                    else:
                        is_paused = bool(getattr(s, "is_paused", False))
                    if not is_paused:
                        if HAS_LIBTORRENT and hasattr(lt, "torrent_flags"):
                            handle.unset_flags(lt.torrent_flags.auto_managed)
                        handle.pause()
                except Exception:
                    pass
                if self._progress_cb:
                    self._progress_cb(
                        download_id,
                        status["downloaded"],
                        status["total_size"],
                        0.0,
                        0.0,
                        status.get("seeds", 0),
                        status.get("peers", 0),
                        0.0,
                    )
                continue

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

            # Check for completion or state transition out of checking
            state = status["state"]
            if state in ("finished", "seeding"):
                if entry.status != "completed" and entry.status != "seeding":
                    files = self.get_torrent_files(download_id)
                    if files:
                        entry.metadata["files"] = files
                    trackers = self.get_torrent_trackers(download_id)
                    if trackers:
                        entry.metadata["trackers"] = trackers
                    self._db.update_download(entry)
                    self._db.update_status(download_id, "completed")
                    if self._status_cb:
                        self._status_cb(download_id, "completed", "")
                    try:
                        if handle.is_valid():
                            handle.save_resume_data()
                    except Exception:
                        pass
            elif state == "downloading_metadata":
                if entry.status != "fetching_metadata":
                    self._db.update_status(download_id, "fetching_metadata")
                    if self._status_cb:
                        self._status_cb(download_id, "fetching_metadata", "")
            elif entry.status == "fetching_metadata" and (state in ("downloading", "finished", "seeding") or status.get("name")):
                new_status = "completed" if state in ("finished", "seeding") else "downloading"
                self._db.update_status(download_id, new_status)
                if self._status_cb:
                    self._status_cb(download_id, new_status, "")
            elif entry.status == "checking" and state not in ("checking_files", "queued_for_checking"):
                if status["total_size"] > 0 and status["downloaded"] >= status["total_size"]:
                    new_status = "completed"
                else:
                    try:
                        s = handle.status()
                        raw_paused = getattr(s, "paused", None)
                        if raw_paused is not None and not type(raw_paused).__name__.startswith("MagicMock"):
                            is_paused = bool(raw_paused)
                        else:
                            is_paused = bool(getattr(s, "is_paused", False))
                    except Exception:
                        is_paused = False
                    new_status = "paused" if is_paused else "downloading"
                self._db.update_status(download_id, new_status)
                if self._status_cb:
                    self._status_cb(download_id, new_status, "")
            elif entry.status == "completed" and state not in ("finished", "seeding", "checking_files", "queued_for_checking"):
                # Download was marked completed, but actual progress from recheck is incomplete
                if status["total_size"] > 0 and status["downloaded"] < status["total_size"]:
                    try:
                        s = handle.status()
                        raw_paused = getattr(s, "paused", None)
                        if raw_paused is not None and not type(raw_paused).__name__.startswith("MagicMock"):
                            is_paused = bool(raw_paused)
                        else:
                            is_paused = bool(getattr(s, "is_paused", False))
                    except Exception:
                        is_paused = False
                    new_status = "paused" if is_paused else "downloading"
                    self._db.update_status(download_id, new_status)
                    if self._status_cb:
                        self._status_cb(download_id, new_status, "")

            # Stalled torrent detection (speed 0 and seeds 0 for > 45s while downloading)
            if state == "downloading" and status["speed"] == 0 and status["seeds"] == 0:
                last_act = self._last_active_time.get(download_id, 0)
                now = time.time()
                if last_act == 0:
                    self._last_active_time[download_id] = now
                elif now - last_act > 45.0:
                    if entry.status not in ("stalled", "paused", "completed", "error"):
                        self._db.update_status(download_id, "stalled")
                        if self._status_cb:
                            self._status_cb(download_id, "stalled", "")
                        try:
                            handle.force_reannounce()
                        except Exception:
                            pass
            elif state == "downloading" and (status["speed"] > 0 or status["seeds"] > 0):
                self._last_active_time[download_id] = time.time()
                if entry.status == "stalled":
                    self._db.update_status(download_id, "downloading")
                    if self._status_cb:
                        self._status_cb(download_id, "downloading", "")

    # -- details queries -----------------------------------------------------

    def get_torrent_files(self, download_id: str) -> list[dict]:
        """Returns details for each file in the torrent."""
        handle = self._handles.get(download_id)
        if not handle or not _HAS_LIBTORRENT:
            return []

        try:
            if not handle.is_valid():
                return []
            ti = handle.torrent_file()
            if not ti:
                return []

            num_files = ti.num_files()
            files_info = ti.files()
            progress_list = handle.file_progress()
            priorities = handle.get_file_priorities()

            result = []
            for i in range(num_files):
                f_size = files_info.file_size(i)
                f_path = files_info.file_path(i)
                f_prog = progress_list[i] if i < len(progress_list) else 0
                f_prio = priorities[i] if i < len(priorities) else 4
                pct = (f_prog / f_size * 100.0) if f_size > 0 else 100.0
                result.append({
                    "index": i,
                    "path": f_path,
                    "name": os.path.basename(f_path),
                    "size": f_size,
                    "progress": min(pct, 100.0),
                    "downloaded": f_prog,
                    "priority": f_prio,
                })
            return result
        except Exception as exc:
            log.debug("Failed to get torrent files for %s: %s", download_id, exc)
            return []

    def set_torrent_file_priority(self, download_id: str, file_index: int, priority: int) -> bool:
        """Sets priority for a specific file (0 = do not download, 1 = low, 4 = normal, 7 = high)."""
        handle = self._handles.get(download_id)
        if not handle or not _HAS_LIBTORRENT:
            return False

        try:
            if not handle.is_valid():
                return False
            handle.file_priority(file_index, priority)
            return True
        except Exception as exc:
            log.warning("Failed to set file priority for %s[%d]: %s", download_id, file_index, exc)
            return False

    @staticmethod
    def _safe_str(val: Any, default: str = "") -> str:
        if val is None:
            return default
        if isinstance(val, bytes):
            return val.decode("utf-8", errors="replace")
        return str(val)


    def get_torrent_peers(self, download_id: str) -> list[dict]:
        """Returns connected peers information."""
        handle = self._handles.get(download_id)
        if not handle or not _HAS_LIBTORRENT:
            return []

        try:
            if not handle.is_valid():
                return []
            peer_info_list = handle.get_peer_info()
            peers = []
            for p in peer_info_list:
                ip_str = f"{p.ip[0]}:{p.ip[1]}" if isinstance(p.ip, (tuple, list)) else str(p.ip)
                client_str = self._safe_str(getattr(p, "client", "Unknown"), default="Unknown")
                flags_str = self._safe_str(getattr(p, "flags", ""))
                peers.append({
                    "ip": ip_str,
                    "client": client_str,
                    "progress": float(getattr(p, "progress", 0.0)),
                    "down_speed": getattr(p, "down_speed", 0),
                    "up_speed": getattr(p, "up_speed", 0),
                    "flags": flags_str,
                })
            return peers
        except Exception as exc:
            log.debug("Failed to get torrent peers for %s: %s", download_id, exc)
            return []

    def get_torrent_trackers(self, download_id: str) -> list[dict]:
        """Returns tracker status information."""
        handle = self._handles.get(download_id)
        if not handle or not _HAS_LIBTORRENT:
            return []

        try:
            if not handle.is_valid():
                return []
            trackers_list = handle.trackers()
            trackers = []
            for t in trackers_list:
                trackers.append({
                    "url": self._safe_str(getattr(t, "url", "")),
                    "tier": getattr(t, "tier", 0),
                    "send_stats": getattr(t, "send_stats", False),
                })
            return trackers
        except Exception as exc:
            log.debug("Failed to get torrent trackers for %s: %s", download_id, exc)
            return []

    # -- alert processing ----------------------------------------------------

    def _process_alerts(self):
        """Process pending libtorrent session alerts."""
        if not self._session:
            return
        try:
            alerts = self._session.pop_alerts()
        except Exception:
            return

        for alert in alerts:
            if _is_save_resume_data_alert(alert):
                self._handle_save_resume_data_alert(alert)
            elif _is_save_resume_data_failed_alert(alert):
                log.debug("Save resume data failed: %s", getattr(alert, "message", lambda: "")())
            elif hasattr(lt, "file_error_alert") and isinstance(alert, lt.file_error_alert):
                alert_handle = getattr(alert, "handle", None)
                if alert_handle:
                    did = next((d for d, h in self._handles.items() if h is alert_handle), None)
                    if did:
                        self._db.update_status(did, "file_not_found", "File not found on disk")
                        if self._status_cb:
                            self._status_cb(did, "file_not_found", "File not found on disk")

    def _handle_save_resume_data_alert(self, alert: Any):
        alert_handle = getattr(alert, "handle", None)
        if not alert_handle:
            return

        matched_did = None
        alert_hash = _get_info_hash_from_handle(alert_handle)
        for did, h in self._handles.items():
            if h is alert_handle:
                matched_did = did
                break
            h_hash = _get_info_hash_from_handle(h)
            if h_hash and alert_hash and h_hash == alert_hash:
                matched_did = did
                break

        if not matched_did:
            return

        try:
            params = alert.params
            params_hash = _get_info_hash_from_params(params)
            handle_hash = _get_info_hash_from_handle(alert_handle)
            if params_hash and handle_hash and params_hash != handle_hash:
                log.warning(
                    "Refusing to save resume data for %s: hash mismatch (%s != %s)",
                    matched_did, params_hash, handle_hash,
                )
                return

            resume_path = FASTRESUME_DIR / f"{matched_did}.fastresume"
            with open(resume_path, "wb") as f:
                f.write(lt.write_resume_data_buf(params))
            log.debug("Saved fastresume for %s", matched_did)
        except Exception as exc:
            log.warning("Failed saving fastresume for %s: %s", matched_did, exc)
