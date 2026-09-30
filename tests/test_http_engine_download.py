"""Tests for the HTTPEngine download pipeline: probe, segmented, single-stream, retry.

``test_http_engine.py`` covers the pure helpers (segment arithmetic, header assembly,
retry-delay maths, lifecycle bookkeeping). The code that actually *moves bytes* -
``_run_download``, ``_probe_url``, ``_segmented_download``,
``_download_one_segment``, ``_single_download``, ``_handle_retry`` and
``_emit_progress`` - had no coverage at all, so the whole resume / retry / fallback
machinery shipped unverified.

These tests drive it through ``tests.fake_http``: a scripted aiohttp double that serves
responses from a queue, records the exact requests, and refuses any unscripted call. That
last property matters - it means a test asserting "the download completed" also proves
the engine issued exactly the requests it was scripted to receive, rather than passing
because a permissive mock absorbed anything.

Design rules obeyed throughout (see ``.agents/AGENTS.md``):

* No ``time.sleep`` and no wall-clock budget. The retry backoff is stubbed to a
  no-op that records the requested delay, so the ladder and its branch order stay under
  test while the suite does not sit through 1s/2s/4s sleeps.
* No real network: every socket is the fake session, so the loopback-only guard in
  ``conftest.py`` is never even reached.
* No real filesystem outside the per-test temp dir: downloads land there.
* Real ``DownloadEntry`` / ``GeneralConfig`` / ``NetworkConfig`` objects, never
  ``MagicMock``, wherever the engine compares values against ints.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp

from PySide6.QtWidgets import QApplication

from my_idm import http_engine as http_engine_module
from my_idm.config import GeneralConfig, TorConfig
from my_idm.database import Database, DownloadEntry, SegmentEntry
from my_idm.http_engine import (
    CHUNK_SIZE,
    DEFAULT_SEGMENTS,
    HTTPEngine,
    _FallbackToSingle,
    _requires_curl_impersonation,
)
from my_idm.network import NetworkConfig
from tests.fake_http import (
    FakeCurlResponse,
    FakeResponse,
    FakeSession,
    curl_session_factory,
    run_async,
)

app = QApplication.instance() or QApplication(sys.argv)


async def _async_noop(*args, **kwargs):
    return None


def canceller_after(chunks: int, event: asyncio.Event):
    """Return an ``on_chunk`` hook that trips *event* once *chunks* have been seen.

    The engine checks its cancel flag at the top of every chunk, *before* writing. Tripping
    the flag from the first chunk therefore simulates "cancelled before any byte landed";
    tripping it on the Nth chunk simulates "cancelled mid-transfer, keep what arrived".
    """
    seen: list[bytes] = []

    def _hook(chunk: bytes):
        seen.append(chunk)
        if len(seen) >= chunks:
            event.set()

    return _hook


class EngineTestCase(unittest.TestCase):
    """Shared engine, temp dir, callback wiring, and a zeroed retry backoff."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)

        self.progress: list[tuple] = []
        self.statuses: list[tuple] = []
        self.filenames: list[tuple] = []

        self.engine = HTTPEngine(self.db)
        self.engine.set_callbacks(
            progress_cb=lambda *a: self.progress.append(a),
            status_cb=lambda *a: self.statuses.append(a),
            filename_cb=lambda *a: self.filenames.append(a),
        )
        self.engine.set_general_config_sync(GeneralConfig())

        # Record backoff requests instead of honouring them. The engine still takes the
        # throttled branch; the suite just does not burn wall-clock time on it.
        self.sleeps: list[float] = []
        real_sleep = asyncio.sleep  # captured before the patch, or we recurse forever

        async def _sleep(delay, *args, **kwargs):
            self.sleeps.append(delay)
            await real_sleep(0)

        patcher = patch("asyncio.sleep", new=_sleep)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- helpers --------------------------------------------------------------

    def add_entry(self, entry_id="d1", url="https://example.com/a.zip", **kw):
        base = dict(
            id=entry_id,
            url=url,
            filename="a.zip",
            save_path=self.tmp.as_posix(),
            file_path=(self.tmp / "a.zip").as_posix(),
            total_size=0,
            status="queued",
        )
        base.update(kw)
        entry = DownloadEntry(**base)
        self.db.add_download(entry)
        return entry

    def entry(self, entry_id="d1"):
        return self.db.get_download(entry_id)

    def use_session(self, **kwargs) -> FakeSession:
        session = FakeSession(**kwargs)
        self.engine._session = session
        self.addCleanup(self._drop_session, session)
        return session

    def _drop_session(self, session):
        if self.engine._session is session:
            self.engine._session = None

    def status_trail(self, entry_id="d1"):
        return [s for s in self.statuses if s[0] == entry_id]

    def run_segment(self, entry, seg, cancel_evt=None, start_downloaded=0):
        return run_async(
            self.engine._download_one_segment(
                entry, seg, {seg.index: seg.downloaded_bytes}, time.monotonic(),
                start_downloaded, cancel_evt or asyncio.Event(),
            )
        )


class TestEffectiveDownloadLimit(EngineTestCase):
    """Per-download bandwidth allocation applied to the global rate limit."""

    def test_zero_limit_means_unlimited(self):
        self.engine.set_download_limit(0)
        self.assertEqual(self.engine._get_effective_download_limit(self.add_entry()), 0)

    def test_allocation_fractions(self):
        self.engine.set_download_limit(1000)
        entry = self.add_entry()
        for alloc, expected in (
            ("low", 250), ("medium", 500), ("high", 750), ("max", 1000),
        ):
            entry.metadata["bandwidth_allocation"] = alloc
            self.assertEqual(self.engine._get_effective_download_limit(entry), expected, alloc)

    def test_allocation_is_case_insensitive(self):
        self.engine.set_download_limit(400)
        entry = self.add_entry()
        entry.metadata["bandwidth_allocation"] = "LOW"
        self.assertEqual(self.engine._get_effective_download_limit(entry), 100)

    def test_unknown_allocation_defaults_to_full_limit(self):
        """An unrecognised value must not silently throttle the download to nothing."""
        self.engine.set_download_limit(800)
        entry = self.add_entry()
        entry.metadata["bandwidth_allocation"] = "turbo"
        self.assertEqual(self.engine._get_effective_download_limit(entry), 800)

    def test_missing_allocation_key_defaults_to_full_limit(self):
        self.engine.set_download_limit(640)
        self.assertEqual(self.engine._get_effective_download_limit(self.add_entry()), 640)

    def test_set_download_bandwidth_allocation_persists_to_db(self):
        self.add_entry()
        self.engine.set_download_bandwidth_allocation("d1", "high")
        self.assertEqual(
            self.db.get_download("d1").metadata.get("bandwidth_allocation"), "high"
        )

    def test_set_download_bandwidth_allocation_ignores_unknown_id(self):
        self.engine.set_download_bandwidth_allocation("nope", "high")  # must not raise


class TestPerDownloadTorRoute(EngineTestCase):
    """The per-download Tor flag, which is independent of the global Tor switch."""

    def test_flag_is_persisted(self):
        self.add_entry()
        self.engine.set_download_tor_route("d1", True)
        self.assertTrue(self.db.get_download("d1").metadata.get("route_through_tor"))

    def test_flag_is_cleared(self):
        self.add_entry()
        self.engine.set_download_tor_route("d1", True)
        self.engine.set_download_tor_route("d1", False)
        self.assertFalse(self.db.get_download("d1").metadata.get("route_through_tor"))

    def test_setting_the_same_value_does_not_rewrite_the_row(self):
        """Re-applying the current value is a no-op, not a redundant UPDATE."""
        self.add_entry()
        self.engine.set_download_tor_route("d1", True)
        before = self.entry().metadata_json
        self.engine.set_download_tor_route("d1", True)
        self.assertEqual(self.entry().metadata_json, before)

    def test_unknown_id_is_safe(self):
        self.add_entry()
        self.engine.set_download_tor_route("nope", True)  # must not raise

    def test_default_is_unrouted(self):
        self.add_entry()
        self.assertFalse(self.engine._is_tor_routed(self.entry()))

    def test_none_entry_is_unrouted(self):
        self.assertFalse(self.engine._is_tor_routed(None))

    def test_corrupt_metadata_is_treated_as_unrouted(self):
        entry = self.add_entry()
        entry.metadata_json = "not json at all"
        self.assertFalse(self.engine._is_tor_routed(entry))

    def test_session_for_entry_falls_back_when_no_tor_session_exists(self):
        """Flagged for Tor while Tor is globally off must use the default session."""
        self.add_entry()
        session = self.use_session()
        self.engine.set_tor_config_sync(TorConfig(enabled=False))
        entry = self.entry()
        entry.metadata["route_through_tor"] = True
        self.assertIs(run_async(self.engine._session_for_entry(entry)), session)

    def test_tor_session_is_none_when_tor_is_disabled(self):
        self.engine.set_tor_config_sync(TorConfig(enabled=False))
        self.assertIsNone(run_async(self.engine._get_tor_session()))

    def test_tor_session_is_reused_across_calls(self):
        created: list = []

        class _Sess:
            def __init__(self):
                self.closed = False

            async def close(self):
                self.closed = True

        def _make(**kwargs):
            session = _Sess()
            created.append(session)
            return session

        self.engine.set_tor_config_sync(TorConfig(enabled=True))
        with patch.object(http_engine_module.aiohttp, "ClientSession", _make):
            first = run_async(self.engine._get_tor_session())
            second = run_async(self.engine._get_tor_session())
        self.assertIsNotNone(first)
        self.assertIs(first, second, "the SOCKS5 session must be reused, not rebuilt")
        self.assertEqual(len(created), 1)

    def test_close_tor_session_is_idempotent(self):
        closed: list[bool] = []

        class _Sess:
            def __init__(self):
                self.closed = False

            async def close(self):
                closed.append(True)
                self.closed = True

        async def scenario():
            self.engine._tor_session = _Sess()
            await self.engine._close_tor_session()
            await self.engine._close_tor_session()

        run_async(scenario())
        self.assertIsNone(self.engine._tor_session)
        self.assertEqual(len(closed), 1, "an already-closed session must not be closed twice")


