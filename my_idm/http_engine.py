"""HTTP download engine with segmented multi-connection downloads and fallback."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional, Callable
from urllib.parse import unquote, urlparse, parse_qs

import aiohttp
import humanize

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    _HAS_CURL_CFFI = True
except ImportError:
    CurlAsyncSession = None
    _HAS_CURL_CFFI = False

from my_idm.config import (
    TorConfig,
    GeneralConfig,
    clamp_segment_start_delay,
)
from my_idm.database import Database, DEFAULT_QUEUE_ID, DownloadEntry, SegmentEntry
from my_idm.network import NetworkConfig, is_interface_active
from my_idm.utils import check_disk_space, effective_rate_limit, get_unique_filename

log = logging.getLogger(__name__)

# Defaults
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_SEGMENTS = 8
# Ceiling on the *total* stagger a segmented download may add before its last segment issues
# its first request. The per-segment step is scaled down to fit this rather than the tail
# truncated, because bunching the tail back up at the ceiling recreates exactly the connection
# burst the feature exists to avoid.
MAX_SEGMENT_STAGGER_TOTAL_S = 2.0
CHUNK_SIZE = 64 * 1024          # 64 KiB per read
MAX_RETRIES_PER_SEGMENT = 5
RETRY_BASE_DELAY = 1.0          # seconds, exponential backoff base
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 60


def _requires_curl_impersonation(url: str) -> bool:
    if not _HAS_CURL_CFFI or not url:
        return False
    u_lower = url.lower()
    return any(domain in u_lower for domain in ("owocdn.top", "uwucdn.top", "kwik.cx", "kwik.si", "pahe.win"))



ProgressCallback = Callable[
    [str, int, int, float, float],  # download_id, downloaded, total, speed, eta
    None
]
StatusCallback = Callable[[str, str, str], None]  # download_id, status, error_msg
FilenameCallback = Callable[[str, str], None]      # download_id, filename


class HTTPEngine:
    """Manages HTTP(S) downloads with segmented parallel streaming."""

    def __init__(self, db: Database, max_concurrent: int = 3):
        self._db = db
        self._max_concurrent = max_concurrent
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._active_segments: dict[str, list[SegmentEntry]] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._tor_session: Optional[aiohttp.ClientSession] = None
        self._network_config: Optional[NetworkConfig] = None
        self._tor_config: Optional[TorConfig] = None
        self._progress_cb: Optional[ProgressCallback] = None
        self._status_cb: Optional[StatusCallback] = None
        self._filename_cb: Optional[FilenameCallback] = None
        self._last_progress_emit: dict[str, float] = {}
        self._download_limit: int = 0
        #: queue_id -> (download_limit, upload_limit) in bytes/sec, pushed in by the manager
        #: rather than queried per chunk. Upload is carried for symmetry with the torrent
        #: engine and for the queue dialog's "does this queue cap anything" check; HTTP downloads
        #: do not upload, so only the download half is read here.
        self._queue_limits: dict[str, tuple[int, int]] = {}
        self._general_config: Optional[GeneralConfig] = None

    def _get_effective_download_limit(self, entry: DownloadEntry) -> int:
        """Bytes/sec this download may use, or 0 for unlimited.

        Resolved by `utils.effective_rate_limit`, which owns the rule: the tightest non-zero of
        the queue's ceiling and the global one, scaled by the download's allocation share. The
        queue half matters because the global limit is 0 by default, and the old early return
        here made a queue limit - or an allocation - do nothing at all in that state.
        """
        queue_dl = self._queue_limits.get(entry.queue_id or DEFAULT_QUEUE_ID, (0, 0))[0]
        alloc = entry.metadata.get("bandwidth_allocation") or "max"
        return effective_rate_limit(queue_dl, self._download_limit, alloc)

    def set_download_limit(self, limit: int):
        """Set global download rate limit in bytes/sec (0 = unlimited)."""
        self._download_limit = limit

    def set_queue_limits(self, limits: dict[str, tuple[int, int]]):
        """Supply the per-queue bandwidth ceilings, in bytes/sec per queue id.

        A snapshot, not a handle on the database: the chunk pacers read this per chunk, and a
        query per chunk to learn a limit that changes only when the user edits a dialog would be
        the single hottest query in the transfer path. The manager re-pushes on every change.
        """
        self._queue_limits = {
            queue_id: (int(dl or 0), int(ul or 0))
            for queue_id, (dl, ul) in (limits or {}).items()
        }

    def set_download_bandwidth_allocation(self, download_id: str, allocation: str):
        """Set bandwidth allocation ('low', 'medium', 'high', 'max') for an HTTP download."""
        entry = self._db.get_download(download_id)
        if entry:
            entry.metadata["bandwidth_allocation"] = allocation
            self._db.update_download(entry)

    def get_live_segments(self, download_id: str) -> Optional[list[SegmentEntry]]:
        """Return live in-memory segments for active downloading tasks."""
        return self._active_segments.get(download_id)

    def _segment_stagger_step(self, pending_count: int) -> float:
        """Seconds between one pending segment's first request and the previous one's.

        ``0`` — the default — means every pending segment starts immediately, which is the
        behaviour this app has always had. A non-zero step is scaled down so the *last* of
        ``pending_count`` segments still starts within ``MAX_SEGMENT_STAGGER_TOTAL_S``: at the
        maximum preference with 32 segments the step would otherwise add a full minute of
        dead time before the last connection is even opened.
        """
        if pending_count <= 1 or not self._general_config:
            return 0.0
        step = clamp_segment_start_delay(
            self._general_config.segment_start_delay_ms
        ) / 1000.0
        if step <= 0:
            return 0.0
        max_step = MAX_SEGMENT_STAGGER_TOTAL_S / (pending_count - 1)
        if step > max_step:
            log.info(
                "Segment start stagger scaled from %d ms to %d ms so the last of %d "
                "segments still starts within %.1f s",
                self._general_config.segment_start_delay_ms,
                round(max_step * 1000), pending_count, MAX_SEGMENT_STAGGER_TOTAL_S,
            )
            step = max_step
        return step

    # -- public API ----------------------------------------------------------

    def set_callbacks(self, progress_cb: ProgressCallback,
                      status_cb: StatusCallback,
                      filename_cb: Optional[FilenameCallback] = None):
        self._progress_cb = progress_cb
        self._status_cb = status_cb
        self._filename_cb = filename_cb

    def set_network_config_sync(self, config: NetworkConfig):
        """Set network config synchronously prior to start or in tests."""
        self._network_config = config

    def set_general_config_sync(self, config: GeneralConfig):
        """Set general preferences (retries, timeouts, backoff) synchronously."""
        self._general_config = config

    def _get_retry_delay(self, attempt: int) -> float:
        """Calculate retry delay based on GeneralConfig or fallback defaults."""
        if self._general_config:
            return self._general_config.get_retry_delay(attempt)
        delay = RETRY_BASE_DELAY * (2 ** max(0, attempt))
        return min(delay, 60.0)

    def _get_max_retries(self, entry: Optional[DownloadEntry] = None) -> int:
        """Get configured max retries for an entry or global config."""
        if entry and entry.max_retries > 0:
            return entry.max_retries
        if self._general_config and self._general_config.max_retries > 0:
            return self._general_config.max_retries
        return MAX_RETRIES_PER_SEGMENT

    async def set_network_config(self, config: NetworkConfig):
        """Update network config at runtime and recreate the client session."""
        self._network_config = config
        await self._safe_recreate_session()

    @property
    def network_config(self) -> Optional[NetworkConfig]:
        return self._network_config

    @property
    def tor_config(self) -> Optional[TorConfig]:
        return self._tor_config

    def set_tor_config_sync(self, config: TorConfig):
        """Set Tor config synchronously prior to start or in tests."""
        self._tor_config = config

    async def set_tor_config(self, config: TorConfig):
        """Update Tor config at runtime and recreate the client session."""
        old_tor = (
            self._tor_config.enabled and self._tor_config.route_http
            if self._tor_config else False
        )
        new_tor = config.enabled and config.route_http
        self._tor_config = config

        # If routing didn't change and session exists, no need to recreate session
        if old_tor == new_tor and self._session and not self._session.closed:
            return

        await self._safe_recreate_session()

    async def _safe_recreate_session(self):
        """Safely recreate the client session without leaving orphaned download tasks."""
        # Collect currently active downloads
        active_entries: list[DownloadEntry] = []
        for did, task in list(self._tasks.items()):
            if task and not task.done():
                entry = self._db.get_download(did)
                if entry and entry.status in ("downloading", "checking"):
                    active_entries.append(entry)
                evt = self._cancel_events.get(did)
                if evt:
                    evt.set()
                task.cancel()

        # Wait for all running tasks to cleanly finish saving their progress
        running = [t for t in self._tasks.values() if not t.done()]
        if running:
            await asyncio.gather(*running, return_exceptions=True)

        self._tasks.clear()
        self._cancel_events.clear()

        # Recreate session with the updated network/Tor connector
        await self._recreate_session()

        # Seamlessly restart active downloads with the fresh session
        for entry in active_entries:
            fresh = self._db.get_download(entry.id) or entry
            if fresh.status in ("downloading", "queued"):
                log.info("Resuming download %s after network routing change", entry.id)
                await self.add(fresh)

    async def start(self):
        await self._recreate_session()

    async def _recreate_session(self):
        if self._session and not self._session.closed:
            await self._session.close()
        await self._close_tor_session()

        connector = None
        # Check if Tor is enabled and routing HTTP downloads
        if self._tor_config and self._tor_config.enabled and self._tor_config.route_http:
            try:
                from aiohttp_socks import ProxyConnector
                connector = ProxyConnector.from_url(self._tor_config.tor_socks_url)
                log.info("HTTPEngine routing through Tor SOCKS5 proxy: %s", self._tor_config.tor_socks_url)
            except Exception as e:
                log.warning("Could not create Tor ProxyConnector: %s", e)
        elif self._network_config and self._network_config.is_interface_bound:
            try:
                connector = aiohttp.TCPConnector(
                    local_addr=(self._network_config.interface_ip, 0)
                )
                log.info(
                    "HTTPEngine TCPConnector bound to interface %s (%s)",
                    self._network_config.interface_name,
                    self._network_config.interface_ip,
                )
            except Exception as e:
                log.warning(
                    "Could not bind TCPConnector to %s: %s",
                    self._network_config.interface_ip,
                    e,
                )

        timeout = aiohttp.ClientTimeout(
            connect=CONNECT_TIMEOUT, sock_read=READ_TIMEOUT
        )
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )

    # -- per-download Tor routing ---------------------------------------------

    def _is_tor_routed(self, entry) -> bool:
        """True when *entry* is individually flagged to use Tor."""
        if entry is None:
            return False
        try:
            return bool(entry.metadata.get("route_through_tor", False))
        except Exception:
            return False

    def set_download_tor_route(self, download_id: str, enabled: bool) -> None:
        """Flag or unflag a single download for Tor routing.

        The route is applied when the download starts, so an in-flight download
        must be restarted by the caller for it to take effect immediately.
        """
        entry = self._db.get_download(download_id) if self._db else None
        if not entry:
            return
        if entry.metadata.get("route_through_tor", False) == bool(enabled):
            return
        entry.metadata["route_through_tor"] = bool(enabled)
        self._db.update_download(entry)
        log.info("HTTPEngine: download %s Tor route -> %s", download_id, enabled)

    async def _get_tor_session(self):
        """Lazily create (or reuse) the SOCKS5 session used for Tor-flagged downloads.

        Kept separate from the main session so a single download can be routed
        through Tor even when the global Tor switch is off, and so unflagged
        downloads keep using the direct/general-proxy session.
        """
        if self._tor_session is not None and not self._tor_session.closed:
            return self._tor_session
        if not (self._tor_config and self._tor_config.enabled):
            return None
        try:
            from aiohttp_socks import ProxyConnector
            connector = ProxyConnector.from_url(self._tor_config.tor_socks_url)
        except Exception as exc:
            log.warning("Could not create per-download Tor connector: %s", exc)
            return None
        self._tor_session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(connect=CONNECT_TIMEOUT, sock_read=READ_TIMEOUT),
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
        log.info("HTTPEngine per-download Tor session via %s", self._tor_config.tor_socks_url)
        return self._tor_session

    async def _close_tor_session(self) -> None:
        if self._tor_session is not None and not self._tor_session.closed:
            try:
                await self._tor_session.close()
            except Exception as exc:
                log.debug("Error closing per-download Tor session: %s", exc)
        self._tor_session = None

    async def _session_for_entry(self, entry) -> aiohttp.ClientSession:
        """Return the client session a request for *entry* must use."""
        if self._is_tor_routed(entry):
            tor_session = await self._get_tor_session()
            if tor_session is not None:
                return tor_session
            log.warning(
                "Download %s is flagged for Tor but no Tor session is available; "
                "falling back to the default route",
                getattr(entry, "id", "?"),
            )
        return self._session


    def _build_headers(
        self,
        headers: Optional[dict] = None,
        entry: Optional[DownloadEntry] = None,
        url: str = "",
    ) -> dict:
        req_headers = {"User-Agent": DEFAULT_USER_AGENT}
        if entry and entry.metadata:
            if "headers" in entry.metadata and isinstance(entry.metadata["headers"], dict):
                req_headers.update(entry.metadata["headers"])
            if "referer" in entry.metadata and entry.metadata["referer"]:
                req_headers["Referer"] = entry.metadata["referer"]

        url_check = url or (entry.url if entry else "")
        if url_check and any(cdn in url_check for cdn in ("owocdn.top", "uwucdn.top", "kwik.")):
            # Kwik CDN servers (owocdn.top, uwucdn.top, kwik.*) strictly require https://kwik.cx/ as Referer
            req_headers["Referer"] = "https://kwik.cx/"

        if headers:
            req_headers.update(headers)
        return req_headers

    def _request_kwargs(
        self,
        headers: Optional[dict] = None,
        entry: Optional[DownloadEntry] = None,
        url: str = "",
    ) -> dict:
        kwargs: dict = {}
        kwargs["headers"] = self._build_headers(headers, entry=entry, url=url)

        tor_routing_http = (
            self._tor_config
            and self._tor_config.enabled
            and self._tor_config.route_http
        )
        if (
            not tor_routing_http
            and not self._is_tor_routed(entry)
            and self._network_config
            and self._network_config.proxy_enabled
            and self._network_config.proxy_url
        ):
            kwargs["proxy"] = self._network_config.proxy_url
        return kwargs

    async def stop(self):
        for did in list(self._tasks):
            evt = self._cancel_events.pop(did, None)
            if evt:
                evt.set()
            task = self._tasks.pop(did, None)
            if task and not task.done():
                try:
                    await asyncio.wait_for(task, timeout=3.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    task.cancel()
            # Mark as queued so it automatically resumes on next application start
            self._db.update_status(did, "queued")
        if self._session:
            await self._session.close()
            self._session = None
        await self._close_tor_session()

    async def add(self, entry: DownloadEntry):
        """Start (or resume) an HTTP download."""
        existing = self._tasks.get(entry.id)
        if existing and not existing.done():
            log.warning("Download %s is already active in HTTPEngine, skipping duplicate add", entry.id)
            return

        cancel_evt = asyncio.Event()
        self._cancel_events[entry.id] = cancel_evt
        task = asyncio.create_task(self._run_download(entry, cancel_evt))
        self._tasks[entry.id] = task

    async def pause(self, download_id: str):
        evt = self._cancel_events.pop(download_id, None)
        if evt:
            evt.set()
        self._db.update_status(download_id, "paused")
        self._emit_status(download_id, "paused")
        task = self._tasks.pop(download_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        entry = self._db.get_download(download_id)
        if entry:
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
            if self._progress_cb:
                self._progress_cb(download_id, entry.downloaded_size, entry.total_size, 0.0, 0.0)

    async def cancel(self, download_id: str):
        evt = self._cancel_events.pop(download_id, None)
        if evt:
            evt.set()
        task = self._tasks.pop(download_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def is_active(self, download_id: str) -> bool:
        t = self._tasks.get(download_id)
        return t is not None and not t.done()

    def get_active_download_ids(self) -> set[str]:
        return {did for did, t in self._tasks.items() if not t.done()}

    # -- download logic ------------------------------------------------------

    def _enforce_browser_min_size(self, entry: DownloadEntry, probed_total: int) -> bool:
        """Refuse a browser capture that is below the configured minimum size.

        ``BrowserServer`` applies the minimum itself when it can size the download, but it
        could not: Chrome reports ``totalBytes: 0`` for responses without a Content-Length
        (and for every ``blob:`` URL), and the server's fallback probe has a 1.8 s budget
        that a slow or auth-gated origin can blow. Its check is therefore guarded by
        ``total_bytes > 0``, so an unsizeable URL walked straight past a configured
        minimum - which is how a sub-threshold file kept getting captured.

        The engine probes every download before transferring a byte, so it gets a second,
        authoritative chance. This is deliberately not applied to hand-added downloads: the
        setting means "minimum size to *intercept*", and a user pasting a 4 KB URL should
        still get it.

        Returns True to continue, False once the entry has been marked as skipped.
        """
        min_bytes = 0
        try:
            meta = entry.metadata
            if meta:
                min_bytes = int(meta.get("pending_min_bytes") or 0)
        except Exception:
            min_bytes = 0
        if min_bytes <= 0:
            return True

        # Still unknown after our own probe: we cannot judge, and refusing everything
        # unsizeable would break every chunked / gzip-encoded download.
        if probed_total <= 0:
            log.info(
                "Browser minimum of %d bytes not enforced for %s: size still unknown",
                min_bytes, entry.id,
            )
            return True

        if probed_total >= min_bytes:
            # Now that the real size is known the threshold has been satisfied, so drop it
            # rather than re-checking on every resume.
            try:
                entry.metadata.pop("pending_min_bytes", None)
                self._db.update_download(entry)
            except Exception:
                pass
            return True

        source = (entry.metadata.get("capture_source") if entry.metadata else "") or "browser"
        if source == "clipboard":
            reason = (
                f"File size ({probed_total} bytes) is below the clipboard-capture minimum of "
                f"{min_bytes} bytes. Raise, lower or disable the minimum in "
                f"Preferences -> Clipboard Capture to keep capturing files this small."
            )
            log.info("Skipping clipboard download %s: %s", entry.id, reason)
        else:
            reason = (
                f"File size ({probed_total} bytes) is below the browser-capture minimum of "
                f"{min_bytes} bytes. Raise or disable the minimum in "
                f"Preferences -> Browser Integration to keep capturing files this small."
            )
            log.info("Skipping browser download %s: %s", entry.id, reason)
        entry.error_message = reason
        # update_status persists both the status and the message. Do NOT follow it with
        # update_download(entry): that writes every column, and `entry.status` is still the
        # pre-refusal value, so it would put the row straight back to "queued" - which is
        # how a refused capture would sit in the queue looking like it was about to start.
        self._db.update_status(entry.id, "error", reason)
        self._emit_status(entry.id, "error", reason)
        return False

    def _enforce_disk_space(self, entry: DownloadEntry, probed_total: int) -> bool:
        """Refuse to start a download the target volume cannot hold.

        The failure this prevents is expensive and completely avoidable: the segmented
        path pre-allocates the whole file with ``truncate(total_size)`` and the
        single-stream path appends as it goes, so a 40 GB download into a 2 GB volume
        either fails immediately at allocation or grinds for an hour and dies at 95%.

        Only the *remaining* bytes are required, not the whole file: a resume that already
        has 30 GB on disk needs 10 GB more, and demanding 40 GB would refuse a download
        that is nearly done.

        Returns True to continue, False once the entry has been marked as failed. A volume
        whose free space cannot be read never blocks anything - see
        ``utils.check_disk_space``.
        """
        if not self._general_config or not self._general_config.disk_space_check:
            return True
        if probed_total <= 0:
            # Size unknown (chunked response, magnet): nothing to compare against. The
            # progressive writes will surface the real failure if the disk does fill.
            return True

        target = entry.file_path or entry.save_path
        if not target:
            return True

        already = 0
        try:
            candidate = Path(target)
            if candidate.is_file():
                already = candidate.stat().st_size
        except OSError:
            already = 0
        needed = max(0, int(probed_total) - already)
        if needed <= 0:
            return True

        headroom = int(getattr(self._general_config, "disk_space_headroom_mb", 0) or 0) * 1024 * 1024
        ok, free, shortfall = check_disk_space(target, needed, headroom)
        if ok:
            if needed + headroom > free:
                log.debug(
                    "Disk headroom is tight for %s: %d free, %d needed (+%d headroom)",
                    entry.id, free, needed, headroom,
                )
            return True

        reason = (
            f"Not enough disk space: {humanize.naturalsize(needed, binary=True)} still "
            f"needed but only {humanize.naturalsize(free, binary=True)} free"
            + (f" (a {humanize.naturalsize(headroom, binary=True)} safety margin is also "
               f"required)" if headroom else "")
            + f". Short by {humanize.naturalsize(shortfall, binary=True)}. "
            f"Free up space, choose a different folder, or turn off "
            f"Preferences -> General -> 'Check free disk space before downloading'."
        )
        log.warning("Refusing to start %s: %s", entry.id, reason)
        entry.error_message = reason
        # update_status persists both status and message. No update_download here: it
        # writes every column and `entry.status` is still the pre-refusal value, which
        # would put the row straight back to "queued".
        self._db.update_status(entry.id, "error", reason)
        self._emit_status(entry.id, "error", reason)
        return False

    async def _run_download(self, entry: DownloadEntry,
                            cancel_evt: asyncio.Event):
        download_id = entry.id

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
                entry.status = "error"
                entry.error_message = err
                self._db.update_status(download_id, "error", err)
                self._emit_status(download_id, "error", err)
                return

        try:
            entry.status = "downloading"
            self._db.update_status(download_id, "downloading")
            self._emit_status(download_id, "downloading")

            # Probe the URL
            supports_range, total_size, etag, filename = await self._probe_url(
                entry.url, entry=entry
            )

            # Apply a browser-capture minimum that the capture path could not evaluate
            # because the extension reported no size and its own probe came back empty.
            # This is the first point where the authoritative size is known, so it is the
            # only place the threshold can be enforced without guessing. Downloads the
            # user added by hand never carry `pending_min_bytes` and are unaffected.
            if not self._enforce_browser_min_size(entry, total_size):
                return

            # Now the size is authoritative, it can be compared with the volume.
            if not self._enforce_disk_space(entry, total_size):
                return

            # Update entry with discovered info
            if total_size and total_size != entry.total_size:
                entry.total_size = total_size
            if etag and not entry.etag:
                entry.etag = etag

            # If explicit_filename was specified by caller/backlog/user, preserve it and do not overwrite with website header filename!
            meta = entry.metadata if hasattr(entry, "metadata") else {}
            has_explicit_fn = meta.get("explicit_filename", False)
            if has_explicit_fn and entry.filename:
                candidate = entry.filename
            else:
                candidate = filename or entry.filename or self._filename_from_url(entry.url)
            if candidate:
                existing_entries = self._db.get_all_downloads()
                reserved = {
                    d.filename for d in existing_entries
                    if d.id != entry.id and d.save_path == entry.save_path and d.filename
                }
                # If entry already created its own file_path on disk and is resuming, preserve it
                if entry.file_path and Path(entry.file_path).exists() and entry.filename == candidate:
                    unique_fn = entry.filename
                else:
                    unique_fn = get_unique_filename(entry.save_path, candidate, reserved_names=reserved)

                if unique_fn != entry.filename or not entry.file_path:
                    entry.filename = unique_fn
                    entry.file_path = str(Path(entry.save_path) / unique_fn)
                    self._emit_filename(entry.id, entry.filename)
            self._db.update_download(entry)

            if cancel_evt.is_set():
                return

            if supports_range and total_size and total_size > CHUNK_SIZE * 4:
                # Try segmented download
                try:
                    await self._segmented_download(entry, cancel_evt)
                except _FallbackToSingle:
                    log.info("Falling back to single-stream for %s", entry.url)
                    # Clean up segments
                    self._db.delete_segments(download_id)
                    entry.downloaded_size = 0
                    self._db.update_download(entry)
                    await self._single_download(entry, cancel_evt)
            else:
                await self._single_download(entry, cancel_evt)

            if not cancel_evt.is_set():
                if entry.file_path and Path(entry.file_path).exists():
                    try:
                        f_size = Path(entry.file_path).stat().st_size
                        if f_size > 0:
                            entry.downloaded_size = f_size
                            if entry.total_size <= 0:
                                entry.total_size = f_size
                    except Exception:
                        pass
                if entry.total_size > 0 and entry.downloaded_size < entry.total_size:
                    entry.downloaded_size = entry.total_size
                entry.status = "completed"
                self._db.update_download(entry)
                self._db.update_progress(download_id, entry.downloaded_size, "completed")
                self._db.update_status(download_id, "completed")
                self._emit_status(download_id, "completed")
                self._emit_progress(
                    download_id, entry.downloaded_size,
                    entry.total_size, 0.0, 0.0
                )

        except asyncio.CancelledError:
            log.debug("Download %s cancelled", download_id)
        except Exception as exc:
            if cancel_evt.is_set():
                log.debug("Download %s cancelled/paused with exception: %s", download_id, exc)
            else:
                current = self._db.get_download(download_id)
                if not current or current.status not in ("paused", "completed"):
                    log.exception("Download %s failed: %s", download_id, exc)
                    self._handle_retry(entry, str(exc))
        finally:
            self._tasks.pop(download_id, None)
            self._cancel_events.pop(download_id, None)

    async def _probe_url(self, url: str, entry: Optional[DownloadEntry] = None):
        """HEAD or GET request to discover file size, range support, ETag, filename."""
        supports_range = False
        total_size = 0
        etag = ""
        filename = ""
        req_kwargs = self._request_kwargs(entry=entry, url=url)
        headers = req_kwargs.get("headers", {})

        # If domain requires browser impersonation or entry requested it
        use_curl = _HAS_CURL_CFFI and (
            _requires_curl_impersonation(url)
            or (entry and entry.metadata.get("use_curl_cffi"))
        )

        if use_curl:
            try:
                async with CurlAsyncSession(impersonate="chrome124") as cs:
                    resp = await cs.get(url, headers=headers, stream=True, timeout=20)
                    if resp.status_code in (200, 206):
                        cl = resp.headers.get("content-length")
                        if cl:
                            total_size = int(cl)
                        ar = resp.headers.get("accept-ranges", "").lower()
                        supports_range = (ar == "bytes")
                        etag = resp.headers.get("etag", "")
                        filename = self._extract_filename_from_headers(resp.headers, url)
                        if entry:
                            if not entry.metadata:
                                entry.metadata = {}
                            entry.metadata["use_curl_cffi"] = True
                        return supports_range, total_size, etag, filename
            except Exception as exc:
                log.warning("curl_cffi probe failed for %s: %s", url, exc)

        # Standard aiohttp probe
        try:
            session = await self._session_for_entry(entry)
            async with session.head(
                url, allow_redirects=True, **req_kwargs
            ) as resp:
                if resp.status == 200:
                    total_size = int(resp.headers.get("Content-Length", 0))
                    ar = resp.headers.get("Accept-Ranges", "").lower()
                    supports_range = (ar == "bytes")
                    etag = resp.headers.get("ETag", "")
                    filename = self._extract_filename_from_headers(resp.headers, str(resp.url))
                elif resp.status in (403, 503) and _HAS_CURL_CFFI:
                    # Cloudflare block fallback to curl_cffi
                    try:
                        async with CurlAsyncSession(impersonate="chrome124") as cs:
                            c_resp = await cs.get(url, headers=headers, stream=True, timeout=20)
                            if c_resp.status_code in (200, 206):
                                cl = c_resp.headers.get("content-length")
                                if cl:
                                    total_size = int(cl)
                                ar = c_resp.headers.get("accept-ranges", "").lower()
                                supports_range = (ar == "bytes")
                                etag = c_resp.headers.get("etag", "")
                                filename = self._extract_filename_from_headers(c_resp.headers, url)
                                if entry:
                                    if not entry.metadata:
                                        entry.metadata = {}
                                    entry.metadata["use_curl_cffi"] = True
                                return supports_range, total_size, etag, filename
                    except Exception as c_exc:
                        log.warning("curl_cffi fallback probe failed for %s: %s", url, c_exc)
        except Exception as exc:
            log.warning("HEAD request failed for %s: %s", url, exc)

        return supports_range, total_size, etag, filename

    # -- segmented download --------------------------------------------------

    async def _segmented_download(self, entry: DownloadEntry,
                                  cancel_evt: asyncio.Event):
        download_id = entry.id
        total_size = entry.total_size
        num_segments = entry.num_segments or DEFAULT_SEGMENTS
        file_path = Path(entry.file_path)

        # Load existing segments or create new ones
        segments = self._db.get_segments(download_id)
        if not segments:
            segments = self._create_segments(download_id, total_size,
                                             num_segments)
            self._db.add_segments(segments)

        self._active_segments[download_id] = segments

        # Ensure target file exists and is correctly sized for resuming
        file_path.parent.mkdir(parents=True, exist_ok=True)
        if not file_path.exists() or file_path.stat().st_size < total_size:
            with open(file_path, "a+b") as f:
                f.truncate(total_size)

        # Track progress per segment
        seg_progress: dict[int, int] = {
            s.index: s.downloaded_bytes for s in segments
        }
        start_time = time.monotonic()
        start_downloaded = sum(seg_progress.values())

        sem = asyncio.Semaphore(num_segments)

        async def download_segment(seg: SegmentEntry, start_delay: float):
            if seg.status == "completed":
                return
            async with sem:
                await self._download_one_segment(
                    entry, seg, seg_progress, start_time,
                    start_downloaded, cancel_evt, start_delay,
                )

        pending = [
            s for s in segments if s.status != "completed"
        ]
        if not pending:
            # All done: fall through to the same cleanup the gather path uses, so
            # get_live_segments() cannot keep reporting a finished download as segmented.
            self._active_segments.pop(download_id, None)
            return  # All segments done

        # Staggered starts. Every pending segment is a task created in one burst, so
        # without this a download opens all N connections in the same event-loop
        # iteration - which some hosts answer with a 429. The rank is over *pending*
        # segments, not over `segments`, so a resume that already has 6 of 8 done
        # spreads its remaining 2 rather than idling the last one for 7 delays.
        stagger_step = self._segment_stagger_step(len(pending))
        tasks = [
            asyncio.create_task(download_segment(s, rank * stagger_step))
            for rank, s in enumerate(pending)
        ]

        try:
            await asyncio.gather(*tasks)
        except _FallbackToSingle:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        except (asyncio.CancelledError, Exception):
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            total_dl = sum(seg_progress.values())
            if total_dl > 0:
                self._db.update_progress(download_id, total_dl)
            self._active_segments.pop(download_id, None)

    async def _download_segment_curl(
        self, entry: DownloadEntry, seg: SegmentEntry,
        current_start: int, headers: dict,
        seg_progress: dict[int, int],
        start_time: float, start_downloaded: int,
        cancel_evt: asyncio.Event,
    ):
        req_kwargs = self._request_kwargs(headers, entry=entry, url=entry.url)
        c_headers = req_kwargs.get("headers", {})
        async with CurlAsyncSession(impersonate="chrome124") as cs:
            resp = await cs.get(entry.url, headers=c_headers, stream=True, timeout=READ_TIMEOUT)
            if resp.status_code == 416:
                raise _FallbackToSingle("416 Range Not Satisfiable")
            if resp.status_code not in (200, 206):
                # aiohttp.ClientError, not a bare Exception: the retry ladder in
                # _download_one_segment catches (aiohttp.ClientError, TimeoutError, OSError),
                # so a plain Exception bypasses it entirely and the segment is abandoned
                # still marked "downloading" in the details panel.
                raise aiohttp.ClientError(
                    f"Unexpected status {resp.status_code} in curl segment download"
                )
            if resp.status_code == 200 and seg.index > 0:
                raise _FallbackToSingle("Server returned 200 instead of 206")

            file_path = Path(entry.file_path)
            async for chunk in resp.aiter_content():
                if cancel_evt.is_set():
                    seg.status = "paused"
                    self._db.update_segment(seg.id, seg.downloaded_bytes, "paused")
                    total_dl = sum(seg_progress.values())
                    if total_dl > 0:
                        self._db.update_progress(entry.id, total_dl)
                    return

                with open(file_path, "r+b") as f:
                    f.seek(current_start)
                    f.write(chunk)

                chunk_len = len(chunk)
                current_start += chunk_len
                seg.downloaded_bytes += chunk_len
                seg_progress[seg.index] = seg.downloaded_bytes

                eff_limit = self._get_effective_download_limit(entry)
                if eff_limit > 0:
                    expected_time = chunk_len / eff_limit
                    if expected_time > 0.001:
                        await asyncio.sleep(min(expected_time, 1.0))

                total_dl = sum(seg_progress.values())
                elapsed = time.monotonic() - start_time
                speed = ((total_dl - start_downloaded) / elapsed if elapsed > 0 else 0)
                remaining = entry.total_size - total_dl
                eta = remaining / speed if speed > 0 else 0

                entry.downloaded_size = total_dl
                entry.speed = speed
                entry.eta_seconds = eta
                if seg.status != "downloading":
                    seg.status = "downloading"
                self._db.update_segment(seg.id, seg.downloaded_bytes, "downloading")
                self._emit_progress(entry.id, total_dl, entry.total_size, speed, eta)

            seg.status = "completed"
            self._db.update_segment(seg.id, seg.downloaded_bytes, "completed")

    async def _download_one_segment(
        self, entry: DownloadEntry, seg: SegmentEntry,
        seg_progress: dict[int, int],
        start_time: float, start_downloaded: int,
        cancel_evt: asyncio.Event,
        start_delay: float = 0.0,
    ):
        use_curl = _HAS_CURL_CFFI and (
            _requires_curl_impersonation(entry.url)
            or entry.metadata.get("use_curl_cffi")
        )

        # This segment's slice of the launch stagger (see `_segment_stagger_step`). It waits
        # *here*, ahead of the retry ladder, for two reasons: the wait is paid once on the
        # initial request and a retry is not charged again on top of its own backoff, and a
        # cancel arriving during the wait stays a CancelledError instead of being caught by
        # the ladder as if it were a transport failure. The ladder's opening `cancel_evt`
        # check also covers a pause that lands while waiting, so a segment cancelled before it
        # transferred is never sent a request at all.
        if start_delay > 0 and not cancel_evt.is_set():
            await asyncio.sleep(start_delay)

        max_retries = self._get_max_retries(entry)
        # The most recent transport failure, carried into the terminal raise so the row's
        # error_message names a cause rather than only a retry count.
        last_error: BaseException | None = None
        for attempt in range(max_retries):
            if cancel_evt.is_set():
                # Interrupted before finishing: report it as paused rather than
                # leaving the details panel claiming it is still pending.
                if seg.status == "downloading":
                    seg.status = "paused"
                    self._db.update_segment(seg.id, seg.downloaded_bytes, "paused")
                return
            try:
                current_start = seg.start_byte + seg.downloaded_bytes
                if current_start > seg.end_byte:
                    seg.status = "completed"
                    self._db.update_segment(seg.id, seg.downloaded_bytes,
                                            "completed")
                    return

                # The segment is about to transfer. Without this the status stayed
                # "pending" for the whole download and only flipped to "completed"
                # at the very end, so the details panel looked frozen even though
                # bytes were arriving.
                if seg.status != "downloading":
                    seg.status = "downloading"
                    self._db.update_segment(seg.id, seg.downloaded_bytes,
                                            "downloading")

                headers = {
                    "Range": f"bytes={current_start}-{seg.end_byte}"
                }

                if use_curl:
                    await self._download_segment_curl(
                        entry, seg, current_start, headers,
                        seg_progress, start_time, start_downloaded, cancel_evt,
                    )
                    return

                session = await self._session_for_entry(entry)
                async with session.get(
                    entry.url, **self._request_kwargs(headers, entry=entry, url=entry.url)
                ) as resp:
                    if resp.status == 416:
                        raise _FallbackToSingle("416 Range Not Satisfiable")
                    if resp.status == 403:
                        if _HAS_CURL_CFFI and not use_curl:
                            entry.metadata["use_curl_cffi"] = True
                            use_curl = True
                            continue
                        raise _FallbackToSingle("403 Forbidden on range request")
                    if resp.status not in (200, 206):
                        raise aiohttp.ClientError(
                            f"Unexpected status {resp.status}"
                        )
                    if resp.status == 200 and seg.index > 0:
                        # Server ignoring range → fallback
                        raise _FallbackToSingle(
                            "Server returned 200 instead of 206"
                        )

                    file_path = Path(entry.file_path)
                    async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                        if cancel_evt.is_set():
                            # Save progress and exit
                            seg.status = "paused"
                            self._db.update_segment(
                                seg.id, seg.downloaded_bytes, "paused"
                            )
                            total_dl = sum(seg_progress.values())
                            if total_dl > 0:
                                self._db.update_progress(entry.id, total_dl)
                            return

                        with open(file_path, "r+b") as f:
                            f.seek(current_start)
                            f.write(chunk)

                        chunk_len = len(chunk)
                        current_start += chunk_len
                        seg.downloaded_bytes += chunk_len
                        seg_progress[seg.index] = seg.downloaded_bytes

                        eff_limit = self._get_effective_download_limit(entry)
                        if eff_limit > 0:
                            expected_time = chunk_len / eff_limit
                            if expected_time > 0.001:
                                await asyncio.sleep(min(expected_time, 1.0))

                        # Aggregate progress
                        total_dl = sum(seg_progress.values())
                        elapsed = time.monotonic() - start_time
                        speed = (
                            (total_dl - start_downloaded) / elapsed
                            if elapsed > 0 else 0
                        )
                        remaining = entry.total_size - total_dl
                        eta = remaining / speed if speed > 0 else 0

                        self._emit_progress(
                            entry.id, total_dl,
                            entry.total_size, speed, eta,
                        )

                seg.status = "completed"
                self._db.update_segment(
                    seg.id, seg.downloaded_bytes, "completed"
                )

                # Persist aggregate progress
                total_dl = sum(seg_progress.values())
                self._db.update_progress(entry.id, total_dl)
                return

            except _FallbackToSingle:
                raise
            except asyncio.CancelledError:
                seg.status = "paused"
                self._db.update_segment(
                    seg.id, seg.downloaded_bytes, "paused"
                )
                total_dl = sum(seg_progress.values())
                if total_dl > 0:
                    self._db.update_progress(entry.id, total_dl)
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                if cancel_evt.is_set():
                    return
                delay = self._get_retry_delay(attempt)
                log.warning(
                    "Segment %d attempt %d failed: %s — retrying in %.1fs",
                    seg.index, attempt + 1, exc, delay,
                )
                # Remember the cause: the terminal raise below is what reaches the row's
                # error_message, and without this the user is shown a retry count instead of
                # a diagnosable reason.
                last_error = exc
                # In-memory too: the details panel reads the live SegmentEntry,
                # so a DB-only update would leave the row showing a stale status.
                if seg.status == "downloading":
                    seg.status = "pending"
                self._db.update_segment(
                    seg.id, seg.downloaded_bytes, "pending"
                )
                await asyncio.sleep(delay)

        # Exhausted retries for this segment
        seg.status = "error"
        self._db.update_segment(seg.id, seg.downloaded_bytes, "error")
        raise Exception(
            f"Segment {seg.index} failed after {max_retries} retries"
            + (f": {last_error}" if last_error is not None else "")
        )

    @staticmethod
    def _create_segments(download_id: str, total_size: int,
                         num_segments: int) -> list[SegmentEntry]:
        seg_size = total_size // num_segments
        segments = []
        for i in range(num_segments):
            start = i * seg_size
            end = (total_size - 1) if i == num_segments - 1 else (
                start + seg_size - 1
            )
            segments.append(SegmentEntry(
                id=str(uuid.uuid4()),
                download_id=download_id,
                index=i,
                start_byte=start,
                end_byte=end,
            ))
        return segments

    # -- single-stream download ----------------------------------------------

    async def _single_download_curl(
        self, entry: DownloadEntry, file_path: Path,
        existing_size: int, headers: dict, mode: str,
        start_time: float, start_downloaded: int,
        cancel_evt: asyncio.Event,
    ):
        req_kwargs = self._request_kwargs(headers, entry=entry, url=entry.url)
        c_headers = req_kwargs.get("headers", {})
        async with CurlAsyncSession(impersonate="chrome124") as cs:
            resp = await cs.get(entry.url, headers=c_headers, stream=True, timeout=READ_TIMEOUT)
            if resp.status_code not in (200, 206):
                raise Exception(f"Status {resp.status_code} in curl single download")

            total_from_header = resp.headers.get("content-length")
            if resp.status_code == 200 and total_from_header:
                entry.total_size = int(total_from_header)
            elif resp.status_code == 206:
                cr = resp.headers.get("content-range", "")
                if "/" in cr:
                    entry.total_size = int(cr.split("/")[-1])

            meta = entry.metadata if hasattr(entry, "metadata") else {}
            has_explicit_fn = meta.get("explicit_filename", False)
            if not has_explicit_fn:
                get_filename = self._extract_filename_from_headers(resp.headers, entry.url)
                if get_filename and get_filename != entry.filename:
                    existing_entries = self._db.get_all_downloads()
                    reserved = {
                        d.filename for d in existing_entries
                        if d.id != entry.id and d.save_path == entry.save_path and d.filename
                    }
                    unique_fn = get_unique_filename(entry.save_path, get_filename, reserved_names=reserved)
                    if unique_fn != entry.filename:
                        entry.filename = unique_fn
                        entry.file_path = str(Path(entry.save_path) / entry.filename)
                        file_path = Path(entry.file_path)
                        self._emit_filename(entry.id, entry.filename)

            self._db.update_download(entry)
            downloaded = existing_size

            with open(file_path, mode) as f:
                async for chunk in resp.aiter_content():
                    if cancel_evt.is_set():
                        self._db.update_progress(entry.id, downloaded)
                        return

                    f.write(chunk)
                    downloaded += len(chunk)

                    eff_limit = self._get_effective_download_limit(entry)
                    if eff_limit > 0:
                        expected_time = len(chunk) / eff_limit
                        if expected_time > 0.001:
                            await asyncio.sleep(min(expected_time, 1.0))

                    elapsed = time.monotonic() - start_time
                    speed = ((downloaded - start_downloaded) / elapsed if elapsed > 0 else 0)
                    remaining = (entry.total_size - downloaded) if entry.total_size else 0
                    eta = remaining / speed if speed > 0 else 0

                    entry.downloaded_size = downloaded
                    entry.speed = speed
                    entry.eta_seconds = eta
                    self._emit_progress(entry.id, downloaded, entry.total_size, speed, eta)

            self._db.update_progress(entry.id, downloaded)

    async def _single_download(self, entry: DownloadEntry,
                               cancel_evt: asyncio.Event):
        file_path = Path(entry.file_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        use_curl = _HAS_CURL_CFFI and (
            _requires_curl_impersonation(entry.url)
            or entry.metadata.get("use_curl_cffi")
        )

        # Resume: start from existing file size
        existing_size = 0
        if file_path.exists():
            existing_size = file_path.stat().st_size

        headers = {}
        if existing_size > 0:
            headers["Range"] = f"bytes={existing_size}-"

        start_time = time.monotonic()

        max_single_retries = self._get_max_retries(entry)
        # Set after a 416 proves the on-disk prefix is not a valid resume point. The retry
        # loop recomputes `headers` from `file_path.stat().st_size` on every attempt, so
        # popping the header alone was undone on the next pass and the same stale Range was
        # re-sent until the ladder gave up.
        force_restart = False
        # See _download_one_segment: surfaced in the terminal raise below.
        last_error: BaseException | None = None
        for attempt in range(max_single_retries):
            if cancel_evt.is_set():
                return
            try:
                # Refresh existing size on disk for accurate resume and headers on each attempt
                existing_size = 0 if force_restart else (
                    file_path.stat().st_size if file_path.exists() else 0
                )
                headers = {}
                if existing_size > 0:
                    headers["Range"] = f"bytes={existing_size}-"

                start_time = time.monotonic()
                start_downloaded = existing_size
                mode = "ab" if existing_size > 0 else "wb"

                if use_curl:
                    await self._single_download_curl(
                        entry, file_path, existing_size, headers, mode,
                        start_time, start_downloaded, cancel_evt,
                    )
                    return

                session = await self._session_for_entry(entry)
                async with session.get(
                    entry.url, **self._request_kwargs(headers, entry=entry, url=entry.url)
                ) as resp:
                    if resp.status == 403 and _HAS_CURL_CFFI and not use_curl:
                        entry.metadata["use_curl_cffi"] = True
                        use_curl = True
                        continue
                    if resp.status == 416:
                        # 416 Range Not Satisfiable: file may already be complete
                        if entry.total_size and existing_size >= entry.total_size:
                            entry.downloaded_size = entry.total_size
                            return
                        # Or the range is stale (the resource changed, or we hold a partial
                        # file the server will not extend): restart the whole body.
                        force_restart = True
                        existing_size = 0
                        start_downloaded = 0
                        continue
                    if resp.status not in (200, 206):
                        raise aiohttp.ClientError(
                            f"Status {resp.status}"
                        )

                    if resp.status == 200:
                        # Server doesn't support resume, start over
                        existing_size = 0
                        start_downloaded = 0
                        mode = "wb"
                    else:
                        mode = "ab"

                    total_from_header = resp.headers.get("Content-Length")
                    if resp.status == 200 and total_from_header:
                        entry.total_size = int(total_from_header)
                    elif resp.status == 206:
                        cr = resp.headers.get("Content-Range", "")
                        if "/" in cr:
                            entry.total_size = int(cr.split("/")[-1])

                    meta = entry.metadata if hasattr(entry, "metadata") else {}
                    has_explicit_fn = meta.get("explicit_filename", False)
                    if not has_explicit_fn:
                        get_filename = self._extract_filename_from_headers(
                            resp.headers, str(resp.url)
                        )
                        if get_filename and get_filename != entry.filename:
                            existing_entries = self._db.get_all_downloads()
                            reserved = {
                                d.filename for d in existing_entries
                                if d.id != entry.id and d.save_path == entry.save_path and d.filename
                            }
                            unique_fn = get_unique_filename(entry.save_path, get_filename, reserved_names=reserved)
                            if unique_fn != entry.filename:
                                entry.filename = unique_fn
                                entry.file_path = str(
                                    Path(entry.save_path) / entry.filename
                                )
                                file_path = Path(entry.file_path)
                                self._emit_filename(entry.id, entry.filename)

                    self._db.update_download(entry)
                    downloaded = existing_size

                    with open(file_path, mode) as f:
                        async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                            if cancel_evt.is_set():
                                self._db.update_progress(
                                    entry.id, downloaded
                                )
                                return

                            f.write(chunk)
                            downloaded += len(chunk)

                            eff_limit = self._get_effective_download_limit(entry)
                            if eff_limit > 0:
                                expected_time = len(chunk) / eff_limit
                                if expected_time > 0.001:
                                    await asyncio.sleep(min(expected_time, 1.0))

                            elapsed = time.monotonic() - start_time
                            speed = (
                                (downloaded - start_downloaded) / elapsed
                                if elapsed > 0 else 0
                            )
                            remaining = (
                                (entry.total_size - downloaded)
                                if entry.total_size else 0
                            )
                            eta = remaining / speed if speed > 0 else 0

                            self._emit_progress(
                                entry.id, downloaded,
                                entry.total_size, speed, eta,
                            )

                    entry.downloaded_size = downloaded
                    self._db.update_progress(entry.id, downloaded)
                    return

            except asyncio.CancelledError:
                if file_path.exists():
                    self._db.update_progress(entry.id, file_path.stat().st_size)
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                if cancel_evt.is_set():
                    return
                delay = self._get_retry_delay(attempt)
                log.warning(
                    "Single-stream attempt %d failed: %s — retrying in %.1fs",
                    attempt + 1, exc, delay,
                )
                last_error = exc
                await asyncio.sleep(delay)

        raise Exception(
            f"Single-stream download failed after {max_single_retries} retries"
            + (f": {last_error}" if last_error is not None else "")
        )

    # -- retry handling -------------------------------------------------------

    def _handle_retry(self, entry: DownloadEntry, error_msg: str):
        current = self._db.get_download(entry.id)
        if current and current.status in ("paused", "completed"):
            log.debug("Skipping retry for %s because status is %s", entry.id, current.status)
            return

        count = self._db.increment_retry(entry.id)
        if count < entry.max_retries:
            delay = self._general_config.get_retry_delay(count - 1)
            entry.metadata["next_retry_at"] = time.time() + delay
            entry.metadata["retry_delay"] = delay
            # Carry the new count onto the in-memory entry. update_download writes every
            # column, so the caller's stale retry_count would otherwise be written back over
            # the increment - leaving the counter at 0 forever, which makes the exhaustion
            # check below unreachable and re-queues a failing download indefinitely.
            entry.retry_count = count
            self._db.update_download(entry)
            msg = (
                f"Retrying in {int(delay)}s ({count}/{entry.max_retries}): {error_msg}"
                if delay >= 1
                else f"Retrying ({count}/{entry.max_retries}): {error_msg}"
            )
            self._db.update_status(entry.id, "queued", msg)
            self._emit_status(entry.id, "queued", msg)
            log.info(
                "Download %s retry %d/%d scheduled in %.1fs: %s",
                entry.id, count, entry.max_retries, delay, error_msg,
            )
        else:
            self._db.update_status(entry.id, "error", error_msg)
            self._emit_status(entry.id, "error", error_msg)
            log.error(
                "Download %s exhausted retries: %s", entry.id, error_msg
            )

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _filename_from_url(url: str) -> str:
        parsed = urlparse(url)
        path = parsed.path
        name = unquote(path.split("/")[-1]) if path else ""
        if not name or "." not in name:
            qs = parse_qs(parsed.query)
            for key in ("file", "filename", "name", "title"):
                val = qs.get(key)
                if val and val[0]:
                    cand = unquote(val[0])
                    if "." in cand:
                        return Path(cand).name
        return name or "download"

    @staticmethod
    def _extract_filename_from_headers(headers, final_url: str = "") -> str:
        cd = headers.get("Content-Disposition", "")
        filename = ""
        if cd:
            if "filename*=" in cd:
                part = cd.split("filename*=")[-1].split(";")[0].strip().strip('"\'')
                if "''" in part:
                    _, _, encoded = part.partition("''")
                    filename = unquote(encoded)
                else:
                    filename = unquote(part)
            elif "filename=" in cd:
                part = cd.split("filename=")[-1].split(";")[0].strip().strip('"\'')
                filename = unquote(part)

        if not filename and final_url:
            parsed = urlparse(final_url)
            path = unquote(parsed.path)
            name = Path(path).name
            if name and "." in name:
                filename = name
            else:
                qs = parse_qs(parsed.query)
                for key in ("file", "filename", "name", "title"):
                    val = qs.get(key)
                    if val and val[0]:
                        cand = unquote(val[0])
                        if "." in cand:
                            filename = Path(cand).name
                            break
                if not filename and name:
                    filename = name
        return filename

    def _emit_progress(self, download_id: str, downloaded: int,
                       total: int, speed: float, eta: float):
        if not self._progress_cb:
            return
        cancel_evt = self._cancel_events.get(download_id)
        if cancel_evt and cancel_evt.is_set():
            return
        current = self._db.get_download(download_id)
        if current and current.status == "paused":
            speed = 0.0
            eta = 0.0
        now = time.monotonic()
        last = self._last_progress_emit.get(download_id, 0.0)
        # Throttle to at most 10 emits/sec (100ms interval) unless download is finished
        if now - last < 0.1 and (total <= 0 or downloaded < total):
            return
        self._last_progress_emit[download_id] = now
        self._progress_cb(download_id, downloaded, total, speed, eta)

    def _emit_status(self, download_id: str, status: str,
                     error_msg: str = ""):
        if self._status_cb:
            self._status_cb(download_id, status, error_msg)

    def _emit_filename(self, download_id: str, filename: str):
        if self._filename_cb:
            self._filename_cb(download_id, filename)


class _FallbackToSingle(Exception):
    """Sentinel to trigger fallback from segmented to single-stream."""
    pass
