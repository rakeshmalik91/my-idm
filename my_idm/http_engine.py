"""HTTP download engine with segmented multi-connection downloads and fallback."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional, Callable
from urllib.parse import unquote, urlparse

import aiohttp

from my_idm.database import Database, DownloadEntry, SegmentEntry

log = logging.getLogger(__name__)

# Defaults
DEFAULT_SEGMENTS = 8
CHUNK_SIZE = 64 * 1024          # 64 KiB per read
MAX_RETRIES_PER_SEGMENT = 5
RETRY_BASE_DELAY = 1.0          # seconds, exponential backoff base
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 60


ProgressCallback = Callable[
    [str, int, int, float, float],  # download_id, downloaded, total, speed, eta
    None
]
StatusCallback = Callable[[str, str, str], None]  # download_id, status, error_msg


class HTTPEngine:
    """Manages HTTP(S) downloads with segmented parallel streaming."""

    def __init__(self, db: Database, max_concurrent: int = 3):
        self._db = db
        self._max_concurrent = max_concurrent
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._progress_cb: Optional[ProgressCallback] = None
        self._status_cb: Optional[StatusCallback] = None

    # -- public API ----------------------------------------------------------

    def set_callbacks(self, progress_cb: ProgressCallback,
                      status_cb: StatusCallback):
        self._progress_cb = progress_cb
        self._status_cb = status_cb

    async def start(self):
        timeout = aiohttp.ClientTimeout(
            connect=CONNECT_TIMEOUT, sock_read=READ_TIMEOUT
        )
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": "My-IDM/1.0"},
        )

    async def stop(self):
        for did in list(self._tasks):
            await self.pause(did)
        if self._session:
            await self._session.close()
            self._session = None

    async def add(self, entry: DownloadEntry):
        """Start (or resume) an HTTP download."""
        cancel_evt = asyncio.Event()
        self._cancel_events[entry.id] = cancel_evt
        task = asyncio.create_task(self._run_download(entry, cancel_evt))
        self._tasks[entry.id] = task

    async def pause(self, download_id: str):
        evt = self._cancel_events.pop(download_id, None)
        if evt:
            evt.set()
        task = self._tasks.pop(download_id, None)
        if task and not task.done():
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        self._db.update_status(download_id, "paused")
        self._emit_status(download_id, "paused")

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

    # -- download logic ------------------------------------------------------

    async def _run_download(self, entry: DownloadEntry,
                            cancel_evt: asyncio.Event):
        download_id = entry.id
        try:
            self._db.update_status(download_id, "downloading")
            self._emit_status(download_id, "downloading")

            # Probe the URL
            supports_range, total_size, etag, filename = await self._probe_url(
                entry.url
            )

            # Update entry with discovered info
            if total_size and total_size != entry.total_size:
                entry.total_size = total_size
            if etag and not entry.etag:
                entry.etag = etag
            if filename and not entry.filename:
                entry.filename = filename
            if not entry.filename:
                entry.filename = self._filename_from_url(entry.url)
            if not entry.file_path:
                entry.file_path = str(
                    Path(entry.save_path) / entry.filename
                )
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
                entry.status = "completed"
                entry.downloaded_size = entry.total_size or entry.downloaded_size
                self._db.update_status(download_id, "completed")
                self._emit_status(download_id, "completed")
                self._emit_progress(
                    download_id, entry.downloaded_size,
                    entry.total_size, 0.0, 0.0
                )

        except asyncio.CancelledError:
            log.debug("Download %s cancelled", download_id)
        except Exception as exc:
            log.exception("Download %s failed: %s", download_id, exc)
            self._handle_retry(entry, str(exc))
        finally:
            self._tasks.pop(download_id, None)
            self._cancel_events.pop(download_id, None)

    async def _probe_url(self, url: str):
        """HEAD request to discover file size, range support, ETag, filename."""
        supports_range = False
        total_size = 0
        etag = ""
        filename = ""

        try:
            async with self._session.head(
                url, allow_redirects=True
            ) as resp:
                if resp.status == 200:
                    total_size = int(
                        resp.headers.get("Content-Length", 0)
                    )
                    ar = resp.headers.get("Accept-Ranges", "").lower()
                    supports_range = ar == "bytes"
                    etag = resp.headers.get("ETag", "")

                    cd = resp.headers.get("Content-Disposition", "")
                    if "filename=" in cd:
                        filename = cd.split("filename=")[-1].strip().strip('"\'')
        except Exception as exc:
            log.warning("HEAD request failed for %s: %s", url, exc)
            # Will fall back to single download on GET

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

            # Pre-allocate file
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "wb") as f:
                f.truncate(total_size)

        # Track progress per segment
        seg_progress: dict[int, int] = {
            s.index: s.downloaded_bytes for s in segments
        }
        start_time = time.monotonic()
        start_downloaded = sum(seg_progress.values())

        sem = asyncio.Semaphore(num_segments)

        async def download_segment(seg: SegmentEntry):
            if seg.status == "completed":
                return
            async with sem:
                await self._download_one_segment(
                    entry, seg, seg_progress, start_time,
                    start_downloaded, cancel_evt,
                )

        pending = [
            s for s in segments if s.status != "completed"
        ]
        if not pending:
            return  # All segments done

        tasks = [
            asyncio.create_task(download_segment(s)) for s in pending
        ]

        try:
            await asyncio.gather(*tasks)
        except _FallbackToSingle:
            for t in tasks:
                if not t.done():
                    t.cancel()
            raise

    async def _download_one_segment(
        self, entry: DownloadEntry, seg: SegmentEntry,
        seg_progress: dict[int, int],
        start_time: float, start_downloaded: int,
        cancel_evt: asyncio.Event,
    ):
        for attempt in range(MAX_RETRIES_PER_SEGMENT):
            if cancel_evt.is_set():
                return
            try:
                current_start = seg.start_byte + seg.downloaded_bytes
                if current_start > seg.end_byte:
                    seg.status = "completed"
                    self._db.update_segment(seg.id, seg.downloaded_bytes,
                                            "completed")
                    return

                headers = {
                    "Range": f"bytes={current_start}-{seg.end_byte}"
                }
                async with self._session.get(
                    entry.url, headers=headers
                ) as resp:
                    if resp.status == 416:
                        raise _FallbackToSingle("416 Range Not Satisfiable")
                    if resp.status == 403:
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
                            self._db.update_segment(
                                seg.id, seg.downloaded_bytes, "pending"
                            )
                            return

                        with open(file_path, "r+b") as f:
                            f.seek(current_start)
                            f.write(chunk)

                        chunk_len = len(chunk)
                        current_start += chunk_len
                        seg.downloaded_bytes += chunk_len
                        seg_progress[seg.index] = seg.downloaded_bytes

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
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                log.warning(
                    "Segment %d attempt %d failed: %s — retrying in %.1fs",
                    seg.index, attempt + 1, exc, delay,
                )
                self._db.update_segment(
                    seg.id, seg.downloaded_bytes, "pending"
                )
                await asyncio.sleep(delay)

        # Exhausted retries for this segment
        seg.status = "error"
        self._db.update_segment(seg.id, seg.downloaded_bytes, "error")
        raise Exception(
            f"Segment {seg.index} failed after {MAX_RETRIES_PER_SEGMENT} retries"
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

    async def _single_download(self, entry: DownloadEntry,
                               cancel_evt: asyncio.Event):
        file_path = Path(entry.file_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Resume: start from existing file size
        existing_size = 0
        if file_path.exists():
            existing_size = file_path.stat().st_size

        headers = {}
        if existing_size > 0:
            headers["Range"] = f"bytes={existing_size}-"

        start_time = time.monotonic()

        for attempt in range(MAX_RETRIES_PER_SEGMENT):
            if cancel_evt.is_set():
                return
            try:
                async with self._session.get(
                    entry.url, headers=headers
                ) as resp:
                    if resp.status == 416:
                        # File already complete
                        return
                    if resp.status not in (200, 206):
                        raise aiohttp.ClientError(
                            f"Status {resp.status}"
                        )

                    if resp.status == 200:
                        # Server doesn't support resume, start over
                        existing_size = 0
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

                            elapsed = time.monotonic() - start_time
                            speed = (
                                (downloaded - existing_size) / elapsed
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

            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                log.warning(
                    "Single-stream attempt %d failed: %s — retrying in %.1fs",
                    attempt + 1, exc, delay,
                )
                await asyncio.sleep(delay)

        raise Exception(
            f"Single-stream download failed after {MAX_RETRIES_PER_SEGMENT} retries"
        )

    # -- retry handling -------------------------------------------------------

    def _handle_retry(self, entry: DownloadEntry, error_msg: str):
        count = self._db.increment_retry(entry.id)
        if count < entry.max_retries:
            self._db.update_status(entry.id, "queued", error_msg)
            self._emit_status(entry.id, "queued", error_msg)
            log.info(
                "Download %s retry %d/%d queued",
                entry.id, count, entry.max_retries,
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
        path = urlparse(url).path
        name = unquote(path.split("/")[-1]) if path else ""
        return name or "download"

    def _emit_progress(self, download_id: str, downloaded: int,
                       total: int, speed: float, eta: float):
        if self._progress_cb:
            self._progress_cb(download_id, downloaded, total, speed, eta)

    def _emit_status(self, download_id: str, status: str,
                     error_msg: str = ""):
        if self._status_cb:
            self._status_cb(download_id, status, error_msg)


class _FallbackToSingle(Exception):
    """Sentinel to trigger fallback from segmented to single-stream."""
    pass