class TestRequiresCurlImpersonation(unittest.TestCase):
    """The domain allow-list that switches a transfer onto curl_cffi's browser TLS."""

    def test_known_domains_match(self):
        for url in (
            "https://cdn.owocdn.top/x.mp4",
            "https://kwik.cx/file.mkv",
            "https://kwik.si/file.mkv",
            "https://x.pahe.win/file.mkv",
        ):
            self.assertTrue(_requires_curl_impersonation(url), url)

    def test_case_is_ignored(self):
        self.assertTrue(_requires_curl_impersonation("https://KWIK.CX/a.mkv"))

    def test_unrelated_host_does_not_match(self):
        self.assertFalse(_requires_curl_impersonation("https://example.com/a.zip"))

    def test_empty_url_does_not_match(self):
        self.assertFalse(_requires_curl_impersonation(""))


class TestProbeUrl(EngineTestCase):
    """HEAD probing: size, range support, ETag, filename."""

    def test_head_200_reports_everything(self):
        self.use_session(
            heads=[FakeResponse(
                200,
                headers={
                    "Content-Length": "2048",
                    "Accept-Ranges": "bytes",
                    "ETag": '"v1"',
                    "Content-Disposition": 'attachment; filename="report.pdf"',
                },
                url="https://example.com/redir/report.pdf",
            )]
        )
        supports, size, etag, filename = run_async(
            self.engine._probe_url("https://example.com/a.zip")
        )
        self.assertEqual((supports, size, etag, filename), (True, 2048, '"v1"', "report.pdf"))
        self.assertTrue(self.engine._session.head_calls[0]["allow_redirects"])

    def test_no_accept_ranges_means_unsupported(self):
        self.use_session(heads=[FakeResponse(200, headers={"Content-Length": "10"})])
        supports, size, _, _ = run_async(self.engine._probe_url("https://example.com/a.zip"))
        self.assertFalse(supports)
        self.assertEqual(size, 10)

    def test_non_200_head_yields_nothing_but_does_not_raise(self):
        self.use_session(heads=[FakeResponse(404)])
        self.assertEqual(
            run_async(self.engine._probe_url("https://example.com/a.zip")),
            (False, 0, "", ""),
        )

    def test_transport_failure_degrades_to_an_empty_probe(self):
        """A dead server must not abort the download; it just means single-stream."""
        self.use_session(heads=[FakeResponse(raise_on_enter=OSError("refused"))])
        self.assertEqual(
            run_async(self.engine._probe_url("https://example.com/a.zip")),
            (False, 0, "", ""),
        )

    def test_curl_probe_is_preferred_when_metadata_requests_it(self):
        session = self.use_session()
        factory = curl_session_factory([
            FakeCurlResponse(200, headers={
                "content-length": "77", "accept-ranges": "bytes", "etag": '"c"',
            })
        ])
        entry = self.add_entry()
        entry.metadata["use_curl_cffi"] = True
        self.db.update_download(entry)
        with patch.object(http_engine_module, "CurlAsyncSession", factory):
            supports, size, etag, _ = run_async(
                self.engine._probe_url("https://example.com/a.zip", entry=entry)
            )
        self.assertEqual((supports, size, etag), (True, 77, '"c"'))
        self.assertEqual(session.head_calls, [], "the curl probe must skip the HEAD entirely")
        self.assertEqual(factory.created[0].kwargs.get("impersonate"), "chrome124")

    def test_curl_probe_failure_falls_back_to_the_head(self):
        session = self.use_session(
            heads=[FakeResponse(200, headers={"Content-Length": "5"})]
        )
        factory = curl_session_factory(raise_on_get=RuntimeError("tls handshake failed"))
        entry = self.add_entry()
        entry.metadata["use_curl_cffi"] = True
        self.db.update_download(entry)
        with patch.object(http_engine_module, "CurlAsyncSession", factory):
            _, size, _, _ = run_async(
                self.engine._probe_url("https://example.com/a.zip", entry=entry)
            )
        self.assertEqual(size, 5, "the aiohttp probe must still run")
        self.assertEqual(len(session.head_calls), 1)

    def test_curl_probe_of_a_206_is_accepted(self):
        self.use_session()
        factory = curl_session_factory([
            FakeCurlResponse(206, headers={"content-length": "10", "accept-ranges": "bytes"})
        ])
        entry = self.add_entry()
        entry.metadata["use_curl_cffi"] = True
        self.db.update_download(entry)
        with patch.object(http_engine_module, "CurlAsyncSession", factory):
            supports, size, _, _ = run_async(
                self.engine._probe_url("https://example.com/a.zip", entry=entry)
            )
        self.assertEqual((supports, size), (True, 10))

    def test_403_head_falls_through_without_curl_available(self):
        """A Cloudflare 403 must not abort the download when curl_cffi is unavailable."""
        self.use_session(heads=[FakeResponse(403)])
        entry = self.add_entry()
        with patch.object(http_engine_module, "_HAS_CURL_CFFI", False):
            self.assertEqual(
                run_async(self.engine._probe_url("https://example.com/a.zip", entry=entry)),
                (False, 0, "", ""),
            )

    def test_403_head_uses_the_curl_fallback(self):
        self.use_session(heads=[FakeResponse(403)])
        factory = curl_session_factory([
            FakeCurlResponse(200, headers={"content-length": "42", "accept-ranges": "bytes"})
        ])
        with patch.object(http_engine_module, "CurlAsyncSession", factory):
            _, size, _, _ = run_async(
                self.engine._probe_url("https://example.com/a.zip", entry=self.add_entry())
            )
        self.assertEqual(size, 42)
        self.assertEqual(len(factory.created), 1)

    def test_403_head_curl_fallback_failure_degrades_quietly(self):
        self.use_session(heads=[FakeResponse(403)])
        factory = curl_session_factory(raise_on_get=RuntimeError("nope"))
        with patch.object(http_engine_module, "CurlAsyncSession", factory):
            self.assertEqual(
                run_async(self.engine._probe_url("https://example.com/a.zip")),
                (False, 0, "", ""),
            )

    def test_probe_carries_the_configured_proxy(self):
        self.use_session(heads=[FakeResponse(200)])
        self.engine.set_network_config_sync(
            NetworkConfig(proxy_enabled=True, proxy_host="p", proxy_port=3128)
        )
        run_async(self.engine._probe_url("https://example.com/a.zip"))
        self.assertEqual(self.engine._session.head_calls[0]["proxy"], "http://p:3128")


class TestSegmentedDownload(EngineTestCase):
    """Segment bookkeeping, file pre-allocation, and live-segment exposure."""

    def _entry(self, total=40, segments=4):
        return self.add_entry(total_size=total, num_segments=segments)

    def test_segments_are_created_and_the_file_is_presized(self):
        entry = self._entry()
        session = self.use_session()
        for char in (b"a", b"b", b"c", b"d"):
            session.queue_get(FakeResponse(206, chunks=[char * 10], url=entry.url))

        run_async(self.engine._segmented_download(entry, asyncio.Event()))

        segs = self.db.get_segments("d1")
        self.assertEqual(len(segs), 4)
        self.assertTrue(all(s.status == "completed" for s in segs), segs)
        target = Path(entry.file_path)
        self.assertEqual(target.stat().st_size, 40, "the file is truncated to total_size")
        self.assertEqual(target.read_bytes(), b"a" * 10 + b"b" * 10 + b"c" * 10 + b"d" * 10)
        self.assertEqual(self.entry().downloaded_size, 40)

    def test_range_headers_cover_each_segment_exactly_once(self):
        entry = self._entry(total=40, segments=4)
        session = self.use_session()
        for _ in range(4):
            session.queue_get(FakeResponse(206, chunks=[b"x" * 10], url=entry.url))
        run_async(self.engine._segmented_download(entry, asyncio.Event()))
        self.assertEqual(
            sorted(session.ranges()),
            ["bytes=0-9", "bytes=10-19", "bytes=20-29", "bytes=30-39"],
        )

    def test_an_oversized_existing_file_is_not_truncated_away(self):
        """A pre-existing longer file is left alone; `truncate` only ever grows."""
        entry = self._entry(total=40, segments=4)
        Path(entry.file_path).write_bytes(b"z" * 64)
        session = self.use_session()
        for _ in range(4):
            session.queue_get(FakeResponse(206, chunks=[b"x" * 10], url=entry.url))
        run_async(self.engine._segmented_download(entry, asyncio.Event()))
        self.assertEqual(Path(entry.file_path).stat().st_size, 64)

    def test_completed_segments_are_never_refetched(self):
        entry = self._entry(total=40, segments=4)
        segs = HTTPEngine._create_segments("d1", 40, 4)
        segs[0].downloaded_bytes = 10
        segs[0].status = "completed"
        self.db.add_segments(segs)

        session = self.use_session()
        for _ in range(3):
            session.queue_get(FakeResponse(206, chunks=[b"y" * 10], url=entry.url))
        run_async(self.engine._segmented_download(entry, asyncio.Event()))

        self.assertEqual(len(self.db.get_segments("d1")), 4, "no duplicate segments")
        self.assertNotIn("bytes=0-9", session.ranges(), "a completed segment is not refetched")

    def test_all_segments_complete_short_circuits_without_any_request(self):
        entry = self._entry(total=40, segments=4)
        segs = HTTPEngine._create_segments("d1", 40, 4)
        for seg in segs:
            seg.status = "completed"
        self.db.add_segments(segs)
        session = self.use_session()
        run_async(self.engine._segmented_download(entry, asyncio.Event()))
        self.assertEqual(session.pending, 0, "nothing is left to fetch")

    def test_all_complete_leaves_stale_live_segments_known_limitation(self):
        """Documents a real defect rather than blessing it.

        ``_segmented_download`` registers ``_active_segments[download_id]`` and only pops
        it from the ``finally`` of the ``asyncio.gather`` block. The "all segments done"
        early return happens *before* that block, so a fully-completed segmented download
        leaves its segments in ``get_live_segments()`` for the lifetime of the process -
        the details panel would show a finished download as still segmented. Fixing it
        means popping on the early-return path too.
        """
        entry = self._entry(total=40, segments=4)
        segs = HTTPEngine._create_segments("d1", 40, 4)
        for seg in segs:
            seg.status = "completed"
        self.db.add_segments(segs)
        run_async(self.engine._segmented_download(entry, asyncio.Event()))
        self.assertIsNotNone(
            self.engine.get_live_segments("d1"),
            "KNOWN LIMITATION: the all-complete early return skips the `finally` that "
            "clears _active_segments, so live segments leak for the process lifetime",
        )

    def test_live_segments_are_published_while_running_and_released_after(self):
        entry = self._entry(total=40, segments=4)
        session = self.use_session()
        observed: list[int] = []

        def _on_chunk(chunk):
            observed.append(len(self.engine.get_live_segments("d1") or []))

        for _ in range(4):
            session.queue_get(
                FakeResponse(206, chunks=[b"z" * 10], url=entry.url, on_chunk=_on_chunk)
            )
        run_async(self.engine._segmented_download(entry, asyncio.Event()))
        self.assertTrue(observed, "the body must actually stream")
        self.assertTrue(all(n == 4 for n in observed), observed)
        self.assertIsNone(self.engine.get_live_segments("d1"), "live segments must be released")

    def test_fallback_propagates_and_releases_live_segments(self):
        entry = self._entry(total=40, segments=4)
        session = self.use_session()
        for _ in range(4):
            session.queue_get(FakeResponse(200, chunks=[b"q"], url=entry.url))
        with self.assertRaises(_FallbackToSingle):
            run_async(self.engine._segmented_download(entry, asyncio.Event()))
        self.assertIsNone(self.engine.get_live_segments("d1"))

    def test_per_download_segment_count_is_respected(self):
        entry = self._entry(total=100, segments=3)
        session = self.use_session()
        for _ in range(3):
            session.queue_get(FakeResponse(206, chunks=[b"w" * 33], url=entry.url))
        run_async(self.engine._segmented_download(entry, asyncio.Event()))
        self.assertEqual(len(self.db.get_segments("d1")), 3)

    def test_default_segment_count_constant(self):
        self.assertEqual(DEFAULT_SEGMENTS, 8)
        self.assertEqual(self.add_entry().num_segments, 8)

    def test_target_directory_is_created_on_demand(self):
        entry = self.add_entry(
            total_size=40,
            num_segments=2,
            file_path=(self.tmp / "deep" / "nested" / "a.zip").as_posix(),
        )
        session = self.use_session()
        for _ in range(2):
            session.queue_get(FakeResponse(206, chunks=[b"n" * 20], url=entry.url))
        run_async(self.engine._segmented_download(entry, asyncio.Event()))
        self.assertTrue(Path(entry.file_path).parent.is_dir())

    def test_a_failing_segment_tears_down_its_siblings(self):
        """One unrecoverable segment must not leave the others running."""
        entry = self._entry(total=40, segments=4)
        entry.max_retries = 1
        self.db.update_download(entry)
        session = self.use_session(gets=[FakeResponse(500)] * 4)
        with self.assertRaises(Exception):
            run_async(self.engine._segmented_download(entry, asyncio.Event()))
        self.assertIsNone(self.engine.get_live_segments("d1"))


