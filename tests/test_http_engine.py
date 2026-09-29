"""Unit tests for HTTPEngine internals.

Focus: pure/DB-only logic that is easy to verify without a network, plus the
lifecycle (add / pause / cancel / is_active) that the rest of the app depends
on. Everything here is offline: no aiohttp session is ever created.
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication

from my_idm.config import GeneralConfig
from my_idm.database import Database, DownloadEntry
from my_idm.network import NetworkConfig
from my_idm.http_engine import (
    DEFAULT_USER_AGENT,
    MAX_RETRIES_PER_SEGMENT,
    HTTPEngine,
)

app = QApplication.instance() or QApplication(sys.argv)


class TestCreateSegments(unittest.TestCase):
    """Segment arithmetic: every byte must be covered exactly once."""

    @staticmethod
    def _segments(total, n):
        return HTTPEngine._create_segments("d1", total, n)

    def test_even_split_covers_every_byte(self):
        segs = self._segments(1000, 4)
        self.assertEqual(len(segs), 4)
        self.assertEqual(segs[0].start_byte, 0)
        self.assertEqual(segs[0].end_byte, 249)
        self.assertEqual(segs[-1].end_byte, 999)
        for prev, nxt in zip(segs, segs[1:]):
            self.assertEqual(nxt.start_byte, prev.end_byte + 1, "segments must be contiguous")

    def test_last_segment_absorbs_the_remainder(self):
        # 1003 // 4 = 250, so 3 bytes are left over for the final segment.
        segs = self._segments(1003, 4)
        self.assertEqual(len(segs), 4)
        self.assertEqual(segs[-1].end_byte, 1002)
        for prev, nxt in zip(segs, segs[1:]):
            self.assertEqual(nxt.start_byte, prev.end_byte + 1)

    def test_single_segment_spans_whole_file(self):
        segs = self._segments(500, 1)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].start_byte, 0)
        self.assertEqual(segs[0].end_byte, 499)

    def test_remainder_larger_than_one_segment(self):
        # 10 bytes over 3 segments: seg_size == 3, so the last takes 7 bytes.
        segs = self._segments(10, 3)
        self.assertEqual([s.start_byte for s in segs], [0, 3, 6])
        self.assertEqual([s.end_byte for s in segs], [2, 5, 9])

    def test_indices_and_ids(self):
        segs = self._segments(100, 2)
        self.assertEqual([s.index for s in segs], [0, 1])
        self.assertTrue(all(s.download_id == "d1" for s in segs))
        self.assertEqual(len({s.id for s in segs}), 2, "segment ids must be unique")

    def test_more_segments_than_bytes(self):
        # Degenerate but must not raise or produce negative ranges.
        segs = self._segments(2, 4)
        self.assertEqual(len(segs), 4)
        self.assertEqual(segs[-1].end_byte, 1)


class TestBuildHeaders(unittest.TestCase):
    """Header assembly, including the Referer auto-injection rule."""

    def setUp(self):
        self.engine = HTTPEngine.__new__(HTTPEngine)
        self.engine._network_config = None
        self.engine._tor_config = None

    def test_default_user_agent_always_present(self):
        headers = self.engine._build_headers()
        self.assertEqual(headers["User-Agent"], DEFAULT_USER_AGENT)

    def test_metadata_headers_are_merged(self):
        entry = DownloadEntry(id="d1", url="https://example.com/a.zip")
        entry.metadata = {"headers": {"Cookie": "abc=1"}}
        headers = self.engine._build_headers(entry=entry, url=entry.url)
        self.assertEqual(headers["Cookie"], "abc=1")
        self.assertEqual(headers["User-Agent"], DEFAULT_USER_AGENT)

    def test_explicit_referer_wins(self):
        entry = DownloadEntry(id="d1", url="https://example.com/a.zip")
        entry.metadata = {"referer": "https://ref.example/"}
        headers = self.engine._build_headers(entry=entry, url=entry.url)
        self.assertEqual(headers["Referer"], "https://ref.example/")

    def test_referer_autoinjected_for_owocdn(self):
        entry = DownloadEntry(id="d1", url="https://cdn.owocdn.top/a.mp4")
        headers = self.engine._build_headers(entry=entry, url=entry.url)
        self.assertIn("Referer", headers)

    def test_referer_autoinjected_for_kwik(self):
        entry = DownloadEntry(id="d1", url="https://kwik.si/a.mkv")
        headers = self.engine._build_headers(entry=entry, url=entry.url)
        self.assertIn("Referer", headers)

    def test_no_referer_for_ordinary_hosts(self):
        entry = DownloadEntry(id="d1", url="https://example.com/a.zip")
        headers = self.engine._build_headers(entry=entry, url=entry.url)
        self.assertNotIn("Referer", headers)

    def test_malformed_metadata_is_ignored(self):
        entry = DownloadEntry(id="d1", url="https://example.com/a.zip")
        entry.metadata = {"headers": "not-a-dict", "referer": None}
        headers = self.engine._build_headers(entry=entry, url=entry.url)
        self.assertEqual(headers["User-Agent"], DEFAULT_USER_AGENT)


class TestRetryPolicy(unittest.TestCase):
    """Retry counts and backoff."""

    def setUp(self):
        self.engine = HTTPEngine.__new__(HTTPEngine)
        self.engine._general_config = None

    def test_max_retries_falls_back_to_module_default(self):
        entry = DownloadEntry(id="d1", url="https://example.com/a.zip")
        self.assertEqual(self.engine._get_max_retries(entry), MAX_RETRIES_PER_SEGMENT)
        self.assertEqual(self.engine._get_max_retries(None), MAX_RETRIES_PER_SEGMENT)

    def test_entry_override_wins(self):
        self.engine._general_config = GeneralConfig(max_retries=9)
        entry = DownloadEntry(id="d1", url="https://example.com/a.zip", max_retries=2)
        self.assertEqual(self.engine._get_max_retries(entry), 2)

    def test_global_config_used_when_entry_has_no_override(self):
        self.engine._general_config = GeneralConfig(max_retries=7)
        entry = DownloadEntry(id="d1", url="https://example.com/a.zip", max_retries=0)
        self.assertEqual(self.engine._get_max_retries(entry), 7)

    def test_retry_delay_is_exponential_and_capped(self):
        delays = [self.engine._get_retry_delay(i) for i in range(10)]
        self.assertEqual(delays, sorted(delays), "delay must not decrease")
        self.assertLessEqual(max(delays), 60.0, "delay must stay capped")
        self.assertTrue(all(d >= 0 for d in delays))

    def test_retry_delay_delegates_to_config(self):
        cfg = MagicMock()
        cfg.get_retry_delay.return_value = 4.2
        self.engine._general_config = cfg
        self.assertEqual(self.engine._get_retry_delay(3), 4.2)
        cfg.get_retry_delay.assert_called_once_with(3)


class TestFilenameFromUrl(unittest.TestCase):
    """Deriving a filename from a URL when the server does not supply one."""

    @staticmethod
    def _name(url):
        return HTTPEngine._filename_from_url(url)

    def test_plain_url(self):
        self.assertEqual(self._name("https://example.com/files/report.pdf"), "report.pdf")

    def test_query_string_stripped(self):
        self.assertEqual(
            self._name("https://example.com/a/b.zip?token=abc&x=1"), "b.zip"
        )

    def test_trailing_slash_falls_back(self):
        self.assertEqual(self._name("https://example.com/"), "download")

    def test_no_extension_in_path_uses_query_parameter(self):
        self.assertEqual(
            self._name("https://example.com/download?file=real.pdf"), "real.pdf"
        )
        self.assertEqual(
            self._name("https://example.com/download?filename=other.zip"), "other.zip"
        )

    def test_query_parameter_without_extension_is_ignored(self):
        self.assertEqual(self._name("https://example.com/x?name=plain"), "x")

    def test_empty_url_falls_back(self):
        self.assertEqual(self._name(""), "download")

    def test_never_returns_a_path_separator(self):
        for url in (
            "https://example.com/a/b/c.bin",
            "https://example.com/a%20b/c.bin",
            "https://example.com/x?file=a%2Fb%2Fc.bin",
        ):
            name = self._name(url)
            self.assertNotIn("/", name)
            self.assertNotIn("\\", name)


class TestRequestKwargsProxy(unittest.TestCase):
    """The proxy short-circuits that route traffic through Tor."""

    def _engine(self, network, tor):
        engine = HTTPEngine.__new__(HTTPEngine)
        engine._network_config = network
        engine._tor_config = tor
        return engine

    def test_general_proxy_applied_when_tor_off(self):
        from my_idm.config import TorConfig

        net = NetworkConfig(proxy_enabled=True, proxy_host="p", proxy_port=8080)
        engine = self._engine(net, TorConfig(enabled=False))
        entry = DownloadEntry(id="d1", url="https://example.com/a.zip")
        self.assertIn("proxy", engine._request_kwargs({}, entry=entry, url=entry.url))

    def test_general_proxy_suppressed_when_tor_routes_http(self):
        from my_idm.config import TorConfig

        net = NetworkConfig(proxy_enabled=True, proxy_host="p", proxy_port=8080)
        engine = self._engine(net, TorConfig(enabled=True, route_http=True))
        entry = DownloadEntry(id="d1", url="https://example.com/a.zip")
        self.assertNotIn("proxy", engine._request_kwargs({}, entry=entry, url=entry.url))

    def test_proxy_absent_when_disabled(self):
        engine = self._engine(NetworkConfig(proxy_enabled=False), None)
        entry = DownloadEntry(id="d1", url="https://example.com/a.zip")
        self.assertNotIn("proxy", engine._request_kwargs({}, entry=entry, url=entry.url))


class TestEngineLifecycle(unittest.TestCase):
    """add / pause / cancel / is_active and the cancel-event lifecycle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(":memory:")
        self.db.open()
        self.engine = HTTPEngine(self.db)
        self.engine._general_config = GeneralConfig()
        self.engine._network_config = None
        self.engine._tor_config = None
        self.engine._session = None
        self.engine._tor_session = None
        self.db.add_download(
            DownloadEntry(
                id="d1", url="https://example.com/a.zip", filename="a.zip",
                save_path=self.tmp.name, file_path=f"{self.tmp.name}/a.zip",
                total_size=1000, status="queued",
            )
        )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_is_active_false_for_unknown_and_finished(self):
        self.assertFalse(self.engine.is_active("nope"))
        self.assertFalse(self.engine.is_active("d1"))

    def test_is_active_true_while_task_pending(self):
        async def scenario():
            async def never():
                await asyncio.sleep(30)

            task = asyncio.ensure_future(never())
            self.engine._tasks["d1"] = task
            try:
                self.assertTrue(self.engine.is_active("d1"))
            finally:
                task.cancel()

        self.run_async(scenario())

    def test_pause_signals_cancel_event_and_is_idempotent(self):
        async def scenario():
            evt = asyncio.Event()
            self.engine._cancel_events["d1"] = evt
            await self.engine.pause("d1")
            self.assertTrue(evt.is_set())
            # Calling again, and for an unknown id, must not raise.
            await self.engine.pause("d1")
            await self.engine.pause("unknown-id")

        self.run_async(scenario())

    def test_cancel_sets_event_and_drops_task(self):
        async def scenario():
            evt = asyncio.Event()
            self.engine._cancel_events["d1"] = evt

            async def quick():
                await asyncio.sleep(3600)

            task = asyncio.ensure_future(quick())
            self.engine._tasks["d1"] = task
            await self.engine.cancel("d1")

            self.assertTrue(evt.is_set())
            self.assertNotIn("d1", self.engine._tasks)
            self.assertNotIn("d1", self.engine._cancel_events)

        self.run_async(scenario())

    def test_cancel_unknown_id_is_safe(self):
        async def scenario():
            await self.engine.cancel("does-not-exist")

        self.run_async(scenario())

    def test_pause_emits_final_progress_tick(self):
        """A pause emits a last tick: 0 downloaded, the known total retained."""
        async def scenario():
            seen = []
            self.engine.set_callbacks(
                progress_cb=lambda *a: seen.append(a),
                status_cb=lambda *a: None,
                filename_cb=lambda *a: None,
            )
            await self.engine.pause("d1")
            self.assertTrue(seen, "pause should emit a final progress tick")
            download_id, downloaded, total, speed, eta = seen[-1][:5]
            self.assertEqual(download_id, "d1")
            self.assertEqual(downloaded, 0)
            self.assertEqual(speed, 0.0)
            self.assertEqual(eta, 0.0)
            self.assertEqual(total, 1000, "the known total is retained")

        self.run_async(scenario())

    def test_add_ignores_duplicate_active_download(self):
        async def scenario():
            async def never():
                await asyncio.sleep(30)

            entry = self.db.get_download("d1")
            task = asyncio.ensure_future(never())
            self.engine._tasks["d1"] = task
            try:
                await self.engine.add(entry)
                # Still exactly one task: no duplicate was started.
                self.assertEqual(len(self.engine._tasks), 1)
            finally:
                task.cancel()

        self.run_async(scenario())

    @staticmethod
    def run_async(coro):
        return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
