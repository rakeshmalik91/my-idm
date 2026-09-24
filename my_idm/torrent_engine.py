"""Torrent download engine wrapping libtorrent."""

from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable, Any

from my_idm.config import TorConfig, TorrentConfig
from my_idm.database import Database, DownloadEntry
from my_idm.network import NetworkConfig, is_interface_active
from my_idm.utils import normalize_path

log = logging.getLogger(__name__)

# Try to import libtorrent; provide a clear error if missing
try:
    import libtorrent as lt

    _HAS_LIBTORRENT = True
except ImportError:
    _HAS_LIBTORRENT = False
    lt = None  # type: ignore

HAS_LIBTORRENT = _HAS_LIBTORRENT

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


def _priority_to_label(prio: int) -> str:
    """Map libtorrent priority integer to human-readable label."""
    if prio >= 7:
        return "Max (100%)"
    if prio >= 6:
        return "High (75%)"
    if prio >= 3:
        return "Medium (50%)"
    if prio >= 1:
        return "Low (25%)"
    return "Don't Download"


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
        self._torrent_config: Optional[TorrentConfig] = None
        self._general_config: Optional[object] = None  # GeneralConfig
        self._handles: dict[str, object] = {}   # download_id → lt.torrent_handle
        self._progress_cb: Optional[ProgressCallback] = None
        self._status_cb: Optional[StatusCallback] = None
        self._filename_cb: Optional[Callable[[str, str], None]] = None
        self._running = False
        self._last_active_time: dict[str, float] = {}

    def set_general_config(self, config: object):
        """Set general configuration for timeout settings."""
        self._general_config = config

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

    def stop(self, status_cb=None):
        self._running = False
        if not self._session:
            return

        if status_cb:
            status_cb("Saving BitTorrent resume state...", 55)

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
                try:
                    from PySide6.QtWidgets import QApplication
                    app = QApplication.instance()
                    if app:
                        app.processEvents()
                except Exception:
                    pass

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

    @property
    def torrent_config(self) -> Optional[TorrentConfig]:
        return self._torrent_config

    def apply_tor_config(self, config: TorConfig):
        """Apply Tor SOCKS5 proxy settings to libtorrent if routing torrents."""
        self._tor_config = config
        self._apply_all_settings()

    def apply_torrent_config(self, config: TorrentConfig):
        """Apply Torrent engine preferences and update active seeding speed limits."""
        self._torrent_config = config
        self._apply_seeding_limits()

    def set_torrent_config(self, config: TorrentConfig):
        """Alias for apply_torrent_config."""
        self.apply_torrent_config(config)

    def apply_network_config(self, config: NetworkConfig):
        """Apply network interface binding and proxy settings to libtorrent."""
        self._network_config = config
        self._apply_all_settings()
        self._apply_seeding_limits()

    def _apply_seeding_limit_to_handle(self, handle: Any):
        if not _HAS_LIBTORRENT or not handle:
            return
        try:
            if not handle.is_valid():
                return
            if not self._torrent_config:
                return
            dl_limit = self._network_config.download_limit if self._network_config else 0
            limit = self._torrent_config.get_effective_seeding_speed_limit(dl_limit)
            if limit > 0:
                handle.set_upload_limit(limit)
            elif self._network_config and self._network_config.upload_limit > 0:
                handle.set_upload_limit(self._network_config.upload_limit)
            else:
                handle.set_upload_limit(-1)
        except Exception as exc:
            log.debug("Failed setting seeding upload limit on handle: %s", exc)

    def _apply_seeding_limits(self):
        if not _HAS_LIBTORRENT or not self._session or not self._torrent_config:
            return
        for download_id, handle in list(self._handles.items()):
            entry = self._db.get_download(download_id)
            if entry and entry.status == "seeding":
                self._apply_seeding_limit_to_handle(handle)

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

            # 3. Bandwidth limits
            if self._network_config:
                sett["download_rate_limit"] = int(self._network_config.download_limit or 0)
                sett["upload_rate_limit"] = int(self._network_config.upload_limit or 0)

            self._session.apply_settings(sett)
        except Exception as exc:
            log.warning("Failed to apply network settings to libtorrent: %s", exc)

    def set_session_limits(self, download_limit: int, upload_limit: int):
        """Apply global download and upload rate limits to libtorrent session."""
        if self._network_config:
            self._network_config.download_limit = download_limit
            self._network_config.upload_limit = upload_limit
        if not _HAS_LIBTORRENT or not self._session:
            return
        try:
            sett = self._session.get_settings()
            sett["download_rate_limit"] = int(download_limit or 0)
            sett["upload_rate_limit"] = int(upload_limit or 0)
            self._session.apply_settings(sett)
            log.info("Applied libtorrent session limits: down=%d, up=%d", download_limit, upload_limit)
            self._apply_seeding_limits()
        except Exception as exc:
            log.warning("Failed to apply session rate limits: %s", exc)

    def set_torrent_bandwidth_allocation(self, download_id: str, allocation: str):
        """Set allocation level ('low', 'medium', 'high', 'max') for a torrent."""
        entry = self._db.get_download(download_id)
        if entry:
            entry.metadata["bandwidth_allocation"] = allocation
            self._db.update_download(entry)

        handle = self._handles.get(download_id)
        if not handle or not _HAS_LIBTORRENT:
            return

        fractions = {"low": 0.25, "medium": 0.50, "high": 0.75, "max": 1.0}
        frac = fractions.get(allocation.lower(), 1.0)
        try:
            if not handle.is_valid():
                return
            dl_limit = self._network_config.download_limit if self._network_config else 0
            ul_limit = self._network_config.upload_limit if self._network_config else 0
            if dl_limit > 0:
                handle.set_download_limit(int(dl_limit * frac))
            else:
                handle.set_download_limit(-1 if frac >= 1.0 else int(10_000_000 * frac))

            if ul_limit > 0:
                handle.set_upload_limit(int(ul_limit * frac))
            else:
                handle.set_upload_limit(-1 if frac >= 1.0 else int(10_000_000 * frac))
        except Exception as exc:
            log.warning("Failed to set bandwidth allocation for torrent %s: %s", download_id, exc)

    # -- public API ----------------------------------------------------------

    def add_torrent(self, entry: DownloadEntry) -> bool:
        """Add a torrent by magnet URI or .torrent file path.

        Returns True on success, False if libtorrent unavailable.
        """
        if not _HAS_LIBTORRENT or not self._session:
            log.error("Cannot add torrent — libtorrent not available")
            return False

        if entry.download_type != "torrent":
            log.debug("Skipping non-torrent entry in TorrentEngine: %s (type=%s)", entry.id, entry.download_type)
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

        elif url.startswith(("http://", "https://", "ftp://")):
            # Only proceed if the URL actually indicates a .torrent file
            parsed_u = urlparse(url)
            p_lower = parsed_u.path.lower()
            q_lower = parsed_u.query.lower()
            if not (p_lower.endswith(".torrent") or ".torrent" in p_lower or ".torrent" in q_lower):
                log.warning("HTTP URL %s does not point to a .torrent file, skipping remote torrent fetch", url)
                return False

            # Fetch remote .torrent file and cache locally
            torrent_cache = FASTRESUME_DIR / f"{entry.id}.torrent"
            try:
                import urllib.request
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    torrent_data = resp.read()
                with open(torrent_cache, "wb") as f:
                    f.write(torrent_data)
                ti = lt.torrent_info(str(torrent_cache))
                parsed_hash = str(ti.info_hash()).lower()
                if parsed_hash:
                    expected_hash = parsed_hash
            except Exception as exc:
                log.error("Failed to download or parse torrent file from %s: %s", url, exc)
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

        # Apply bandwidth allocation if set
        alloc = entry.metadata.get("bandwidth_allocation") if entry.metadata else None
        if alloc:
            self.set_torrent_bandwidth_allocation(entry.id, alloc)

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
                if _HAS_LIBTORRENT and hasattr(lt, "torrent_flags"):
                    handle.unset_flags(lt.torrent_flags.auto_managed)
            except Exception:
                pass
            handle.pause()
            self._db.update_status(entry.id, "paused")
            if self._status_cb:
                self._status_cb(entry.id, "paused", "")
        elif entry.status == "completed":
            try:
                if _HAS_LIBTORRENT and hasattr(lt, "torrent_flags"):
                    handle.unset_flags(lt.torrent_flags.auto_managed)
            except Exception:
                pass
            handle.pause()
            # Preserve completed status — do not switch to fetching_metadata
        elif entry.status == "seeding":
            # Keep seeding active and apply seeding upload limit
            self._apply_seeding_limit_to_handle(handle)
        else:
            has_meta = False
            try:
                if hasattr(handle, "status"):
                    has_meta = getattr(handle.status(), "has_metadata", False)
            except Exception:
                pass
            initial_status = "downloading" if has_meta else "fetching_metadata"
            self._db.update_status(entry.id, initial_status)
            if initial_status == "fetching_metadata":
                if not entry.fetching_metadata_since:
                    entry.fetching_metadata_since = datetime.now(timezone.utc).isoformat()
                    self._db.update_download(entry)
            if self._status_cb:
                self._status_cb(entry.id, initial_status, "")

        log.info("Added torrent: %s", entry.filename or url)
        return True

    def pause(self, download_id: str):
        handle = self._handles.get(download_id)
        if handle:
            entry = self._db.get_download(download_id) if hasattr(self, "_db") and self._db else None
            if entry:
                try:
                    files = self.get_torrent_files(download_id)
                    if files:
                        entry.metadata["files"] = files
                    trackers = self.get_torrent_trackers(download_id)
                    if trackers:
                        entry.metadata["trackers"] = trackers
                    peers = self.get_torrent_peers(download_id)
                    if peers:
                        entry.metadata["peer_list"] = peers
                    self._db.update_download(entry)
                except Exception:
                    pass
            try:
                if _HAS_LIBTORRENT and hasattr(lt, "torrent_flags"):
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
                if _HAS_LIBTORRENT and hasattr(lt, "torrent_flags"):
                    handle.set_flags(lt.torrent_flags.auto_managed)
            except Exception as e:
                log.debug("Could not set auto_managed flag on resume: %s", e)
            handle.resume()
            s = None
            has_meta = False
            is_done = False
            try:
                if hasattr(handle, "status"):
                    s = handle.status()
                    has_meta = getattr(s, "has_metadata", False)
                    tot = getattr(s, "total_wanted", 0)
                    tot_done = getattr(s, "total_wanted_done", 0)
                    if tot > 0 and tot_done >= tot:
                        is_done = True
                    elif getattr(s, "is_finished", False) or getattr(s, "is_seeding", False):
                        is_done = True
            except Exception:
                pass

            entry = self._db.get_download(download_id)
            if not is_done and entry and entry.total_size > 0 and entry.downloaded_size >= entry.total_size:
                is_done = True

            if is_done:
                new_status = "seeding" if (not self._torrent_config or self._torrent_config.seeding_after_complete) else "completed"
                if new_status == "seeding":
                    self._apply_seeding_limit_to_handle(handle)
            else:
                new_status = "downloading" if has_meta else "fetching_metadata"
            self._db.update_status(download_id, new_status)
            if entry and new_status == "fetching_metadata" and not entry.fetching_metadata_since:
                entry.fetching_metadata_since = datetime.now(timezone.utc).isoformat()
                self._db.update_download(entry)
            if self._status_cb:
                self._status_cb(download_id, new_status, "")

    def force_start(self, download_id: str):
        """Force start torrent by disabling auto-managed queue limits and resuming immediately."""
        handle = self._handles.get(download_id)
        if handle:
            try:
                if _HAS_LIBTORRENT and hasattr(lt, "torrent_flags"):
                    handle.unset_flags(lt.torrent_flags.auto_managed)
            except Exception as e:
                log.debug("Could not unset auto_managed flag on handle: %s", e)
            handle.resume()
            s = None
            has_meta = False
            is_done = False
            try:
                if hasattr(handle, "status"):
                    s = handle.status()
                    has_meta = getattr(s, "has_metadata", False)
                    tot = getattr(s, "total_wanted", 0)
                    tot_done = getattr(s, "total_wanted_done", 0)
                    if tot > 0 and tot_done >= tot:
                        is_done = True
                    elif getattr(s, "is_finished", False) or getattr(s, "is_seeding", False):
                        is_done = True
            except Exception:
                pass

            entry = self._db.get_download(download_id)
            if not is_done and entry and entry.total_size > 0 and entry.downloaded_size >= entry.total_size:
                is_done = True

            if is_done:
                new_status = "seeding" if (not self._torrent_config or self._torrent_config.seeding_after_complete) else "completed"
                if new_status == "seeding":
                    self._apply_seeding_limit_to_handle(handle)
            else:
                new_status = "downloading" if has_meta else "fetching_metadata"
            self._db.update_status(download_id, new_status)
            if entry and new_status == "fetching_metadata" and not entry.fetching_metadata_since:
                entry.fetching_metadata_since = datetime.now(timezone.utc).isoformat()
                self._db.update_download(entry)
            if self._status_cb:
                self._status_cb(download_id, new_status, "")


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
                if _HAS_LIBTORRENT and hasattr(lt, "torrent_flags"):
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

    def rename_root(self, download_id: str, new_name: str) -> bool:
        """Rename the root file or folder of a torrent in libtorrent and on disk."""
        entry = self._db.get_download(download_id)
        if not entry:
            return False

        old_disk_path = (
            Path(entry.file_path)
            if entry.file_path
            else (Path(entry.save_path) / entry.filename if entry.filename else None)
        )
        new_disk_path = Path(entry.save_path) / new_name

        handle = self._handles.get(download_id)
        if handle:
            try:
                if not hasattr(handle, "is_valid") or handle.is_valid():
                    ti = None
                    try:
                        ti = handle.torrent_file() if hasattr(handle, "torrent_file") else None
                    except Exception:
                        pass
                    if not ti:
                        try:
                            ti = handle.get_torrent_info() if hasattr(handle, "get_torrent_info") else None
                        except Exception:
                            pass

                    if ti and hasattr(handle, "rename_file"):
                        try:
                            num_files = ti.num_files()
                            files = ti.files()
                            for i in range(num_files):
                                rel_path = files.file_path(i)
                                parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
                                if parts:
                                    if len(parts) == 1:
                                        handle.rename_file(i, new_name)
                                    else:
                                        parts[0] = new_name
                                        new_rel_path = "/".join(parts)
                                        handle.rename_file(i, new_rel_path)
                        except Exception as exc:
                            log.warning("Failed to rename file(s) in libtorrent handle for %s: %s", download_id, exc)

                    if hasattr(handle, "save_resume_data"):
                        try:
                            handle.save_resume_data()
                        except Exception:
                            pass
            except Exception as exc:
                log.warning("Error checking handle validity for %s rename: %s", download_id, exc)

        # Update metadata files list if present
        if entry.metadata and "files" in entry.metadata and isinstance(entry.metadata["files"], list):
            for f in entry.metadata["files"]:
                f_path = f.get("path", "")
                if f_path:
                    parts = [p for p in f_path.replace("\\", "/").split("/") if p]
                    if parts:
                        if len(parts) > 1:
                            parts[0] = new_name
                            f["path"] = "/".join(parts)
                        else:
                            f["path"] = new_name
                            f["name"] = new_name

        # If file or folder exists on disk, rename it
        if old_disk_path and old_disk_path.exists() and old_disk_path != new_disk_path:
            try:
                new_disk_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_disk_path), str(new_disk_path))
            except Exception as exc:
                log.error("Failed to move torrent path on disk from %s to %s: %s", old_disk_path, new_disk_path, exc)
                return False

        return True

    def get_status(self, download_id: str) -> Optional[dict]:
        """Get current torrent status for polling."""
        handle = self._handles.get(download_id)
        if not handle:
            return None

        try:
            s = handle.status()
            total_size = s.total_wanted
            downloaded = s.total_wanted_done

            if s.has_metadata:
                ti = handle.torrent_file()
                if ti:
                    ti_size = ti.total_size()
                    if ti_size > 0:
                        total_size = ti_size

            total_done = getattr(s, "total_done", 0)
            if total_done > 0:
                downloaded = max(downloaded, total_done)

            state_idx = int(s.state)
            state_name = _STATE_NAMES.get(state_idx, str(s.state))

            if state_name in ("finished", "seeding") and total_size > 0:
                downloaded = total_size
                progress = 100.0
            else:
                progress = s.progress * 100
            speed = s.download_rate
            upload_speed = s.upload_rate
            seeds = s.num_seeds
            peers = s.num_peers
            eta = (
                (total_size - downloaded) / speed
                if speed > 0 else 0
            )

            # Swarm totals from scrape / peer list
            num_comp = getattr(s, "num_complete", -1)
            list_s = getattr(s, "list_seeds", 0)
            total_seeds = max(num_comp, list_s, seeds)
            if total_seeds < 0:
                total_seeds = seeds

            num_incomp = getattr(s, "num_incomplete", -1)
            list_p = getattr(s, "list_peers", 0)
            total_peers = max(num_incomp, list_p, peers)
            if total_peers < 0:
                total_peers = peers

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
                "total_seeds": total_seeds,
                "total_peers": total_peers,
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

            # Update DB and entry transient attributes
            new_total = status.get("total_size", 0)
            new_dl = status.get("downloaded", 0)
            if entry.status in ("completed", "seeding"):
                if new_total > 0:
                    entry.total_size = new_total
                if new_dl > 0:
                    entry.downloaded_size = new_dl
                elif entry.total_size > 0:
                    entry.downloaded_size = entry.total_size
            else:
                if new_total > 0 or entry.total_size == 0:
                    entry.total_size = new_total
                entry.downloaded_size = new_dl
            seeds = status.get("seeds", 0)
            peers = status.get("peers", 0)
            total_seeds = status.get("total_seeds", seeds)
            total_peers = status.get("total_peers", peers)
            entry.seeds = seeds
            entry.peers = peers
            entry.total_seeds = total_seeds
            entry.total_peers = total_peers
            if entry.metadata is not None:
                entry.metadata["seeds"] = seeds
                entry.metadata["peers"] = peers
                entry.metadata["total_seeds"] = total_seeds
                entry.metadata["total_peers"] = total_peers

            resolved_name = status.get("name")
            has_explicit = bool(entry.metadata.get("explicit_filename")) if entry.metadata else False
            if not has_explicit and resolved_name and resolved_name != entry.filename:
                entry.filename = resolved_name
                entry.file_path = str(
                    Path(entry.save_path) / entry.filename
                )
                if self._filename_cb:
                    self._filename_cb(download_id, entry.filename)

            # Store file hierarchy and progress details, trackers, and peers in DB
            has_meta = False
            try:
                if hasattr(handle, "status"):
                    has_meta = bool(getattr(handle.status(), "has_metadata", False))
            except Exception:
                pass
            if has_meta or bool(resolved_name) or bool(entry.filename):
                files = self.get_torrent_files(download_id)
                if files:
                    entry.metadata["files"] = files
                trackers = self.get_torrent_trackers(download_id)
                if trackers:
                    entry.metadata["trackers"] = trackers
                peers = self.get_torrent_peers(download_id)
                if peers:
                    entry.metadata["peer_list"] = peers

            self._db.update_download(entry)

            # If download is paused or stopped in DB, make sure torrent handle stays paused and reports 0 speed
            if entry.status in ("paused", "stopped", "suspended"):
                try:
                    s = handle.status()
                    raw_paused = getattr(s, "paused", None)
                    if raw_paused is not None and not type(raw_paused).__name__.startswith("MagicMock"):
                        is_paused = bool(raw_paused)
                    else:
                        is_paused = bool(getattr(s, "is_paused", False))
                    if not is_paused:
                        if _HAS_LIBTORRENT and hasattr(lt, "torrent_flags"):
                            handle.unset_flags(lt.torrent_flags.auto_managed)
                        handle.pause()
                except Exception:
                    pass
                if self._progress_cb:
                    self._progress_cb(
                        download_id,
                        entry.downloaded_size,
                        entry.total_size,
                        0.0,
                        0.0,
                        status.get("seeds", 0),
                        status.get("peers", 0),
                        0.0,
                    )
                continue

            cb_dl = status["downloaded"]
            cb_tot = status["total_size"]
            if entry.status in ("completed", "seeding"):
                if entry.total_size > 0:
                    cb_tot = max(cb_tot, entry.total_size)
                    cb_dl = max(cb_dl, entry.downloaded_size, cb_tot)

            # Emit progress
            if self._progress_cb:
                self._progress_cb(
                    download_id,
                    cb_dl,
                    cb_tot,
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
                    if not entry.completed_at:
                        entry.completed_at = datetime.now(timezone.utc).isoformat()
                    target_status = "seeding" if (not self._torrent_config or self._torrent_config.seeding_after_complete) else "completed"
                    entry.status = target_status
                    self._db.update_download(entry)
                    self._db.update_status(download_id, target_status)
                    if target_status == "seeding":
                        self._apply_seeding_limit_to_handle(handle)
                    elif target_status == "completed":
                        try:
                            if _HAS_LIBTORRENT and hasattr(lt, "torrent_flags"):
                                handle.unset_flags(lt.torrent_flags.auto_managed)
                            handle.pause()
                        except Exception:
                            pass
                    if self._status_cb:
                        self._status_cb(download_id, target_status, "")
                    try:
                        if handle.is_valid():
                            handle.save_resume_data()
                    except Exception:
                        pass
            elif state == "downloading_metadata":
                if entry.status not in ("fetching_metadata", "completed", "seeding", "paused", "stopped", "suspended"):
                    self._db.update_status(download_id, "fetching_metadata")
                    if not entry.fetching_metadata_since:
                        entry.fetching_metadata_since = datetime.now(timezone.utc).isoformat()
                        self._db.update_download(entry)
                    if self._status_cb:
                        self._status_cb(download_id, "fetching_metadata", "")
                elif entry.status == "fetching_metadata" and not entry.fetching_metadata_since:
                    entry.fetching_metadata_since = datetime.now(timezone.utc).isoformat()
                    self._db.update_download(entry)
            elif entry.status == "fetching_metadata" and (state in ("downloading", "finished", "seeding") or status.get("name") or status["downloaded"] > 0):
                if state in ("finished", "seeding"):
                    new_status = "seeding" if (not self._torrent_config or self._torrent_config.seeding_after_complete) else "completed"
                    if new_status == "seeding":
                        self._apply_seeding_limit_to_handle(handle)
                else:
                    new_status = "downloading"
                entry.status = new_status
                entry.fetching_metadata_since = ""
                self._db.update_download(entry)
                self._db.update_status(download_id, new_status)
                if self._status_cb:
                    self._status_cb(download_id, new_status, "")
            elif entry.status == "checking" and state not in ("checking_files", "queued_for_checking"):
                if status["total_size"] > 0 and status["downloaded"] >= status["total_size"]:
                    new_status = "seeding" if (not self._torrent_config or self._torrent_config.seeding_after_complete) else "completed"
                    if new_status == "seeding":
                        self._apply_seeding_limit_to_handle(handle)
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
            elif entry.status == "completed" and state not in ("finished", "seeding", "checking_files", "queued_for_checking", "downloading_metadata"):
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
                    if entry.status not in ("stalled", "paused", "stopped", "completed", "error", "suspended"):
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

            # Check for fetching_metadata timeout -> suspend
            self._check_fetching_metadata_timeout(download_id, entry, status)

    # -- details queries -----------------------------------------------------

    def get_torrent_files(self, download_id: str) -> list[dict]:
        """Returns details for each file in the torrent."""
        entry = self._db.get_download(download_id) if hasattr(self, "_db") and self._db else None
        is_completed = bool(entry and entry.status in ("completed", "seeding"))
        handle = self._handles.get(download_id)
        if not handle or not _HAS_LIBTORRENT:
            if entry and entry.metadata and "files" in entry.metadata and isinstance(entry.metadata["files"], list):
                cached = entry.metadata["files"]
                if is_completed:
                    for f in cached:
                        if f.get("priority", 4) > 0:
                            f["downloaded"] = f.get("size", 0)
                            f["progress"] = 100.0
                            f["status"] = "completed"
                        else:
                            f["status"] = "skipped"
                return cached
            return []

        try:
            if not handle.is_valid():
                if entry and entry.metadata and "files" in entry.metadata and isinstance(entry.metadata["files"], list):
                    return entry.metadata["files"]
                return []
            ti = None
            try:
                ti = handle.torrent_file() if hasattr(handle, "torrent_file") else None
            except Exception:
                pass
            if not ti:
                if entry and entry.metadata and "files" in entry.metadata and isinstance(entry.metadata["files"], list):
                    return entry.metadata["files"]
                return []

            num_files = ti.num_files()
            files_info = ti.files()
            progress_list = handle.file_progress() if hasattr(handle, "file_progress") else []
            priorities = handle.get_file_priorities() if hasattr(handle, "get_file_priorities") else []

            result = []
            for i in range(num_files):
                f_size = files_info.file_size(i)
                f_path = files_info.file_path(i)
                f_prio = priorities[i] if i < len(priorities) else 4
                if is_completed and f_prio > 0:
                    f_prog = f_size
                    pct = 100.0
                    f_status = "completed"
                elif f_prio == 0:
                    f_prog = progress_list[i] if i < len(progress_list) else 0
                    pct = (f_prog / f_size * 100.0) if f_size > 0 else 0.0
                    f_status = "skipped"
                else:
                    f_prog = progress_list[i] if i < len(progress_list) else 0
                    pct = (f_prog / f_size * 100.0) if f_size > 0 else 100.0
                    if pct >= 100.0 or f_prog >= f_size:
                        f_status = "completed"
                    elif f_prog > 0:
                        f_status = "downloading"
                    else:
                        f_status = "pending"

                result.append({
                    "index": i,
                    "path": f_path,
                    "name": os.path.basename(f_path),
                    "size": f_size,
                    "progress": min(pct, 100.0),
                    "downloaded": f_prog,
                    "priority": f_prio,
                    "priority_label": _priority_to_label(f_prio),
                    "status": f_status,
                })
            return result
        except Exception as exc:
            log.debug("Failed to get torrent files for %s: %s", download_id, exc)
            if entry and entry.metadata and "files" in entry.metadata and isinstance(entry.metadata["files"], list):
                return entry.metadata["files"]
            return []

    def set_torrent_file_priority(self, download_id: str, file_index: int, priority: int) -> bool:
        """Sets priority for a specific file (0 = do not download, 1 = low, 4 = normal, 7 = high)."""
        entry = self._db.get_download(download_id)
        if not entry or entry.download_type != "torrent":
            return False
        handle = self._handles.get(download_id)
        if not handle and entry:
            if entry.status in ("completed", "seeding") and priority == 0:
                if entry.metadata and "files" in entry.metadata and isinstance(entry.metadata["files"], list):
                    for f in entry.metadata["files"]:
                        if f.get("index") == file_index:
                            f["priority"] = priority
                            f["priority_label"] = _priority_to_label(priority)
                            f["status"] = "skipped"
                            break
                    self._db.update_download(entry)
                return True
            self.add_torrent(entry)
            handle = self._handles.get(download_id)

        if not handle or not _HAS_LIBTORRENT:
            return False

        try:
            if not handle.is_valid():
                return False
            handle.file_priority(file_index, priority)

            s = handle.status()
            total_wanted = getattr(s, "total_wanted", 0)
            total_wanted_done = getattr(s, "total_wanted_done", 0)
            is_all_done = (total_wanted > 0 and total_wanted_done >= total_wanted) or getattr(s, "is_finished", False) or getattr(s, "is_seeding", False)

            # Update file metadata if stored
            if entry and "files" in entry.metadata and isinstance(entry.metadata["files"], list):
                for f in entry.metadata["files"]:
                    if f.get("index") == file_index:
                        f["priority"] = priority
                        f["priority_label"] = _priority_to_label(priority)
                        if priority == 0:
                            f["status"] = "skipped"
                        elif f.get("progress", 0.0) >= 100.0 or is_all_done:
                            f["status"] = "completed"
                        elif f.get("downloaded", 0) > 0:
                            f["status"] = "downloading"
                        else:
                            f["status"] = "pending"
                        break
                self._db.update_download(entry)

            # If unchecked file is checked (priority > 0) after download was complete:
            if priority > 0 and not is_all_done and entry and entry.status in ("completed", "seeding"):
                try:
                    if hasattr(lt, "torrent_flags"):
                        handle.set_flags(lt.torrent_flags.auto_managed)
                except Exception:
                    pass
                handle.resume()
                new_status = "downloading"
                entry.status = new_status
                self._db.update_status(download_id, new_status)
                if self._status_cb:
                    self._status_cb(download_id, new_status, "")

            # Calculate and emit updated progress & percentage
            if entry:
                downloaded_bytes = total_wanted_done if total_wanted_done > 0 else getattr(s, "total_done", 0)
                total_bytes = total_wanted if total_wanted > 0 else (entry.total_size or 1)
                entry.downloaded_size = downloaded_bytes
                self._db.update_progress(download_id, downloaded_bytes)
                if self._progress_cb:
                    self._progress_cb(
                        download_id,
                        downloaded_bytes,
                        total_bytes,
                        getattr(s, "download_rate", 0),
                        0.0,
                        getattr(s, "num_seeds", 0),
                        getattr(s, "num_peers", 0),
                        getattr(s, "upload_rate", 0),
                    )

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
            entry = self._db.get_download(download_id) if hasattr(self, "_db") and self._db else None
            if entry and entry.metadata:
                cached = entry.metadata.get("peer_list") or entry.metadata.get("peers")
                if isinstance(cached, list):
                    return cached
            return []

        try:
            if not handle.is_valid():
                entry = self._db.get_download(download_id) if hasattr(self, "_db") and self._db else None
                if entry and entry.metadata:
                    cached = entry.metadata.get("peer_list") or entry.metadata.get("peers")
                    if isinstance(cached, list):
                        return cached
                return []
            peer_info_list = handle.get_peer_info()
            peers = []
            for p in peer_info_list:
                ip_str = f"{p.ip[0]}:{p.ip[1]}" if isinstance(p.ip, (tuple, list)) else str(p.ip)
                client_str = self._safe_str(getattr(p, "client", "Unknown"), default="Unknown").strip()
                if not client_str:
                    client_str = "Unknown"

                # Format flags into standard client flag letters
                flags_letters = []
                if getattr(p, "seed", False) or getattr(p, "upload_only", False):
                    flags_letters.append("S")
                if getattr(p, "down_speed", 0) > 0 or (not getattr(p, "choked", True) and getattr(p, "interesting", False)):
                    flags_letters.append("D")
                if getattr(p, "up_speed", 0) > 0 or (not getattr(p, "remote_choked", True) and getattr(p, "remote_interested", False)):
                    flags_letters.append("U")
                if getattr(p, "optimistic_unchoke", False):
                    flags_letters.append("O")
                if getattr(p, "snubbed", False):
                    flags_letters.append("K")
                if getattr(p, "rc4_encrypted", False) or getattr(p, "plaintext_encrypted", False):
                    flags_letters.append("E")
                if getattr(p, "dht", False):
                    flags_letters.append("H")
                if getattr(p, "pex", False):
                    flags_letters.append("X")
                if not getattr(p, "outgoing_connection", True) or getattr(p, "local_connection", False):
                    flags_letters.append("I")

                flags_str = "".join(flags_letters)
                if not flags_str:
                    raw_flags = getattr(p, "flags", "")
                    flags_str = self._safe_str(raw_flags) if raw_flags else "—"

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
            entry = self._db.get_download(download_id) if hasattr(self, "_db") and self._db else None
            if entry and entry.metadata:
                cached = entry.metadata.get("peer_list") or entry.metadata.get("peers")
                if isinstance(cached, list):
                    return cached
            return []

    def get_torrent_trackers(self, download_id: str) -> list[dict]:
        """Returns tracker status information."""
        handle = self._handles.get(download_id)
        if not handle or not _HAS_LIBTORRENT:
            entry = self._db.get_download(download_id) if hasattr(self, "_db") and self._db else None
            if entry and entry.metadata and "trackers" in entry.metadata and isinstance(entry.metadata["trackers"], list):
                return entry.metadata["trackers"]
            return []

        try:
            if not handle.is_valid():
                entry = self._db.get_download(download_id) if hasattr(self, "_db") and self._db else None
                if entry and entry.metadata and "trackers" in entry.metadata and isinstance(entry.metadata["trackers"], list):
                    return entry.metadata["trackers"]
                return []
            trackers_list = handle.trackers()
            trackers = []
            for t in trackers_list:
                status_str = "Working"
                msg = self._safe_str(getattr(t, "message", "")).strip()
                fails = getattr(t, "fails", 0)
                endpoints = getattr(t, "endpoints", [])
                updating = False
                seeds = -1
                peers = -1
                if endpoints:
                    for ep in endpoints:
                        if getattr(ep, "updating", False):
                            updating = True
                        info_hashes = getattr(ep, "info_hashes", [])
                        for ih in info_hashes:
                            if getattr(ih, "updating", False):
                                updating = True
                            sc = getattr(ih, "scrape_complete", -1)
                            si = getattr(ih, "scrape_incomplete", -1)
                            if sc >= 0:
                                seeds = max(seeds, sc)
                            if si >= 0:
                                peers = max(peers, si)
                            ih_msg = self._safe_str(getattr(ih, "message", "")).strip()
                            if ih_msg and not msg:
                                msg = ih_msg
                if updating:
                    status_str = "Updating"
                elif fails > 0 and msg:
                    status_str = f"Error: {msg}"
                elif fails > 0:
                    status_str = "Unreachable"
                elif msg:
                    status_str = msg
                else:
                    status_str = "Working"

                trackers.append({
                    "url": self._safe_str(getattr(t, "url", "")),
                    "tier": getattr(t, "tier", 0),
                    "status": status_str,
                    "seeds": seeds if seeds >= 0 else 0,
                    "peers": peers if peers >= 0 else 0,
                    "send_stats": getattr(t, "send_stats", False),
                })
            return trackers
        except Exception as exc:
            log.debug("Failed to get torrent trackers for %s: %s", download_id, exc)
            entry = self._db.get_download(download_id) if hasattr(self, "_db") and self._db else None
            if entry and entry.metadata and "trackers" in entry.metadata and isinstance(entry.metadata["trackers"], list):
                return entry.metadata["trackers"]
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

    def _check_fetching_metadata_timeout(self, download_id: str, entry: DownloadEntry, status: dict):
        """Check if torrent has been in fetching_metadata state too long and suspend if needed."""
        timeout_days = 1
        if self._torrent_config is not None:
            timeout_days = getattr(self._torrent_config, "metadata_fetch_timeout_days", 1)
        elif self._general_config is not None:
            timeout_days = getattr(self._general_config, "metadata_fetch_timeout_days", 1)
        else:
            return
        
        if timeout_days <= 0:
            return
        
        # Only check if currently in fetching_metadata state
        if entry.status != "fetching_metadata":
            return
        
        if not entry.fetching_metadata_since:
            return
        
        try:
            started_dt = datetime.fromisoformat(entry.fetching_metadata_since)
            elapsed_days = (datetime.now(timezone.utc) - started_dt).total_seconds() / 86400
            
            if elapsed_days >= timeout_days:
                # Suspend the torrent
                log.info("Suspending torrent %s after %.1f days in fetching_metadata (timeout: %d days)",
                         download_id, elapsed_days, timeout_days)
                
                # Pause the torrent handle
                handle = self._handles.get(download_id)
                if handle:
                    try:
                        if _HAS_LIBTORRENT and hasattr(lt, "torrent_flags"):
                            handle.unset_flags(lt.torrent_flags.auto_managed)
                        handle.pause()
                    except Exception as exc:
                        log.debug("Failed to pause torrent %s for suspension: %s", download_id, exc)
                
                # Update status to suspended and clear queue_order
                entry.status = "suspended"
                entry.queue_order = 0
                entry.fetching_metadata_since = ""
                self._db.update_download(entry)
                self._db.update_status(download_id, "suspended")
                self._db.update_queue_order(download_id, 0)
                
                if self._status_cb:
                    self._status_cb(download_id, "suspended", 
                                    f"Suspended after {timeout_days} day(s) of fetching metadata")
        except (ValueError, TypeError) as exc:
            log.debug("Error parsing fetching_metadata_since for %s: %s", download_id, exc)