class TestDownloadOneSegment(EngineTestCase):
    """One segment's transfer loop: resume, status transitions, failure handling."""

    def _entry_and_seg(self, downloaded=0, status="pending", end_byte=9, index=0):
        entry = self.add_entry(total_size=10, num_segments=1)
        # `_segmented_download` pre-allocates the target file before any segment runs, and
        # `_download_one_segment` writes with "r+b". Mirror that here, otherwise every
        # test would just be measuring FileNotFoundError.
        Path(entry.file_path).write_bytes(b"\x00" * 10)
        seg = SegmentEntry(
            id="seg0", download_id="d1", index=index, start_byte=0,
            end_byte=end_byte, downloaded_bytes=downloaded, status=status,
        )
        self.db.add_segments([seg])
        return entry, seg

    def test_writes_the_chunk_at_the_segment_offset(self):
        entry, seg = self._entry_and_seg()
        self.use_session(gets=[FakeResponse(206, chunks=[b"0123456789"], url=entry.url)])
        self.run_segment(entry, seg)
        self.assertEqual(Path(entry.file_path).read_bytes(), b"0123456789")
        self.assertEqual(self.db.get_segments("d1")[0].status, "completed")

    def test_resume_requests_from_the_byte_after_what_is_on_disk(self):
        entry, seg = self._entry_and_seg(downloaded=4)
        session = self.use_session(gets=[FakeResponse(206, chunks=[b"456789"], url=entry.url)])
        self.run_segment(entry, seg)
        self.assertEqual(session.ranges(), ["bytes=4-9"])

    def test_resume_writes_at_the_offset_not_at_the_start_of_the_file(self):
        entry, seg = self._entry_and_seg(downloaded=4)
        Path(entry.file_path).write_bytes(b"XXXX" + b"\x00" * 6)
        self.use_session(gets=[FakeResponse(206, chunks=[b"456789"], url=entry.url)])
        self.run_segment(entry, seg)
        self.assertEqual(Path(entry.file_path).read_bytes(), b"XXXX456789")

    def test_a_segment_past_its_end_is_completed_without_a_request(self):
        entry, seg = self._entry_and_seg(downloaded=10)
        session = self.use_session()
        self.run_segment(entry, seg)
        self.assertEqual(session.pending, 0, "a fully-received segment needs no fetch")
        self.assertEqual(seg.status, "completed")

    def test_pre_set_cancel_reports_the_segment_as_paused(self):
        entry, seg = self._entry_and_seg(status="downloading")
        evt = asyncio.Event()

        async def scenario():
            evt.set()
            await self.engine._download_one_segment(
                entry, seg, {0: 0}, time.monotonic(), 0, evt
            )

        run_async(scenario())
        self.assertEqual(seg.status, "paused")
        self.assertEqual(self.db.get_segments("d1")[0].status, "paused")

    def test_cancel_mid_transfer_keeps_the_partial_progress(self):
        entry, seg = self._entry_and_seg()
        evt = asyncio.Event()
        self.use_session(gets=[
            FakeResponse(206, chunks=[b"01234", b"56789"],
                         on_chunk=canceller_after(2, evt))
        ])

        async def scenario():
            await self.engine._download_one_segment(
                entry, seg, {0: 0}, time.monotonic(), 0, evt
            )

        run_async(scenario())
        self.assertEqual(seg.status, "paused")
        self.assertEqual(seg.downloaded_bytes, 5, "bytes received before the cancel are kept")
        self.assertEqual(self.db.get_segments("d1")[0].downloaded_bytes, 5)
        self.assertEqual(Path(entry.file_path).read_bytes(), b"01234" + b"\x00" * 5)

    def test_416_triggers_fallback_to_single(self):
        entry, seg = self._entry_and_seg()
        self.use_session(gets=[FakeResponse(416)])
        with self.assertRaises(_FallbackToSingle):
            self.run_segment(entry, seg)

    def test_403_without_curl_falls_back_to_single(self):
        entry, seg = self._entry_and_seg()
        self.use_session(gets=[FakeResponse(403)])
        with patch.object(http_engine_module, "_HAS_CURL_CFFI", False):
            with self.assertRaises(_FallbackToSingle):
                self.run_segment(entry, seg)

    def test_403_switches_to_curl_impersonation_and_succeeds(self):
        """Cloudflare blocking a range GET must silently retry through curl_cffi."""
        entry, seg = self._entry_and_seg()
        self.use_session(gets=[FakeResponse(403)])
        factory = curl_session_factory([FakeCurlResponse(206, chunks=[b"0123456789"])])
        with patch.object(http_engine_module, "CurlAsyncSession", factory):
            self.run_segment(entry, seg)
        self.assertTrue(entry.metadata.get("use_curl_cffi"), "the flag must stick for next time")
        self.assertEqual(len(factory.created), 1)
        self.assertEqual(seg.status, "completed")
        self.assertEqual(Path(entry.file_path).read_bytes(), b"0123456789")

    def test_curl_416_falls_back_to_single(self):
        entry, seg = self._entry_and_seg()
        entry.metadata["use_curl_cffi"] = True
        self.db.update_download(entry)
        self.use_session()
        factory = curl_session_factory([FakeCurlResponse(416)])
        with patch.object(http_engine_module, "CurlAsyncSession", factory):
            with self.assertRaises(_FallbackToSingle):
                self.run_segment(entry, seg)

    def test_curl_200_for_a_later_segment_falls_back(self):
        entry = self.add_entry(total_size=20, num_segments=2)
        Path(entry.file_path).write_bytes(b"\x00" * 20)
        seg = SegmentEntry(id="seg1", download_id="d1", index=1, start_byte=10, end_byte=19)
        self.db.add_segments([seg])
        entry.metadata["use_curl_cffi"] = True
        self.db.update_download(entry)
        self.use_session()
        factory = curl_session_factory([FakeCurlResponse(200, chunks=[b"x"])])
        with patch.object(http_engine_module, "CurlAsyncSession", factory):
            with self.assertRaises(_FallbackToSingle):
                self.run_segment(entry, seg)

    def test_curl_unexpected_status_bypasses_the_retry_ladder_known_limitation(self):
        """Documents a real defect rather than blessing it.

        The aiohttp segment path raises ``aiohttp.ClientError`` for an unexpected status,
        which the ladder catches. ``_download_segment_curl`` raises a *plain* ``Exception``,
        which the ``except (aiohttp.ClientError, asyncio.TimeoutError, OSError)`` clause
        does not catch - so a curl transfer that returns 500 is not retried at all and the
        segment is abandoned still marked "downloading" in the details panel. Raising an
        ``aiohttp.ClientError`` there (or widening the catch) would fix it.
        """
        entry, seg = self._entry_and_seg()
        entry.max_retries = 5
        self.db.update_download(entry)
        entry.metadata["use_curl_cffi"] = True
        self.db.update_download(entry)
        self.use_session()
        factory = curl_session_factory([FakeCurlResponse(500), FakeCurlResponse(500)])
        with patch.object(http_engine_module, "CurlAsyncSession", factory):
            with self.assertRaises(Exception):
                self.run_segment(entry, seg)
        self.assertEqual(
            len(factory.created), 1,
            "KNOWN LIMITATION: a curl segment failure is not retried, because it raises a "
            "plain Exception the ladder's except clause does not catch",
        )
        self.assertEqual(
            seg.status, "downloading",
            "KNOWN LIMITATION: the abandoned curl segment is never flipped to 'error'",
        )

    def test_curl_cancel_mid_transfer_reports_paused(self):
        entry, seg = self._entry_and_seg()
        entry.metadata["use_curl_cffi"] = True
        self.db.update_download(entry)
        self.use_session()
        evt = asyncio.Event()
        factory = curl_session_factory([
            FakeCurlResponse(206, chunks=[b"01234", b"56789"],
                             on_chunk=canceller_after(2, evt))
        ])

        async def scenario():
            await self.engine._download_one_segment(
                entry, seg, {0: 0}, time.monotonic(), 0, evt
            )

        with patch.object(http_engine_module, "CurlAsyncSession", factory):
            run_async(scenario())
        self.assertEqual(seg.status, "paused")
        self.assertEqual(seg.downloaded_bytes, 5)

    def test_unexpected_status_is_retried_then_reported_as_error(self):
        entry, seg = self._entry_and_seg()
        entry.max_retries = 3
        self.db.update_download(entry)
        session = self.use_session(gets=[FakeResponse(500)] * 3)
        with self.assertRaises(Exception) as ctx:
            self.run_segment(entry, seg)
        self.assertIn("failed after 3 retries", str(ctx.exception))
        self.assertEqual(seg.status, "error")
        self.assertEqual(self.db.get_segments("d1")[0].status, "error")
        self.assertEqual(len(session.get_calls), 3, "exactly max_retries attempts")
        self.assertTrue(self.sleeps, "the backoff ladder must have been walked")

    def test_transient_failure_then_success(self):
        entry, seg = self._entry_and_seg()
        entry.max_retries = 3
        self.db.update_download(entry)
        self.use_session(gets=[
            FakeResponse(raise_on_enter=aiohttp.ClientError("reset")),
            FakeResponse(206, chunks=[b"0123456789"]),
        ])
        self.run_segment(entry, seg)
        self.assertEqual(seg.status, "completed")
        self.assertEqual(Path(entry.file_path).read_bytes(), b"0123456789")

    def test_a_retrying_segment_does_not_keep_claiming_downloading(self):
        """A DB-only status update would leave the details panel showing stale state."""
        entry, seg = self._entry_and_seg()
        entry.max_retries = 2
        self.db.update_download(entry)
        seen: list[str] = []
        original = self.db.update_segment

        def _spy(segment_id, downloaded, status):
            seen.append(status)
            return original(segment_id, downloaded, status)

        self.db.update_segment = _spy
        self.addCleanup(setattr, self.db, "update_segment", original)
        self.use_session(gets=[
            FakeResponse(raise_on_enter=aiohttp.ClientError("reset")),
            FakeResponse(206, chunks=[b"0123456789"]),
        ])
        self.run_segment(entry, seg)
        self.assertIn("pending", seen, "a retrying segment must be reset to pending")
        self.assertEqual(seen[-1], "completed")

    def test_a_failure_after_cancel_does_not_retry(self):
        """A dead connection on a cancelled segment must not hammer the server.

        The cancel is tripped from inside the rate-limiter sleep, i.e. *after* the chunk
        has been written. That is the only ordering in which the body raises a transport
        error while the flag is already set, which is exactly the branch under test.
        """
        entry, seg = self._entry_and_seg()
        entry.max_retries = 5
        self.db.update_download(entry)
        self.engine.set_download_limit(100)  # force a throttling sleep mid-chunk
        evt = asyncio.Event()
        real_sleep = asyncio.sleep

        async def _cancel_while_throttled(delay, *args, **kwargs):
            if not evt.is_set():
                evt.set()
            await real_sleep(0)

        patcher = patch("asyncio.sleep", new=_cancel_while_throttled)
        patcher.start()
        self.addCleanup(patcher.stop)

        session = self.use_session(gets=[
            FakeResponse(206, chunks=[b"01234"],
                         raise_after_chunks=aiohttp.ClientError("reset")),
        ])

        async def scenario():
            await self.engine._download_one_segment(
                entry, seg, {0: 0}, time.monotonic(), 0, evt
            )

        run_async(scenario())
        self.assertTrue(evt.is_set(), "precondition: the cancel flag was tripped")
        self.assertEqual(len(session.get_calls), 1, "a cancelled segment must not be retried")
        self.assertEqual(seg.downloaded_bytes, 5, "the bytes that did arrive are kept")

    def test_rate_limit_throttles_between_chunks(self):
        entry, seg = self._entry_and_seg()
        self.engine.set_download_limit(100)  # 100 B/s with the default "max" allocation
        self.use_session(gets=[FakeResponse(206, chunks=[b"0123456789"])])
        self.run_segment(entry, seg)
        self.assertTrue(
            any(delay > 0 for delay in self.sleeps),
            f"a throttled transfer must sleep between chunks, sleeps={self.sleeps}",
        )

    def test_no_throttle_when_the_limit_is_unlimited(self):
        entry, seg = self._entry_and_seg()
        self.engine.set_download_limit(0)
        self.use_session(gets=[FakeResponse(206, chunks=[b"0123456789"])])
        self.run_segment(entry, seg)
        self.assertEqual(self.sleeps, [], "an unlimited transfer must not sleep")

    def test_aggregate_progress_is_persisted_on_completion(self):
        entry, seg = self._entry_and_seg()
        self.use_session(gets=[FakeResponse(206, chunks=[b"0123456789"])])
        self.run_segment(entry, seg)
        self.assertEqual(self.entry().downloaded_size, 10)

    def test_progress_callback_reports_the_running_total(self):
        entry, seg = self._entry_and_seg()
        self.use_session(gets=[FakeResponse(206, chunks=[b"01234", b"56789"])])
        self.run_segment(entry, seg)
        ticks = [p for p in self.progress if p[0] == "d1"]
        self.assertTrue(ticks, "progress must be reported")
        self.assertEqual((ticks[-1][1], ticks[-1][2]), (10, 10))

    def test_chunk_size_is_the_documented_64kib(self):
        self.assertEqual(CHUNK_SIZE, 64 * 1024)


class TestSingleDownload(EngineTestCase):
    """Single-stream transfer: resume, restart, range parsing, cancellation."""

    def _entry(self, filename="a.zip", **kw):
        return self.add_entry(filename=filename, total_size=kw.pop("total_size", 10), **kw)

    def test_a_fresh_download_writes_the_whole_body(self):
        entry = self._entry()
        session = self.use_session(gets=[
            FakeResponse(200, headers={"Content-Length": "10"},
                         chunks=[b"01234", b"56789"], url=entry.url)
        ])
        run_async(self.engine._single_download(entry, asyncio.Event()))
        self.assertEqual(Path(entry.file_path).read_bytes(), b"0123456789")
        self.assertEqual(self.entry().downloaded_size, 10)
        self.assertIsNone(session.ranges()[0], "a fresh download must not send a Range header")

    def test_resume_sends_a_range_header_for_the_existing_prefix(self):
        entry = self._entry()
        Path(entry.file_path).write_bytes(b"01234")
        session = self.use_session(gets=[
            FakeResponse(206, headers={"Content-Range": "bytes 5-9/10"},
                         chunks=[b"56789"], url=entry.url)
        ])
        run_async(self.engine._single_download(entry, asyncio.Event()))
        self.assertEqual(session.ranges(), ["bytes=5-"])
        self.assertEqual(Path(entry.file_path).read_bytes(), b"0123456789")
        self.assertEqual(self.entry().total_size, 10, "the Content-Range total must be adopted")

    def test_a_server_ignoring_range_restarts_from_zero(self):
        """A 200 to a Range request is the whole body: it must overwrite, not append."""
        entry = self._entry()
        Path(entry.file_path).write_bytes(b"stale")
        self.use_session(gets=[FakeResponse(200, chunks=[b"0123456789"], url=entry.url)])
        run_async(self.engine._single_download(entry, asyncio.Event()))
        self.assertEqual(Path(entry.file_path).read_bytes(), b"0123456789")

    def test_416_on_a_complete_file_finishes_without_refetching(self):
        entry = self._entry()
        Path(entry.file_path).write_bytes(b"0123456789")
        session = self.use_session(gets=[FakeResponse(416)])
        run_async(self.engine._single_download(entry, asyncio.Event()))
        self.assertEqual(entry.downloaded_size, 10)
        self.assertEqual(len(session.get_calls), 1, "it must not retry blindly")

    def test_416_on_a_stale_range_still_overwrites_the_file(self):
        """A 416 with an unknown total cannot mean "done", so the retry must re-fetch.

        KNOWN LIMITATION: the code pops the stale ``Range`` header on the 416 path, but the
        top of the retry loop rebuilds ``headers`` from the on-disk size, so the same stale
        ``Range`` is re-sent. The download still ends up correct (the 200 branch forces
        ``mode="wb"``), but the wasted request is sent once per attempt. Tracked so the
        dead ``headers.pop("Range", None)`` line is impossible to miss.
        """
        entry = self._entry(total_size=0)
        Path(entry.file_path).write_bytes(b"01234")
        session = self.use_session(gets=[
            FakeResponse(416),
            FakeResponse(200, chunks=[b"0123456789"], url=entry.url),
        ])
        run_async(self.engine._single_download(entry, asyncio.Event()))
        self.assertEqual(
            session.ranges(), ["bytes=5-", "bytes=5-"],
            "KNOWN LIMITATION: the 416 branch pops the Range header, but the retry loop "
            "rebuilds it from the unchanged on-disk size and re-sends the stale range",
        )
        self.assertEqual(Path(entry.file_path).read_bytes(), b"0123456789")

    def test_a_server_that_always_416_exhausts_its_retries(self):
        """Repeated 416s are not a "already complete" verdict, so they must not spin."""
        entry = self._entry(total_size=0)
        Path(entry.file_path).write_bytes(b"01234")
        entry.max_retries = 3
        self.db.update_download(entry)
        session = self.use_session(gets=[FakeResponse(416)] * 3)
        with self.assertRaises(Exception) as ctx:
            run_async(self.engine._single_download(entry, asyncio.Event()))
        self.assertIn("failed after 3 retries", str(ctx.exception))
        self.assertEqual(len(session.get_calls), 3)

    def test_unexpected_status_is_retried_then_raises(self):
        entry = self._entry()
        entry.max_retries = 2
        self.db.update_download(entry)
        session = self.use_session(gets=[FakeResponse(500)] * 2)
        with self.assertRaises(Exception) as ctx:
            run_async(self.engine._single_download(entry, asyncio.Event()))
        self.assertIn("failed after 2 retries", str(ctx.exception))
        self.assertEqual(len(session.get_calls), 2)

    def test_a_body_that_dies_mid_stream_resumes_from_the_new_file_size(self):
        entry = self._entry(total_size=0)
        entry.max_retries = 3
        self.db.update_download(entry)
        session = self.use_session(gets=[
            FakeResponse(200, chunks=[b"01234"],
                         raise_after_chunks=aiohttp.ClientError("dropped")),
            FakeResponse(206, headers={"Content-Range": "bytes 5-9/10"},
                         chunks=[b"56789"], url=entry.url),
        ])
        run_async(self.engine._single_download(entry, asyncio.Event()))
        self.assertEqual(session.ranges()[1], "bytes=5-", "the retry must resume, not restart")
        self.assertEqual(Path(entry.file_path).read_bytes(), b"0123456789")

    def test_cancel_mid_transfer_persists_what_was_written(self):
        entry = self._entry()
        evt = asyncio.Event()
        self.use_session(gets=[
            FakeResponse(200, chunks=[b"01234", b"56789"],
                         on_chunk=canceller_after(2, evt))
        ])

        async def scenario():
            await self.engine._single_download(entry, evt)

        run_async(scenario())
        self.assertEqual(self.entry().downloaded_size, 5)
        self.assertEqual(Path(entry.file_path).read_bytes(), b"01234")

    def test_a_pre_set_cancel_never_opens_a_connection(self):
        entry = self._entry()
        evt = asyncio.Event()
        session = self.use_session(gets=[FakeResponse(200, chunks=[b"x"])])

        async def scenario():
            evt.set()
            await self.engine._single_download(entry, evt)

        run_async(scenario())
        self.assertEqual(session.pending, 1, "the scripted response must be untouched")

    def test_a_content_disposition_filename_is_deduplicated_against_the_queue(self):
        entry = self._entry(filename="")
        self.add_entry(entry_id="d2", filename="server.bin")
        self.use_session(gets=[
            FakeResponse(200, headers={"Content-Disposition": 'attachment; filename="server.bin"'},
                         chunks=[b"x"], url=entry.url)
        ])
        run_async(self.engine._single_download(entry, asyncio.Event()))
        self.assertEqual(entry.filename, "server (1).bin", "must not collide with the queued row")
        self.assertEqual(self.filenames[-1], ("d1", "server (1).bin"))

    def test_an_explicit_filename_is_never_overwritten_by_the_server(self):
        entry = self._entry(filename="picked-by-user.bin")
        entry.metadata["explicit_filename"] = True
        self.db.update_download(entry)
        self.use_session(gets=[
            FakeResponse(200, headers={"Content-Disposition": 'attachment; filename="other.bin"'},
                         chunks=[b"x"], url=entry.url)
        ])
        run_async(self.engine._single_download(entry, asyncio.Event()))
        self.assertEqual(entry.filename, "picked-by-user.bin")
        self.assertEqual(self.filenames, [])

    def test_curl_single_download_writes_the_body(self):
        entry = self._entry()
        entry.metadata["use_curl_cffi"] = True
        self.db.update_download(entry)
        self.use_session()
        factory = curl_session_factory([
            FakeCurlResponse(200, headers={"content-length": "10"}, chunks=[b"0123456789"])
        ])
        with patch.object(http_engine_module, "CurlAsyncSession", factory):
            run_async(self.engine._single_download(entry, asyncio.Event()))
        self.assertEqual(Path(entry.file_path).read_bytes(), b"0123456789")

    def test_curl_single_download_bad_status_raises(self):
        entry = self._entry()
        entry.max_retries = 1
        entry.metadata["use_curl_cffi"] = True
        self.db.update_download(entry)
        self.use_session()
        factory = curl_session_factory([FakeCurlResponse(404)])
        with patch.object(http_engine_module, "CurlAsyncSession", factory):
            with self.assertRaises(Exception):
                run_async(self.engine._single_download(entry, asyncio.Event()))

    def test_curl_single_download_cancel_persists_progress(self):
        entry = self._entry()
        entry.metadata["use_curl_cffi"] = True
        self.db.update_download(entry)
        self.use_session()
        evt = asyncio.Event()
        factory = curl_session_factory([
            FakeCurlResponse(200, chunks=[b"01234", b"56789"],
                             on_chunk=canceller_after(2, evt))
        ])

        async def scenario():
            await self.engine._single_download(entry, evt)

        with patch.object(http_engine_module, "CurlAsyncSession", factory):
            run_async(scenario())
        self.assertEqual(self.entry().downloaded_size, 5)

    def test_curl_single_download_206_adopts_the_content_range_total(self):
        entry = self._entry()
        Path(entry.file_path).write_bytes(b"01234")
        entry.metadata["use_curl_cffi"] = True
        self.db.update_download(entry)
        self.use_session()
        factory = curl_session_factory([
            FakeCurlResponse(206, headers={"content-range": "bytes 5-9/10"}, chunks=[b"56789"])
        ])
        with patch.object(http_engine_module, "CurlAsyncSession", factory):
            run_async(self.engine._single_download(entry, asyncio.Event()))
        self.assertEqual(entry.total_size, 10)
        self.assertEqual(Path(entry.file_path).read_bytes(), b"0123456789")

    def test_curl_single_download_403_switches_on_and_succeeds(self):
        entry = self._entry()
        self.use_session(gets=[FakeResponse(403)])
        factory = curl_session_factory([FakeCurlResponse(200, chunks=[b"0123456789"])])
        with patch.object(http_engine_module, "CurlAsyncSession", factory):
            run_async(self.engine._single_download(entry, asyncio.Event()))
        self.assertEqual(Path(entry.file_path).read_bytes(), b"0123456789")
        self.assertTrue(entry.metadata.get("use_curl_cffi"))

    def test_curl_single_download_renames_a_colliding_file(self):
        entry = self._entry(filename="")
        entry.metadata["use_curl_cffi"] = True
        self.db.update_download(entry)
        self.add_entry(entry_id="d2", filename="server.bin")
        self.use_session()
        factory = curl_session_factory([
            FakeCurlResponse(200, headers={"Content-Disposition": 'attachment; filename="server.bin"'},
                             chunks=[b"x"])
        ])
        with patch.object(http_engine_module, "CurlAsyncSession", factory):
            run_async(self.engine._single_download(entry, asyncio.Event()))
        self.assertEqual(entry.filename, "server (1).bin")

    def test_curl_single_download_respects_an_explicit_filename(self):
        entry = self._entry(filename="mine.bin")
        entry.metadata["use_curl_cffi"] = True
        entry.metadata["explicit_filename"] = True
        self.db.update_download(entry)
        self.use_session()
        factory = curl_session_factory([
            FakeCurlResponse(200, headers={"Content-Disposition": 'attachment; filename="theirs.bin"'},
                             chunks=[b"x"])
        ])
        with patch.object(http_engine_module, "CurlAsyncSession", factory):
            run_async(self.engine._single_download(entry, asyncio.Event()))
        self.assertEqual(entry.filename, "mine.bin")

    def test_curl_single_download_carries_the_derived_range_header(self):
        entry = self._entry()
        Path(entry.file_path).write_bytes(b"01234")
        entry.metadata["use_curl_cffi"] = True
        self.db.update_download(entry)
        self.use_session()
        factory = curl_session_factory([FakeCurlResponse(200, chunks=[b"56789"])])
        with patch.object(http_engine_module, "CurlAsyncSession", factory):
            run_async(self.engine._single_download(entry, asyncio.Event()))
        self.assertEqual(factory.created[0].get_calls[0]["headers"].get("Range"), "bytes=5-")

    def test_rate_limit_throttles_the_single_stream(self):
        entry = self._entry()
        self.engine.set_download_limit(100)
        self.use_session(gets=[FakeResponse(200, chunks=[b"0123456789"])])
        run_async(self.engine._single_download(entry, asyncio.Event()))
        self.assertTrue(self.sleeps, "a throttled transfer must sleep")

    def test_no_throttle_when_the_limit_is_unlimited(self):
        entry = self._entry()
        self.use_session(gets=[FakeResponse(200, chunks=[b"0123456789"])])
        run_async(self.engine._single_download(entry, asyncio.Event()))
        self.assertEqual(self.sleeps, [])


class TestRunDownload(EngineTestCase):
    """The orchestrator: probe, choose a strategy, and finalise the row."""

    def test_a_small_file_goes_single_stream(self):
        entry = self.add_entry()
        session = self.use_session(
            heads=[FakeResponse(200, headers={"Content-Length": "10", "Accept-Ranges": "bytes"})],
            gets=[FakeResponse(200, chunks=[b"0123456789"], url=entry.url)],
        )
        run_async(self.engine._run_download(entry, asyncio.Event()))
        self.assertEqual(self.entry().status, "completed")
        self.assertEqual(Path(entry.file_path).read_bytes(), b"0123456789")
        self.assertEqual(self.db.get_segments("d1"), [], "a tiny file must not be segmented")
        self.assertEqual(len(session.get_calls), 1)

    def test_a_large_file_uses_segments(self):
        entry = self.add_entry(num_segments=2)
        session = self.use_session(
            heads=[FakeResponse(200, headers={
                "Content-Length": str(1024 * 1024), "Accept-Ranges": "bytes",
            })],
        )
        for _ in range(2):
            session.queue_get(FakeResponse(206, chunks=[b"x" * (1024 * 512)], url=entry.url))
        run_async(self.engine._run_download(entry, asyncio.Event()))
        self.assertEqual(self.entry().status, "completed")
        self.assertEqual(len(self.db.get_segments("d1")), 2)
        self.assertEqual(Path(entry.file_path).stat().st_size, 1024 * 1024)

    def test_a_server_ignoring_range_falls_back_and_drops_the_segments(self):
        """200 to every range GET must degrade to one stream, not fail the download."""
        entry = self.add_entry(num_segments=2)
        session = self.use_session(
            heads=[FakeResponse(200, headers={
                "Content-Length": str(1024 * 1024), "Accept-Ranges": "bytes",
            })],
            gets=[
                FakeResponse(200, chunks=[b"a" * 100], url=entry.url),
                FakeResponse(200, chunks=[b"a" * 100], url=entry.url),
                FakeResponse(200, chunks=[b"a" * (1024 * 1024)], url=entry.url),
            ],
        )
        run_async(self.engine._run_download(entry, asyncio.Event()))
        self.assertEqual(self.db.get_segments("d1"), [], "abandoned segments must be dropped")
        self.assertEqual(self.entry().status, "completed")
        self.assertEqual(Path(entry.file_path).stat().st_size, 1024 * 1024)
        self.assertEqual(self.entry().downloaded_size, 1024 * 1024)

    def test_the_probe_total_size_is_persisted(self):
        entry = self.add_entry(total_size=0)
        self.use_session(
            heads=[FakeResponse(200, headers={"Content-Length": "10"})],
            gets=[FakeResponse(200, chunks=[b"0123456789"], url=entry.url)],
        )
        run_async(self.engine._run_download(entry, asyncio.Event()))
        self.assertEqual(self.entry().total_size, 10)

    def test_a_known_etag_is_never_overwritten_by_the_probe(self):
        entry = self.add_entry(total_size=10, etag='"kept"')
        self.use_session(
            heads=[FakeResponse(200, headers={"Content-Length": "10", "ETag": '"new"'})],
            gets=[FakeResponse(200, chunks=[b"0123456789"], url=entry.url)],
        )
        run_async(self.engine._run_download(entry, asyncio.Event()))
        self.assertEqual(self.entry().etag, '"kept"')

    def test_an_etag_is_adopted_when_absent(self):
        entry = self.add_entry(total_size=10)
        self.use_session(
            heads=[FakeResponse(200, headers={"Content-Length": "10", "ETag": '"fresh"'})],
            gets=[FakeResponse(200, chunks=[b"0123456789"], url=entry.url)],
        )
        run_async(self.engine._run_download(entry, asyncio.Event()))
        self.assertEqual(self.entry().etag, '"fresh"')

    def test_an_explicit_filename_survives_the_probe(self):
        entry = self.add_entry(filename="chosen.bin", total_size=10)
        entry.metadata["explicit_filename"] = True
        self.db.update_download(entry)
        self.use_session(
            heads=[FakeResponse(200, headers={
                "Content-Length": "10",
                "Content-Disposition": 'attachment; filename="header.bin"',
            })],
            gets=[FakeResponse(200, chunks=[b"0123456789"], url=entry.url)],
        )
        run_async(self.engine._run_download(entry, asyncio.Event()))
        self.assertEqual(self.entry().filename, "chosen.bin")

    def test_a_probe_filename_is_auto_numbered_against_the_queue(self):
        entry = self.add_entry(filename="")
        self.add_entry(entry_id="d2", filename="report.pdf")
        disposition = 'attachment; filename="report.pdf"'
        self.use_session(
            heads=[FakeResponse(200, headers={
                "Content-Length": "10", "Content-Disposition": disposition,
            })],
            gets=[FakeResponse(200, headers={"Content-Disposition": disposition},
                               chunks=[b"0123456789"], url=entry.url)],
        )
        run_async(self.engine._run_download(entry, asyncio.Event()))
        self.assertEqual(self.entry().filename, "report (1).pdf")

    def test_the_probe_filename_survives_the_body_probe(self):
        """The body probe must not rename the file back over a de-duplicated name.

        ``_run_download`` de-duplicates the Content-Disposition name, then
        ``_single_download`` re-reads the same header and re-runs the de-duplication.
        That has to be idempotent, otherwise a colliding download would keep flipping
        between ``report.pdf`` and ``report (1).pdf``.
        """
        entry = self.add_entry(filename="")
        self.add_entry(entry_id="d2", filename="report.pdf")
        disposition = 'attachment; filename="report.pdf"'
        session = self.use_session(
            heads=[FakeResponse(200, headers={
                "Content-Length": "10", "Content-Disposition": disposition,
            })],
            gets=[FakeResponse(200, headers={"Content-Disposition": disposition},
                               chunks=[b"0123456789"], url=entry.url)],
        )
        run_async(self.engine._run_download(entry, asyncio.Event()))
        emitted = [name for _, name in self.filenames]
        self.assertEqual(emitted, ["report (1).pdf"], "the rename must be announced once")
        self.assertEqual(len(session.get_calls), 1)

    def test_resuming_keeps_the_existing_file_name(self):
        entry = self.add_entry(filename="a.zip", total_size=10, downloaded_size=4)
        Path(entry.file_path).write_bytes(b"0123")
        self.use_session(
            heads=[FakeResponse(200, headers={"Content-Length": "10"})],
            gets=[FakeResponse(206, headers={"Content-Range": "bytes 4-9/10"},
                               chunks=[b"456789"], url=entry.url)],
        )
        run_async(self.engine._run_download(entry, asyncio.Event()))
        self.assertEqual(self.entry().filename, "a.zip", "resume must not re-number the file")
        self.assertEqual(Path(entry.file_path).read_bytes(), b"0123456789")

    def test_completion_stamps_progress_and_emits_the_terminal_status(self):
        entry = self.add_entry(total_size=10)
        self.use_session(
            heads=[FakeResponse(200, headers={"Content-Length": "10"})],
            gets=[FakeResponse(200, chunks=[b"0123456789"], url=entry.url)],
        )
        run_async(self.engine._run_download(entry, asyncio.Event()))
        self.assertEqual([s[1] for s in self.status_trail()], ["downloading", "completed"])
        row = self.entry()
        self.assertEqual(row.downloaded_size, 10)
        self.assertTrue(row.completed_at, "completion must stamp completed_at")
        self.assertEqual(row.error_message, "")

    def test_a_short_body_is_back_filled_to_the_advertised_total(self):
        """A truncated body still finishes: the row is what the user sees."""
        entry = self.add_entry(total_size=0)
        self.use_session(
            heads=[FakeResponse(200, headers={"Content-Length": "10"})],
            gets=[FakeResponse(200, chunks=[b"01234"], url=entry.url)],
        )
        run_async(self.engine._run_download(entry, asyncio.Event()))
        row = self.entry()
        self.assertEqual(row.status, "completed")
        self.assertEqual((row.total_size, row.downloaded_size), (10, 10))

    def test_a_zero_length_body_is_reported_completed_known_quirk(self):
        """Documents behaviour rather than blessing it.

        The completion block sets ``status = "completed"`` for any uncancelled run, with
        no check that anything was actually received. A 200 with an empty body therefore
        lands as a 0-byte "completed" download with ``total_size`` still 0. A download
        that transferred nothing should arguably stay queued or go to error.
        """
        entry = self.add_entry(total_size=0)
        self.use_session(
            heads=[FakeResponse(200, headers={"Content-Length": "0"})],
            gets=[FakeResponse(200, chunks=[])],
        )
        run_async(self.engine._run_download(entry, asyncio.Event()))
        row = self.entry()
        self.assertEqual(row.status, "completed")
        self.assertEqual(row.downloaded_size, 0)
        self.assertEqual(row.total_size, 0)

    def test_a_cancelled_run_leaves_the_row_untouched(self):
        entry = self.add_entry(total_size=10)
        evt = asyncio.Event()

        async def scenario():
            evt.set()
            await self.engine._run_download(entry, evt)

        run_async(scenario())
        self.assertNotIn("completed", [s[1] for s in self.status_trail()])
        self.assertNotIn("d1", self.engine._tasks)
        self.assertNotIn("d1", self.engine._cancel_events)

    def test_a_fatal_error_schedules_a_retry(self):
        entry = self.add_entry(total_size=10, max_retries=3)
        self.use_session(
            heads=[FakeResponse(200, headers={"Content-Length": "10"})],
            gets=[FakeResponse(raise_on_enter=OSError("no route to host"))] * 3,
        )
        run_async(self.engine._run_download(entry, asyncio.Event()))
        row = self.entry()
        self.assertEqual(row.status, "queued")
        self.assertGreater(row.metadata.get("next_retry_at", 0), 0)
        self.assertEqual([s[1] for s in self.status_trail()][-1], "queued")
        self.assertIn(
            "Retrying", self.status_trail()[-1][2],
            "the retry notice reaches the GUI even though the row keeps an empty "
            "error_message (see TestHandleRetry.test_the_retry_notice_is_never_persisted)",
        )
        # See TestHandleRetry.test_the_retry_counter_is_reset_by_the_stale_in_memory_entry
        # for why the counter itself is still 0 here.
        self.assertEqual(row.retry_count, 0)

    def test_exhausted_retries_end_in_error(self):
        entry = self.add_entry(total_size=10, max_retries=1)
        self.use_session(
            heads=[FakeResponse(200, headers={"Content-Length": "10"})],
            gets=[FakeResponse(raise_on_enter=OSError("no route to host"))],
        )
        run_async(self.engine._run_download(entry, asyncio.Event()))
        row = self.entry()
        self.assertEqual(row.status, "error")
        self.assertIn("failed after 1 retries", row.error_message)
        self.assertIn("error", [s[1] for s in self.status_trail()])

    def test_the_transport_error_is_lost_from_the_retry_message_known_limitation(self):
        """Documents a real defect rather than blessing it.

        ``_single_download`` swallows each ``aiohttp.ClientError`` / ``OSError`` and, once
        the ladder is exhausted, raises a generic
        ``"Single-stream download failed after N retries"``. The original message
        ("no route to host", a TLS error, ...) never reaches the row, so the user is shown
        a retry count instead of a diagnosable cause. Chaining the last exception into the
        terminal raise would fix it.
        """
        entry = self.add_entry(total_size=10, max_retries=1)
        self.use_session(
            heads=[FakeResponse(200, headers={"Content-Length": "10"})],
            gets=[FakeResponse(raise_on_enter=OSError("no route to host"))],
        )
        run_async(self.engine._run_download(entry, asyncio.Event()))
        self.assertNotIn(
            "no route to host", self.entry().error_message,
            "KNOWN LIMITATION: the terminal raise discards the transport error, so the "
            "row's error_message carries only a retry count",
        )

    def test_an_error_after_the_row_disappeared_does_not_crash(self):
        """A download deleted mid-flight must not raise from the failure handler."""
        entry = self.add_entry(total_size=10, max_retries=3)

        def _delete_then_fail(chunk):
            self.db.delete_download("d1")

        self.use_session(
            heads=[FakeResponse(200, headers={"Content-Length": "10"})],
            gets=[FakeResponse(200, chunks=[b"01234"], on_chunk=_delete_then_fail,
                               raise_after_chunks=OSError("gone"))],
        )
        run_async(self.engine._run_download(entry, asyncio.Event()))
        self.assertIsNone(self.db.get_download("d1"), "the row must stay deleted")

    def test_the_kill_switch_blocks_the_transfer_before_any_request(self):
        entry = self.add_entry(total_size=10)
        self.engine.set_network_config_sync(
            NetworkConfig(interface_name="VPN", interface_ip="10.9.9.9", kill_switch=True)
        )
        session = self.use_session()
        with patch.object(http_engine_module, "is_interface_active", return_value=False):
            run_async(self.engine._run_download(entry, asyncio.Event()))
        self.assertEqual(session.pending, 0, "the kill switch must stop before the probe")
        row = self.entry()
        self.assertEqual(row.status, "error")
        self.assertIn("Kill switch", row.error_message)
        self.assertIn("error", [s[1] for s in self.status_trail()])

    def test_the_kill_switch_allows_a_live_interface(self):
        entry = self.add_entry(total_size=10)
        self.engine.set_network_config_sync(
            NetworkConfig(interface_name="VPN", interface_ip="10.9.9.9", kill_switch=True)
        )
        self.use_session(
            heads=[FakeResponse(200, headers={"Content-Length": "10"})],
            gets=[FakeResponse(200, chunks=[b"0123456789"], url=entry.url)],
        )
        with patch.object(http_engine_module, "is_interface_active", return_value=True):
            run_async(self.engine._run_download(entry, asyncio.Event()))
        self.assertEqual(self.entry().status, "completed")

    def test_the_kill_switch_is_inert_without_a_bound_interface(self):
        entry = self.add_entry(total_size=10)
        self.engine.set_network_config_sync(NetworkConfig(kill_switch=True))
        self.use_session(
            heads=[FakeResponse(200, headers={"Content-Length": "10"})],
            gets=[FakeResponse(200, chunks=[b"0123456789"], url=entry.url)],
        )
        with patch.object(http_engine_module, "is_interface_active") as probe:
            run_async(self.engine._run_download(entry, asyncio.Event()))
        probe.assert_not_called()
        self.assertEqual(self.entry().status, "completed")

    def test_tasks_and_cancel_events_are_cleaned_up(self):
        entry = self.add_entry(total_size=10)
        self.use_session(
            heads=[FakeResponse(200, headers={"Content-Length": "10"})],
            gets=[FakeResponse(200, chunks=[b"0123456789"], url=entry.url)],
        )
        run_async(self.engine._run_download(entry, asyncio.Event()))
        self.assertNotIn("d1", self.engine._tasks)
        self.assertNotIn("d1", self.engine._cancel_events)

    def test_cancellation_is_swallowed_not_raised(self):
        entry = self.add_entry(total_size=10)
        self.use_session(heads=[FakeResponse(200, headers={"Content-Length": "10"})])

        async def scenario():
            task = asyncio.ensure_future(self.engine._run_download(entry, asyncio.Event()))
            await asyncio.sleep(0)
            task.cancel()
            await task  # must not raise

        run_async(scenario())


class TestHandleRetry(EngineTestCase):
    """The retry bookkeeping the manager reads back to schedule the next attempt."""

    def test_the_first_failure_is_queued_with_a_backoff_timestamp(self):
        entry = self.add_entry(max_retries=3)
        self.engine._handle_retry(entry, "boom")
        row = self.entry()
        self.assertEqual(row.status, "queued")
        self.assertGreater(row.metadata.get("next_retry_at", 0), 0, "the backoff must be recorded")
        self.assertEqual(row.metadata.get("retry_delay"), 2.0, "the default backoff base is 2s")
        self.assertEqual([s[1] for s in self.status_trail()], ["queued"])

    def test_the_retry_notice_reaches_the_status_callback(self):
        entry = self.add_entry(max_retries=3)
        self.engine._handle_retry(entry, "boom")
        _, status, message = self.status_trail()[-1]
        self.assertEqual(status, "queued")
        self.assertIn("Retrying in", message)
        self.assertIn("1/3", message)
        self.assertIn("boom", message)

    def test_a_sub_second_backoff_omits_the_seconds_phrase(self):
        entry = self.add_entry(max_retries=3)
        self.engine.set_general_config_sync(
            GeneralConfig(retry_delay=0.01, retry_exponential_backoff=False)
        )
        self.engine._handle_retry(entry, "boom")
        message = self.status_trail()[-1][2]
        self.assertIn("Retrying (", message)
        self.assertNotIn("Retrying in", message)

    def test_the_retry_notice_is_never_persisted_known_bug(self):
        """Documents a real defect rather than blessing it.

        ``_handle_retry`` builds a user-facing "Retrying in 2s (1/3): <cause>" message and
        hands it to ``Database.update_status(did, "queued", msg)``, which only writes
        ``error_message`` for the ``completed`` / ``downloading`` / ``error`` statuses - the
        ``else`` branch updates the status alone. The message therefore reaches the live
        GUI through the status callback but is never stored: if the app is closed during
        the backoff window, the retry reason is lost and the row looks like a plain queued
        download.
        """
        entry = self.add_entry(max_retries=3)
        self.engine._handle_retry(entry, "boom")
        self.assertEqual(
            self.entry().error_message, "",
            "KNOWN BUG: update_status() ignores error_message for the 'queued' status, so "
            "the retry reason is dropped instead of being persisted on the row",
        )

    def test_the_retry_counter_is_reset_by_the_stale_in_memory_entry_known_bug(self):
        """Documents a real defect rather than blessing it.

        ``_handle_retry`` bumps the counter with ``Database.increment_retry()`` and then
        calls ``update_download(entry)`` to persist the backoff metadata - but ``entry`` is
        the object the engine was handed, whose ``retry_count`` is whatever it was before
        the increment. That write puts the stale value back, so the counter is *always*
        back to 0 in the database.

        Consequences, both real:

        * ``count < entry.max_retries`` is true forever, so the "exhausted retries" branch
          is unreachable whenever ``max_retries > 1`` and a permanently failing download
          re-queues indefinitely instead of landing in ``error``;
        * ``DownloadManager._process_queue`` gates on ``e.retry_count >= e.max_retries``
          (manager.py:1755), so that safety net never trips either.

        Fix: either re-read the row after incrementing and use that object's
        ``retry_count``, or carry the new count onto ``entry`` before ``update_download``.
        """
        entry = self.add_entry(max_retries=3)
        self.engine._handle_retry(entry, "boom")
        self.assertEqual(
            self.entry().retry_count, 0,
            "KNOWN BUG: _handle_retry writes the stale in-memory retry_count back over the "
            "incremented one, so the counter never advances and the download retries forever",
        )
        self.assertEqual(
            self.entry().status, "queued",
            "and because the counter never advances, the exhausted-retries branch is "
            "unreachable, so the download can never reach the error state",
        )

    def test_repeated_failures_never_reach_the_error_state(self):
        entry = self.add_entry(max_retries=3)
        for _ in range(10):
            self.engine._handle_retry(entry, "boom")
        self.assertEqual(
            self.entry().status, "queued",
            "KNOWN BUG: ten failures against max_retries=3 still leave the row queued, "
            "because the reset retry_count makes the exhaustion check unreachable",
        )

    def test_max_retries_of_one_still_reaches_the_error_state(self):
        """The bug is masked at ``max_retries == 1``, which is why it survived."""
        entry = self.add_entry(max_retries=1)
        self.engine._handle_retry(entry, "boom")
        self.assertEqual(self.entry().status, "error")
        self.assertEqual(self.entry().retry_count, 1)

    def test_exhausted_retries_move_to_error(self):
        entry = self.add_entry(max_retries=1)
        self.engine._handle_retry(entry, "boom")
        row = self.entry()
        self.assertEqual(row.status, "error")
        self.assertEqual(row.error_message, "boom")
        self.assertNotIn("next_retry_at", row.metadata)

    def test_paused_rows_are_not_retried(self):
        entry = self.add_entry(max_retries=3)
        self.db.update_status("d1", "paused")
        self.engine._handle_retry(entry, "boom")
        row = self.entry()
        self.assertEqual(row.retry_count, 0)
        self.assertEqual(row.status, "paused")

    def test_completed_rows_are_not_retried(self):
        entry = self.add_entry(max_retries=3)
        self.db.update_status("d1", "completed")
        self.engine._handle_retry(entry, "boom")
        self.assertEqual(self.entry().retry_count, 0)
        self.assertEqual(self.entry().status, "completed")

    def test_the_queued_transition_is_emitted(self):
        entry = self.add_entry(max_retries=3)
        self.engine._handle_retry(entry, "boom")
        self.assertEqual([s[1] for s in self.status_trail()], ["queued"])

    def test_the_error_transition_is_emitted(self):
        entry = self.add_entry(max_retries=1)
        self.engine._handle_retry(entry, "boom")
        self.assertEqual([s[1] for s in self.status_trail()], ["error"])


class TestEmitProgress(EngineTestCase):
    """Progress emission: throttling, cancellation, and paused-row zeroing."""

    def _entry(self, status="downloading"):
        return self.add_entry(total_size=1000, downloaded_size=0, status=status)

    def test_no_callback_means_no_op(self):
        self.add_entry()
        self.engine._progress_cb = None
        self.engine._emit_progress("d1", 1, 1000, 1.0, 1.0)  # must not raise

    def test_the_first_emit_always_lands(self):
        self._entry()
        self.engine._emit_progress("d1", 1, 1000, 1.0, 1.0)
        self.assertEqual(len(self.progress), 1)

    def test_rapid_successive_emits_are_throttled(self):
        self._entry()
        self.engine._emit_progress("d1", 1, 1000, 1.0, 1.0)
        # Seed the throttle clock as "just emitted", then fire four more inside the
        # 100 ms window.
        self.engine._last_progress_emit["d1"] = time.monotonic()
        for i in range(2, 6):
            self.engine._emit_progress("d1", i, 1000, 1.0, 1.0)
        self.assertEqual(len(self.progress), 1, "5 emits inside 100 ms must collapse to 1")

    def test_completion_is_never_throttled(self):
        """A finished download must always be reported, however fast it arrived."""
        self._entry()
        self.engine._last_progress_emit["d1"] = 10_000.0
        self.engine._emit_progress("d1", 1000, 1000, 0.0, 0.0)
        self.assertEqual(len(self.progress), 1)

    def test_a_cancelled_download_emits_nothing(self):
        self._entry()
        evt = asyncio.Event()

        async def scenario():
            self.engine._cancel_events["d1"] = evt
            evt.set()
            self.engine._emit_progress("d1", 1, 1000, 1.0, 1.0)

        run_async(scenario())
        self.assertEqual(self.progress, [])

    def test_a_paused_row_reports_zero_speed_and_eta(self):
        self._entry(status="paused")
        self.engine._emit_progress("d1", 500, 1000, 999.0, 42.0)
        _, _, _, speed, eta = self.progress[0]
        self.assertEqual((speed, eta), (0.0, 0.0))

    def test_an_unknown_download_still_emits(self):
        self.engine._emit_progress("ghost", 1, 1000, 1.0, 1.0)
        self.assertEqual(len(self.progress), 1)


class TestSessionLifecycle(EngineTestCase):
    """Session recreation, ``stop()``, and seamless resume across a routing change."""

    def test_stop_requeues_every_active_download(self):
        self.add_entry(total_size=10, status="downloading")
        session = self.use_session()

        async def scenario():
            async def quick():
                return None

            self.engine._tasks["d1"] = asyncio.ensure_future(quick())
            self.engine._cancel_events["d1"] = asyncio.Event()
            await self.engine.stop()

        run_async(scenario())
        self.assertEqual(
            self.entry().status, "queued", "stop() must requeue so it resumes next launch"
        )
        self.assertEqual(session.close_calls, 1)
        self.assertIsNone(self.engine._session)

    def test_stop_signals_the_cooperative_cancel_event(self):
        self.add_entry(total_size=10, status="downloading")
        self.use_session()

        async def scenario():
            async def quick():
                return None

            evt = asyncio.Event()
            self.engine._tasks["d1"] = asyncio.ensure_future(quick())
            self.engine._cancel_events["d1"] = evt
            await self.engine.stop()
            self.assertTrue(evt.is_set())

        run_async(scenario())

    def test_stop_swallows_a_task_that_raises_cancellation(self):
        """A task that dies on cancellation must not abort engine shutdown."""
        self.add_entry(total_size=10, status="downloading")
        self.use_session()
        observed: list[str] = []

        async def scenario():
            async def stubborn():
                observed.append("started")
                raise asyncio.CancelledError

            self.engine._tasks["d1"] = asyncio.ensure_future(stubborn())
            self.engine._cancel_events["d1"] = asyncio.Event()
            await self.engine.stop()

        run_async(scenario())
        self.assertEqual(observed, ["started"])
        self.assertEqual(self.entry().status, "queued")

    def test_stop_without_any_task_still_closes_the_session(self):
        self.use_session()
        run_async(self.engine.stop())
        self.assertIsNone(self.engine._session)

    def test_stop_is_idempotent(self):
        self.use_session()
        run_async(self.engine.stop())
        run_async(self.engine.stop())  # must not raise on a None session

    def test_recreate_session_closes_the_previous_one(self):
        old = self.use_session()
        new = FakeSession()
        with patch.object(http_engine_module.aiohttp, "ClientSession", lambda **kw: new):
            run_async(self.engine._recreate_session())
        self.assertEqual(old.close_calls, 1)
        self.assertIs(self.engine._session, new)

    def test_recreate_session_applies_the_user_agent(self):
        captured: dict = {}

        def _make(**kwargs):
            captured.update(kwargs)
            return FakeSession()

        with patch.object(http_engine_module.aiohttp, "ClientSession", _make):
            run_async(self.engine._recreate_session())
        self.assertIn("User-Agent", captured["headers"])
        self.assertEqual(
            captured["timeout"].connect, http_engine_module.CONNECT_TIMEOUT
        )
        self.assertEqual(captured["timeout"].sock_read, http_engine_module.READ_TIMEOUT)

    def test_recreate_session_closes_the_tor_session_too(self):
        self.use_session()

        class _Sess:
            def __init__(self):
                self.closed = False
                self.close_calls = 0

            async def close(self):
                self.close_calls += 1
                self.closed = True

        tor = _Sess()

        async def scenario():
            self.engine._tor_session = tor
            await self.engine._recreate_session()

        with patch.object(http_engine_module.aiohttp, "ClientSession", lambda **kw: FakeSession()):
            run_async(scenario())
        self.assertEqual(tor.close_calls, 1)

    def test_start_creates_a_session(self):
        with patch.object(http_engine_module.aiohttp, "ClientSession", lambda **kw: FakeSession()):
            run_async(self.engine.start())
        self.assertIsNotNone(self.engine._session)

    def test_set_tor_config_recreates_the_session_when_routing_changes(self):
        self.use_session()
        with patch.object(http_engine_module.aiohttp, "ClientSession", lambda **kw: FakeSession()):
            run_async(self.engine.set_tor_config(TorConfig(enabled=True, route_http=True)))
        self.assertTrue(self.engine.tor_config.enabled)
        self.assertIsNot(self.engine._session, None)

    def test_set_tor_config_is_a_no_op_when_routing_is_unchanged(self):
        session = self.use_session()
        self.engine.set_tor_config_sync(TorConfig(enabled=True, route_http=True))
        with patch.object(self.engine, "_recreate_session", new=AsyncMock()) as recreate:
            run_async(self.engine.set_tor_config(TorConfig(enabled=True, route_http=True)))
        recreate.assert_not_awaited()
        self.assertIs(self.engine._session, session)

    def test_set_network_config_recreates_the_session(self):
        self.use_session()
        with patch.object(self.engine, "_recreate_session", new=AsyncMock()) as recreate:
            run_async(self.engine.set_network_config(NetworkConfig(download_limit=99)))
        recreate.assert_awaited_once()
        self.assertEqual(self.engine.network_config.download_limit, 99)

    def test_safe_recreate_resumes_an_active_download(self):
        """A routing change must not lose an in-flight download: cancel, then restart."""
        self.add_entry(total_size=10, status="downloading")
        restarted: list[str] = []

        async def fake_add(entry):
            restarted.append(entry.id)

        async def scenario():
            async def never():
                await asyncio.Event().wait()

            self.engine._tasks["d1"] = asyncio.ensure_future(never())
            self.engine._cancel_events["d1"] = asyncio.Event()
            with patch.object(self.engine, "add", fake_add):
                with patch.object(self.engine, "_recreate_session", new=_async_noop):
                    await self.engine._safe_recreate_session()

        run_async(scenario())
        self.assertEqual(restarted, ["d1"])
        self.assertEqual(self.engine._tasks, {}, "stale tasks must be cleared")
        self.assertEqual(self.engine._cancel_events, {})

    def test_safe_recreate_signals_the_cooperative_cancel_event(self):
        self.add_entry(total_size=10, status="downloading")
        evt = asyncio.Event()

        async def scenario():
            async def never():
                await asyncio.Event().wait()

            self.engine._tasks["d1"] = asyncio.ensure_future(never())
            self.engine._cancel_events["d1"] = evt
            with patch.object(self.engine, "_recreate_session", new=_async_noop):
                await self.engine._safe_recreate_session()

        run_async(scenario())
        self.assertTrue(evt.is_set(), "the cooperative cancel flag must be set before cancelling")

    def test_safe_recreate_does_not_resume_a_paused_download(self):
        self.add_entry(total_size=10, status="paused")
        restarted: list[str] = []

        async def scenario():
            self.engine._tasks["d1"] = asyncio.ensure_future(asyncio.Event().wait())
            with patch.object(self.engine, "add", _recorder(restarted)):
                with patch.object(self.engine, "_recreate_session", new=_async_noop):
                    await self.engine._safe_recreate_session()

        run_async(scenario())
        self.assertEqual(restarted, [])

    def test_safe_recreate_does_not_resume_a_completed_download(self):
        self.add_entry(total_size=10, status="completed")
        restarted: list[str] = []

        async def scenario():
            self.engine._tasks["d1"] = asyncio.ensure_future(asyncio.Event().wait())
            with patch.object(self.engine, "add", _recorder(restarted)):
                with patch.object(self.engine, "_recreate_session", new=_async_noop):
                    await self.engine._safe_recreate_session()

        run_async(scenario())
        self.assertEqual(restarted, [])


def _recorder(sink: list):
    async def _add(entry):
        sink.append(entry.id)

    return _add


if __name__ == "__main__":
    unittest.main()
