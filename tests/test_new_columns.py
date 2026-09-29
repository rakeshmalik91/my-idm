"""Tests for the Last Seeded and Source table columns."""

import sys
import unittest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from my_idm.database import DownloadEntry
from my_idm.download_model import (
    ANIMEPAHE_SOURCE,
    YOUTUBE_SOURCE,
    Col,
    DownloadTableModel,
    browser_from_user_agent,
    resolve_download_source,
)

app = QApplication.instance() or QApplication(sys.argv)

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
FIREFOX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0"
EDGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
)


def make_entry(**meta) -> DownloadEntry:
    entry = DownloadEntry(
        id="x", url="https://example.com/file.zip", filename="file.zip",
        save_path="C:/tmp", file_path="C:/tmp/file.zip", status="completed",
    )
    if meta:
        entry.metadata = dict(meta)
    return entry


class TestBrowserFromUserAgent(unittest.TestCase):

    def test_detects_chrome(self):
        self.assertEqual(browser_from_user_agent(CHROME_UA), "Chrome")

    def test_detects_firefox(self):
        self.assertEqual(browser_from_user_agent(FIREFOX_UA), "Firefox")

    def test_detects_edge(self):
        self.assertEqual(browser_from_user_agent(EDGE_UA), "Edge")

    def test_edge_wins_over_chrome(self):
        """Edge UAs advertise Chrome too; the more specific token must win."""
        self.assertIn("Chrome/", EDGE_UA)
        self.assertEqual(browser_from_user_agent(EDGE_UA), "Edge")

    def test_empty_and_unknown(self):
        self.assertEqual(browser_from_user_agent(""), "")
        self.assertEqual(browser_from_user_agent("curl/8.0"), "")

    def test_is_case_insensitive(self):
        self.assertEqual(browser_from_user_agent("mozilla/5.0 firefox/121.0"), "Firefox")


class TestResolveDownloadSource(unittest.TestCase):

    def test_manual_is_blank(self):
        self.assertEqual(resolve_download_source(make_entry()), "")

    def test_empty_metadata_is_blank(self):
        self.assertEqual(resolve_download_source(DownloadEntry(id="x", url="u")), "")

    def test_none_entry_is_blank(self):
        self.assertEqual(resolve_download_source(None), "")

    def test_youtube_mode_a(self):
        entry = make_entry(source_type="youtube")
        self.assertEqual(resolve_download_source(entry), YOUTUBE_SOURCE)

    def test_youtube_mode_b(self):
        entry = make_entry(source_type="youtube_native")
        self.assertEqual(resolve_download_source(entry), YOUTUBE_SOURCE)

    def test_animepahe(self):
        self.assertEqual(
            resolve_download_source(make_entry(added_by="animepahe")), ANIMEPAHE_SOURCE
        )

    def test_browser_chrome(self):
        entry = make_entry(source="browser_extension", user_agent=CHROME_UA)
        self.assertEqual(resolve_download_source(entry), "Chrome")

    def test_browser_firefox(self):
        entry = make_entry(source="browser_extension", user_agent=FIREFOX_UA)
        self.assertEqual(resolve_download_source(entry), "Firefox")

    def test_browser_edge(self):
        entry = make_entry(source="browser_extension", user_agent=EDGE_UA)
        self.assertEqual(resolve_download_source(entry), "Edge")

    def test_browser_without_user_agent_is_blank(self):
        entry = make_entry(source="browser_extension")
        self.assertEqual(resolve_download_source(entry), "")

    def test_youtube_wins_over_browser(self):
        entry = make_entry(
            source="browser_extension", source_type="youtube", user_agent=CHROME_UA
        )
        self.assertEqual(resolve_download_source(entry), YOUTUBE_SOURCE)

    def test_only_known_source_values(self):
        """The column must only ever emit the documented set."""
        allowed = {"", "Chrome", "Firefox", "Edge", ANIMEPAHE_SOURCE, YOUTUBE_SOURCE}
        samples = [
            make_entry(),
            make_entry(source_type="youtube"),
            make_entry(source_type="youtube_native"),
            make_entry(added_by="animepahe"),
            make_entry(source="browser_extension", user_agent=CHROME_UA),
            make_entry(source="browser_extension", user_agent=FIREFOX_UA),
            make_entry(source="browser_extension", user_agent=EDGE_UA),
            make_entry(source="something_else", user_agent=CHROME_UA),
        ]
        for entry in samples:
            self.assertIn(resolve_download_source(entry), allowed)


class TestNewColumnDefinitions(unittest.TestCase):

    def test_appended_indices(self):
        self.assertEqual(Col.LAST_SEEDED, 14)
        self.assertEqual(Col.SOURCE, 15)
        self.assertEqual(Col.SEEDING_STARTED_AT, 16)
        self.assertEqual(Col.COUNT, 17)

    def test_headers(self):
        self.assertEqual(Col.HEADERS[Col.LAST_SEEDED], "Last Seeded")
        self.assertEqual(Col.HEADERS[Col.SOURCE], "Source")
        self.assertEqual(Col.HEADERS[Col.SEEDING_STARTED_AT], "Seeding Started At")

    def test_last_seeded_not_in_date_columns(self):
        """Kept out of DATE_COLUMNS so the existing date-sort contract is unchanged."""
        self.assertNotIn(Col.LAST_SEEDED, Col.DATE_COLUMNS)
        self.assertNotIn(Col.SOURCE, Col.DATE_COLUMNS)

    def test_headers_stay_in_sync_with_constants(self):
        self.assertEqual(len(Col.HEADERS), Col.COUNT)
        for name in (
            "QUEUE", "NAME", "SOURCE_DOMAIN", "SIZE", "PROGRESS", "STATUS", "SPEED",
            "ETA", "SEEDS_PEERS", "ADDED", "LAST_TRIED", "COMPLETED", "SAVE_PATH",
            "FILE_NAME", "LAST_SEEDED", "SOURCE",
        ):
            index = getattr(Col, name)
            self.assertIsInstance(Col.HEADERS[index], str, name)


class TestNewColumnRendering(unittest.TestCase):

    def setUp(self):
        self.model = DownloadTableModel()

    def _display(self, entry, col):
        self.model.load_entries([entry])
        return self.model.data(self.model.index(0, col), Qt.ItemDataRole.DisplayRole)

    def test_last_seeded_formatted(self):
        entry = make_entry()
        entry.last_seeded_at = "2026-03-04T05:06:07+00:00"
        value = self._display(entry, Col.LAST_SEEDED)
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}")

    def test_last_seeded_empty_shows_dash(self):
        self.assertEqual(self._display(make_entry(), Col.LAST_SEEDED), "—")

    def test_source_renders_label(self):
        entry = make_entry(source_type="youtube_native")
        self.assertEqual(self._display(entry, Col.SOURCE), "YouTube")

    def test_source_renders_browser(self):
        entry = make_entry(source="browser_extension", user_agent=EDGE_UA)
        self.assertEqual(self._display(entry, Col.SOURCE), "Edge")

    def test_source_renders_animepahe(self):
        entry = make_entry(added_by="animepahe")
        self.assertEqual(self._display(entry, Col.SOURCE), "AnimePahe")

    def test_manual_source_is_blank_string(self):
        self.assertEqual(self._display(make_entry(), Col.SOURCE), "")

    def test_header_data(self):
        self.model.load_entries([make_entry()])
        header_model = self.model.headerData
        self.assertEqual(
            header_model(Col.LAST_SEEDED, Qt.Orientation.Horizontal,
                         Qt.ItemDataRole.DisplayRole),
            "Last Seeded",
        )
        self.assertEqual(
            header_model(Col.SOURCE, Qt.Orientation.Horizontal,
                         Qt.ItemDataRole.DisplayRole),
            "Source",
        )


class TestNewColumnSorting(unittest.TestCase):

    def setUp(self):
        self.model = DownloadTableModel()

    def _keys(self, col, ascending):
        self.model.load_entries([])
        out = []
        for value in ("", "2026-01-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00"):
            entry = make_entry()
            entry.last_seeded_at = value
            out.append(self.model._entry_sort_key(entry, col, ascending))
        return out

    def test_last_seeded_sort_key_shape_matches_other_dates(self):
        """Must be a (int, str) tuple so it stays type-compatible with ADDED/COMPLETED."""
        for ascending in (True, False):
            for key in self._keys(Col.LAST_SEEDED, ascending):
                self.assertIsInstance(key, tuple)
                self.assertEqual(len(key), 2)
                self.assertIsInstance(key[0], int)
                self.assertIsInstance(key[1], str)

    def test_last_seeded_empty_sorts_last_ascending(self):
        keys = self._keys(Col.LAST_SEEDED, True)
        # Empty value gets flag 1, so it sorts after real timestamps.
        self.assertEqual(keys[0][0], 1)
        self.assertEqual(keys[1][0], 0)
        self.assertEqual(sorted(keys)[0][0], 0)

    def test_last_seeded_empty_sorts_last_descending(self):
        keys = self._keys(Col.LAST_SEEDED, False)
        self.assertEqual(keys[0][0], 0)
        self.assertEqual(keys[1][0], 1)

    def test_source_sort_key_is_string(self):
        entry = make_entry(source_type="youtube")
        key = self.model._entry_sort_key(entry, Col.SOURCE, True)
        self.assertIsInstance(key, str)
        self.assertEqual(key, "youtube")

    def test_source_sort_key_blank(self):
        key = self.model._entry_sort_key(make_entry(), Col.SOURCE, False)
        self.assertEqual(key, "")

    def test_sorting_table_by_new_columns_does_not_raise(self):
        a = make_entry()
        a.last_seeded_at = "2026-01-01T00:00:00+00:00"
        b = make_entry()
        b.last_seeded_at = ""
        b.metadata = {"source": "browser_extension", "user_agent": EDGE_UA}
        self.model.load_entries([a, b])
        self.model.sort(Col.LAST_SEEDED, Qt.SortOrder.AscendingOrder)
        self.model.sort(Col.SOURCE, Qt.SortOrder.DescendingOrder)


class TestTorrentLastSeeded(unittest.TestCase):
    """The torrent engine must translate libtorrent's epoch into an ISO stamp."""

    def test_get_status_reads_last_seen_complete(self):
        from my_idm import torrent_engine as te

        if not te.HAS_LIBTORRENT:
            self.skipTest("libtorrent not installed")

        engine = te.TorrentEngine.__new__(te.TorrentEngine)
        engine._handles = {"d1": object()}

        class FakeStatus:
            total_wanted = 1000
            total_wanted_done = 1000
            has_metadata = False
            total_done = 0
            state = 6
            progress = 1.0
            download_rate = 0
            upload_rate = 0
            num_seeds = 5
            num_peers = 2
            num_complete = 9
            list_seeds = 0
            num_incomplete = 3
            list_peers = 0
            all_time_upload = 500
            all_time_download = 1000
            last_seen_complete = 1767225600  # 2026-01-01T00:00:00Z

        class FakeHandle:
            def status(self):
                return FakeStatus()

        engine._handles["d1"] = FakeHandle()
        status = engine.get_status("d1")
        self.assertIsNotNone(status)
        self.assertEqual(status["last_seeded_epoch"], 1767225600)

    def test_get_status_tolerates_missing_attribute(self):
        from my_idm import torrent_engine as te

        if not te.HAS_LIBTORRENT:
            self.skipTest("libtorrent not installed")

        engine = te.TorrentEngine.__new__(te.TorrentEngine)

        class FakeStatus:
            total_wanted = 10
            total_wanted_done = 10
            has_metadata = False
            total_done = 0
            state = 6
            progress = 1.0
            download_rate = 0
            upload_rate = 0
            num_seeds = 0
            num_peers = 0
            num_complete = -1
            list_seeds = 0
            num_incomplete = -1
            list_peers = 0
            all_time_upload = 0
            all_time_download = 0
            # No last_seen_complete / last_seen attributes at all.

        class FakeHandle:
            def status(self):
                return FakeStatus()

        engine._handles = {"d1": FakeHandle()}
        status = engine.get_status("d1")
        self.assertIsNotNone(status)
        self.assertEqual(status["last_seeded_epoch"], 0)

    def test_status_dict_keeps_all_existing_keys(self):
        """The new key must be additive; existing consumers are unaffected."""
        from my_idm import torrent_engine as te

        if not te.HAS_LIBTORRENT:
            self.skipTest("libtorrent not installed")

        engine = te.TorrentEngine.__new__(te.TorrentEngine)

        class FakeStatus:
            total_wanted = 1
            total_wanted_done = 1
            has_metadata = False
            total_done = 0
            state = 6
            progress = 1.0
            download_rate = 0
            upload_rate = 0
            num_seeds = 0
            num_peers = 0
            num_complete = -1
            list_seeds = 0
            num_incomplete = -1
            list_peers = 0
            all_time_upload = 0
            all_time_download = 0
            last_seen_complete = 0

        class FakeHandle:
            def status(self):
                return FakeStatus()

        engine._handles = {"d1": FakeHandle()}
        status = engine.get_status("d1")
        for key in (
            "total_size", "downloaded", "progress", "state", "speed", "upload_speed",
            "seeds", "peers", "total_seeds", "total_peers", "eta", "name",
            "total_upload", "total_download",
        ):
            self.assertIn(key, status)


class TestLastSeededStamping(unittest.TestCase):
    """Regression: Last Seeded stayed empty across pause -> seed cycles."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock
        from my_idm.database import Database
        from my_idm.torrent_engine import TorrentEngine

        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.db = Database(Path(self.tmp_dir.name) / "test.db")
        self.db.open()
        self.addCleanup(self.db.close)
        self.entry = self.db.add_download(
            DownloadEntry(
                id="t1", url="magnet:?xt=urn:btih:abc", filename="show.torrent",
                save_path=self.tmp_dir.name, file_path=f"{self.tmp_dir.name}/show.torrent",
                download_type="torrent", total_size=1000, downloaded_size=1000,
                status="completed",
            )
        )
        # A bare engine with a mocked handle, matching test_torrent_engine.py.
        # A full DownloadManager per test is avoided: it spins up Qt timers and a
        # libtorrent session, which is heavyweight and flaky for this narrow case.
        self.te = TorrentEngine(self.db)
        self.te._running = True
        self.te._session = MagicMock()
        self.te._handles["t1"] = self._fake_handle()
        # Real config objects, not mocks: _apply_seeding_limit_to_handle() and
        # poll_all() compare these values against ints, which a bare MagicMock
        # cannot satisfy.
        from my_idm.config import GeneralConfig, TorrentConfig

        self.te._torrent_config = TorrentConfig()
        self.te._torrent_config.seeding_after_complete = True
        self.te._general_config = GeneralConfig()
        self.te.get_torrent_files = lambda *a, **k: []
        self.te.get_torrent_trackers = lambda *a, **k: []
        self.te.get_torrent_peers = lambda *a, **k: []

    @staticmethod
    def _fake_handle(state=None):
        """A handle whose ``status()`` is a real object with real numbers.

        A bare ``MagicMock()`` silently disables the whole status path:
        ``get_status`` sees a truthy ``has_metadata``, calls
        ``torrent_file()`` which is another mock, and ``ti.total_size() > 0``
        raises ``TypeError`` on a mock. ``get_status`` swallows it and returns
        ``None``, so ``start_seeding``'s ``cur_status`` is always ``{}`` and
        ``seeding_baseline_upload`` is never exercised. ``torrent_file`` and
        ``get_torrent_info`` are pinned to ``None`` so the metadata branch is
        skipped deterministically.
        """
        from unittest.mock import MagicMock

        class Status:
            total_wanted = 1000
            total_wanted_done = 1000
            total_done = 0
            num_seeds = 3
            num_peers = 0
            num_complete = 3
            list_seeds = 0
            num_incomplete = 0
            list_peers = 0
            download_rate = 0
            upload_rate = 0
            progress = 1.0
            has_metadata = True
            state = 5  # _STATE_NAMES[5] == "seeding"
            all_time_upload = 4096
            all_time_download = 8192
            last_seen_complete = 0

        handle = MagicMock()
        handle.status.return_value = state() if state is not None else Status()
        handle.torrent_file.return_value = None
        handle.get_torrent_info.return_value = None
        handle.is_valid.return_value = True
        return handle

    def test_seed_fills_last_seeded(self):
        self.assertEqual(self.db.get_download("t1").last_seeded_at, "")
        self.assertTrue(self.te.start_seeding("t1"))
        stamped = self.db.get_download("t1")
        self.assertTrue(stamped.last_seeded_at)
        self.assertEqual(
            stamped.last_seeded_at, stamped.seeding_started_at,
            "the column and the legacy metadata key must agree",
        )
        self.assertEqual(stamped.metadata.get("seeding_since"), stamped.last_seeded_at)
        self.assertTrue(stamped.metadata.get("manual_seeding"))
        self.assertEqual(stamped.status, "seeding")

    def test_start_seeding_uses_the_status_path_for_the_upload_baseline(self):
        """Proves the status path really ran (CRITICAL 2).

        With a working handle, ``get_status`` returns a real dict and
        ``start_seeding`` records ``total_upload`` as the baseline. With the old
        bare mock the status lookup returned None and the baseline silently fell
        back to 0.
        """
        self.assertTrue(self.te.start_seeding("t1"))
        entry = self.db.get_download("t1")
        self.assertEqual(
            entry.metadata.get("seeding_baseline_upload"), 4096,
            "the baseline must come from get_status()['total_upload'] "
            "(all_time_upload=4096), not the 0 fallback of a swallowed TypeError",
        )
        self.te._handles["t1"].status.assert_called()

    def test_start_seeding_baseline_falls_back_when_status_is_unavailable(self):
        """The documented fallback when libtorrent has no status for the handle."""
        self.te._handles["t1"].status.side_effect = RuntimeError("no status")
        self.assertTrue(self.te.start_seeding("t1"))
        entry = self.db.get_download("t1")
        self.assertEqual(entry.metadata.get("seeding_baseline_upload"), 0)
        self.assertTrue(entry.last_seeded_at, "the timestamp is stamped regardless")

    def test_repeat_seed_updates_timestamp(self):
        self.te.start_seeding("t1")
        first = self.db.get_download("t1").last_seeded_at
        self.assertTrue(first)

        self.te.start_seeding("t1")
        second = self.db.get_download("t1").last_seeded_at
        self.assertGreaterEqual(second, first)

    def test_pause_then_seed_updates_timestamp(self):
        """The reported sequence: pause, seed, pause, seed."""
        seen = []
        for _ in range(2):
            self.te.start_seeding("t1")
            seen.append(self.db.get_download("t1").last_seeded_at)
            self.assertTrue(seen[-1])
            # No time.sleep here: mark_seeding_started uses
            # datetime.now(timezone.utc).isoformat(), whose resolution is
            # microseconds, so a 10 ms sleep was ~10000x more than needed and
            # unpredictable anyway (Windows timer granularity is ~15.6 ms).
            entry = self.db.get_download("t1")
            entry.status = "paused"
            self.db.update_download(entry)
            self.db.update_status("t1", "paused")
        self.te.start_seeding("t1")
        seen.append(self.db.get_download("t1").last_seeded_at)
        for earlier, later in zip(seen, seen[1:]):
            self.assertGreaterEqual(later, earlier)
        self.assertEqual(len(seen), 3)

    def test_completion_transition_stamps_last_seeded(self):
        """Reaching the seeding state must stamp even without start_seeding."""
        entry = self.db.get_download("t1")
        entry.status = "downloading"
        self.db.update_download(entry)

        # The class-level handle already has a real status object with
        # state=5 ("seeding"), so poll_all() sees the completion transition.
        self.te.poll_all()

        after = self.db.get_download("t1")
        self.assertTrue(after.last_seeded_at, "completion into seeding must stamp")
        self.assertTrue(after.metadata.get("seeding_since"))
        self.assertEqual(
            after.metadata.get("seeding_since"), after.last_seeded_at,
            "the column and the legacy metadata key must stay in step",
        )
        self.assertEqual(after.status, "seeding")
        self.te._handles["t1"].status.assert_called()

    def test_poll_backstop_never_regresses_a_newer_stamp(self):
        """libtorrent's last_seen_complete must not overwrite a manual seed time."""
        from datetime import datetime, timezone
        from my_idm.torrent_engine import _newer_seed_stamp

        manual = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc).isoformat()
        older_epoch = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp())
        self.assertEqual(_newer_seed_stamp(manual, older_epoch), "")
        newer_epoch = int(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp())
        self.assertTrue(_newer_seed_stamp(manual, newer_epoch).startswith("2027"))
        self.assertTrue(_newer_seed_stamp("", older_epoch).startswith("2020"))

    def test_poll_backstop_ignores_invalid_input(self):
        from my_idm.torrent_engine import _newer_seed_stamp

        self.assertEqual(_newer_seed_stamp("2026-01-01T00:00:00+00:00", 0), "")
        self.assertEqual(_newer_seed_stamp("2026-01-01T00:00:00+00:00", -5), "")
        self.assertEqual(_newer_seed_stamp("2026-01-01T00:00:00+00:00", 10**18), "")
        self.assertEqual(_newer_seed_stamp("not-a-date", 0), "")


class TestYoutubeProgressMonotonic(unittest.TestCase):
    """Regression: merged video+audio downloads made the bar jump backwards."""

    @staticmethod
    def _status(state, done, total, filename, fmt):
        return {
            "status": state, "downloaded_bytes": done, "total_bytes": total,
            "filename": filename,
            "info_dict": {"format_id": fmt, "filename": filename},
            "speed": 1000.0, "eta": 10,
        }

    def _run(self, statuses, expected_total):
        """Drive start_native_download with a fake ydl that replays *statuses*."""
        import threading
        from unittest.mock import MagicMock, patch
        from my_idm import youtube_tool as ytt
        from my_idm.config import ExternalToolsConfig

        captured = []
        done = threading.Event()

        class FakeYdl:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def add_progress_hook(self, fn):
                self.opts["progress_hooks"].insert(0, fn)

            def download(self, urls):
                for s in statuses:
                    for fn in list(self.opts["progress_hooks"]):
                        fn(dict(s))
                return 0

        module = MagicMock()
        module.YoutubeDL.side_effect = FakeYdl
        with patch.object(ytt, "_import_ytdlp", return_value=module):
            ytt.start_native_download(
                "https://youtu.be/x", "/tmp", "137+140",
                ExternalToolsConfig(ytdlp_ffmpeg_path=""),
                progress_cb=captured.append,
                done_cb=lambda p: done.set(),
                error_cb=lambda m: (captured.append({"error": m}), done.set()),
                expected_total=expected_total,
            )
            self.assertTrue(done.wait(timeout=10))
        return [c for c in captured if "error" not in c]

    def test_merged_download_percentage_never_regresses(self):
        video = [self._status("downloading", d, 50_000_000, "v.mp4", "137")
                 for d in (0, 10_000_000, 30_000_000, 50_000_000)]
        video.append(self._status("finished", 50_000_000, 50_000_000, "v.mp4", "137"))
        audio = [self._status("downloading", d, 5_000_000, "a.m4a", "140")
                 for d in (0, 2_000_000, 5_000_000)]
        audio.append(self._status("finished", 5_000_000, 5_000_000, "a.m4a", "140"))

        events = self._run(video + audio, expected_total=55_000_000)
        pcts = [
            (100.0 * e["downloaded_bytes"] / e["total_bytes"]) if e.get("total_bytes") else 0.0
            for e in events
        ]
        for earlier, later in zip(pcts, pcts[1:]):
            self.assertGreaterEqual(later, earlier - 0.001, f"{pcts}")
        self.assertAlmostEqual(pcts[-1], 100.0, places=3)

    def test_downloaded_bytes_is_monotonic(self):
        video = [self._status("downloading", d, 50_000_000, "v.mp4", "137")
                 for d in (0, 10_000_000, 50_000_000)]
        audio = [self._status("downloading", d, 5_000_000, "a.m4a", "140")
                 for d in (0, 2_000_000, 5_000_000)]
        events = self._run(video + audio, expected_total=55_000_000)
        downloaded = [e["downloaded_bytes"] for e in events]
        for earlier, later in zip(downloaded, downloaded[1:]):
            self.assertGreaterEqual(later, earlier, f"{downloaded}")

    def test_total_is_stable_across_streams(self):
        video = [self._status("downloading", 10_000_000, 50_000_000, "v.mp4", "137")]
        audio = [self._status("downloading", 2_000_000, 5_000_000, "a.m4a", "140")]
        events = self._run(video + audio, expected_total=55_000_000)
        totals = {e.get("total_bytes") for e in events if e.get("total_bytes")}
        self.assertEqual(totals, {55_000_000}, f"total must not change: {totals}")

    def test_final_total_equals_sum_of_streams(self):
        events = self._run(
            [self._status("downloading", 50_000_000, 50_000_000, "v.mp4", "137"),
             self._status("downloading", 5_000_000, 5_000_000, "a.m4a", "140")],
            expected_total=0,
        )
        self.assertEqual(events[-1]["downloaded_bytes"], 55_000_000)
        self.assertEqual(events[-1]["total_bytes"], 55_000_000)


class TestSizeFilter(unittest.TestCase):
    """Size-bucket filter on the Col.SIZE column header."""

    MB = 1024 * 1024
    GB = 1024 * 1024 * 1024

    def _model(self):
        model = DownloadTableModel()
        sizes = [
            1 * self.MB, 50 * self.MB, 500 * self.MB, 3 * self.GB,
            8 * self.GB, 25 * self.GB, 80 * self.GB,
        ]
        entries = [
            DownloadEntry(
                id=f"e{i}", url=f"http://x/{i}", filename=f"f{i}.bin",
                save_path=".", file_path=f"./f{i}.bin", total_size=s,
                downloaded_size=s, status="completed", download_type="http",
            )
            for i, s in enumerate(sizes)
        ]
        model.load_entries(entries)
        return model, entries

    def test_bucket_labels_match_spec(self):
        from my_idm.download_model import SIZE_FILTER_LABELS

        self.assertEqual(
            list(SIZE_FILTER_LABELS.values()),
            ["<10MB", "10-100MB", "100MB-1GB", "1-5GB", "5-10GB", "10-50GB", ">50GB"],
        )

    def test_bucket_boundaries(self):
        from my_idm.download_model import size_bucket_for

        cases = [
            (0, "lt_10mb"), (10 * self.MB - 1, "lt_10mb"),
            (10 * self.MB, "10mb_100mb"), (100 * self.MB - 1, "10mb_100mb"),
            (100 * self.MB, "100mb_1gb"), (self.GB - 1, "100mb_1gb"),
            (self.GB, "1gb_5gb"), (5 * self.GB - 1, "1gb_5gb"),
            (5 * self.GB, "5gb_10gb"), (10 * self.GB - 1, "5gb_10gb"),
            (10 * self.GB, "10gb_50gb"), (50 * self.GB - 1, "10gb_50gb"),
            (50 * self.GB, "gt_50gb"), (500 * self.GB, "gt_50gb"),
        ]
        for size, expected in cases:
            self.assertEqual(size_bucket_for(size), expected, f"{size} bytes")

    def test_unknown_and_negative_sizes_fall_into_smallest_bucket(self):
        from my_idm.download_model import size_bucket_for

        self.assertEqual(size_bucket_for(None), "lt_10mb")
        self.assertEqual(size_bucket_for(""), "lt_10mb")
        self.assertEqual(size_bucket_for(-5), "lt_10mb")

    def test_filter_selects_only_that_bucket(self):
        model, _ = self._model()
        model.set_size_filter({"1gb_5gb"})
        self.assertEqual(model.rowCount(), 1)
        self.assertTrue(model.is_size_filtered())
        self.assertTrue(model.is_filtered())

    def test_filter_supports_multiple_buckets(self):
        model, _ = self._model()
        model.set_size_filter({"lt_10mb", "gt_50gb"})
        self.assertEqual(model.rowCount(), 2)

    def test_selecting_every_bucket_clears_the_filter(self):
        from my_idm.download_model import SIZE_FILTER_BUCKETS

        model, _ = self._model()
        model.set_size_filter({key for key, _l, _a, _b in SIZE_FILTER_BUCKETS})
        self.assertFalse(model.is_size_filtered())
        self.assertEqual(model.rowCount(), 7)

    def test_counts_cover_every_bucket_and_honour_other_filters(self):
        model, _ = self._model()
        counts = model.get_size_counts()
        self.assertEqual(len(counts), 7)
        self.assertEqual(sum(counts.values()), 7)
        model.set_type_filter({"torrent"})
        self.assertEqual(sum(model.get_size_counts().values()), 0)

    def test_combines_with_type_filter(self):
        model, _ = self._model()
        model.set_size_filter({"10gb_50gb"})
        model.set_type_filter({"torrent"})
        self.assertEqual(model.rowCount(), 0)
        model.set_type_filter({"http"})
        self.assertEqual(model.rowCount(), 1)

    def test_clear_filters_resets_size(self):
        model, _ = self._model()
        model.set_size_filter({"lt_10mb"})
        model.clear_filters()
        self.assertFalse(model.is_size_filtered())
        self.assertFalse(model.is_filtered())
        self.assertEqual(model.rowCount(), 7)

    def test_header_exposes_size_column(self):
        from my_idm.header_view import FilterHeaderView

        self.assertIn(Col.SIZE, FilterHeaderView._FILTER_COLUMNS)
        self.assertEqual(FilterHeaderView._FILTER_COLUMNS[Col.SIZE], "Size")
        self.assertIn(Col.STATUS, FilterHeaderView._FILTER_COLUMNS)
        self.assertIn(Col.NAME, FilterHeaderView._FILTER_COLUMNS)

    def test_size_filter_popup_lists_size_buckets(self):
        """Regression: the Size popup listed the HTTP/BitTorrent type labels."""
        from my_idm.download_model import SIZE_FILTER_LABELS
        from my_idm.header_view import MultiselectFilterPopup

        MB = 1024 * 1024
        model = DownloadTableModel()
        model.load_entries(
            [
                DownloadEntry(
                    id=f"e{i}", url=f"http://x/{i}", filename=f"f{i}.bin",
                    save_path=".", file_path=f"./f{i}.bin", total_size=s,
                    downloaded_size=s, status="completed", download_type="http",
                )
                for i, s in enumerate((1 * MB, 3 * 1024 ** 3, 80 * 1024 ** 3))
            ]
        )

        popup = MultiselectFilterPopup(Col.SIZE, None, model.get_size_counts())
        self.assertEqual(
            set(popup._checkboxes.keys()), set(SIZE_FILTER_LABELS.keys()),
            "popup keys must be the size bucket ids",
        )
        labels = [cb.text() for cb in popup._checkboxes.values()]
        for expected in SIZE_FILTER_LABELS.values():
            self.assertTrue(
                any(lbl.startswith(expected) for lbl in labels),
                f"missing bucket {expected!r} in {labels}",
            )
        # The download-type labels must not leak into the Size popup.
        self.assertFalse(any(lbl.startswith("HTTP") for lbl in labels))
        self.assertFalse(any(lbl.startswith("BitTorrent") for lbl in labels))
        # Counts are per bucket, so three rows spread over three buckets.
        counted = [lbl for lbl in labels if not lbl.endswith("(0)")]
        self.assertEqual(len(counted), 3, f"expected 3 non-zero buckets, got {labels}")


class TestSeedingStartedAtColumn(unittest.TestCase):
    """The Seeding Started At column and its use for the duration limit."""

    def _entry(self, **kw):
        e = DownloadEntry(
            id="t1", url="magnet:?xt=urn:btih:abc", filename="a.torrent",
            save_path=".", file_path="./a.torrent", download_type="torrent",
            status="completed",
        )
        for k, v in kw.items():
            setattr(e, k, v)
        return e

    def test_defaults_to_empty(self):
        self.assertEqual(self._entry().seeding_started_at, "")

    def test_index_and_header(self):
        self.assertEqual(Col.SEEDING_STARTED_AT, 16)
        self.assertEqual(Col.HEADERS[Col.SEEDING_STARTED_AT], "Seeding Started At")

    def test_display_and_sort(self):
        model = DownloadTableModel()
        unset = self._entry()
        set_ = self._entry(seeding_started_at="2026-09-28T10:11:12+00:00")
        self.assertEqual(model._display_data(unset, Col.SEEDING_STARTED_AT), "—")
        self.assertRegex(
            model._display_data(set_, Col.SEEDING_STARTED_AT), r"^\d{4}-\d{2}-\d{2} "
        )
        self.assertIsInstance(
            model._entry_sort_key(set_, Col.SEEDING_STARTED_AT, True), tuple
        )

    def test_mark_seeding_started_mirrors_both_fields(self):
        from datetime import datetime, timezone
        from my_idm.torrent_engine import mark_seeding_started

        e = self._entry()
        stamp = mark_seeding_started(e)
        self.assertEqual(e.seeding_started_at, stamp)
        self.assertEqual(e.metadata.get("seeding_since"), stamp)

    def test_mark_seeding_started_overwrites_previous_session(self):
        from datetime import datetime, timedelta, timezone
        from my_idm.torrent_engine import mark_seeding_started

        e = self._entry()
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        first = mark_seeding_started(e, t0)
        second = mark_seeding_started(e, t0 + timedelta(minutes=5))
        self.assertNotEqual(first, second)
        self.assertEqual(e.seeding_started_at, second)

    def test_seeding_session_start_prefers_column(self):
        from my_idm.torrent_engine import seeding_session_start

        e = self._entry(seeding_started_at="2026-09-28T10:00:00+00:00")
        e.metadata["seeding_since"] = "2020-01-01T00:00:00+00:00"
        self.assertEqual(seeding_session_start(e), "2026-09-28T10:00:00+00:00")

    def test_seeding_session_start_falls_back_to_metadata(self):
        from my_idm.torrent_engine import seeding_session_start

        e = self._entry()
        e.metadata["seeding_since"] = "2020-01-01T00:00:00+00:00"
        self.assertEqual(seeding_session_start(e), "2020-01-01T00:00:00+00:00")

    def test_progress_update_repaints_seeding_telemetry_columns(self):
        """Regression: the tail columns kept a stale value after a silent stamp.

        The engine can stamp seeding_started_at / last_seeded_at from the poll
        with no status transition, so update_progress must repaint those cells.
        """
        model = DownloadTableModel()
        entry = make_entry()
        entry.download_type = "torrent"
        entry.status = "seeding"
        entry.total_size = 1000
        entry.downloaded_size = 1000
        model.load_entries([entry])

        seen = []
        model.dataChanged.connect(
            lambda tl, br, roles=None: seen.append((tl.column(), br.column()))
        )
        model.update_progress(entry.id, 1000, 1000, 0.0, 0.0, 1, 0, 0.0)

        covered = {(a, b) for a, b in seen}
        self.assertTrue(
            any(a <= Col.LAST_SEEDED and b >= Col.SEEDING_STARTED_AT for a, b in covered),
            f"seeding columns not repainted: {covered}",
        )

    def test_status_change_syncs_volatile_fields_into_the_row(self):
        """Regression: seeding timestamps were written but never shown.

        update_status only patches status and error_message, and the model keeps
        its own snapshot of each row, so fields the engine writes alongside the
        status stayed stale until the whole table reloaded.
        """
        model = DownloadTableModel()
        entry = make_entry()
        entry.download_type = "torrent"
        entry.status = "seeding"
        model.load_entries([entry])

        row = model.row_for_id(entry.id)
        self.assertIsNotNone(row)
        self.assertEqual(model._display_data(model._entries[row], Col.SEEDING_STARTED_AT), "—")

        fresh = make_entry()
        fresh.download_type = "torrent"
        fresh.status = "seeding"
        fresh.seeding_started_at = "2026-09-28T10:00:00+00:00"
        fresh.last_seeded_at = "2026-09-28T10:00:00+00:00"
        model.update_status(entry.id, "seeding", "")
        model.refresh_entry(entry.id, fresh)

        shown = model._display_data(
            model._entries[model.row_for_id(entry.id)], Col.SEEDING_STARTED_AT
        )
        self.assertRegex(shown, r"^\d{4}-\d{2}-\d{2} ")

    def test_seeding_session_start_empty(self):
        from my_idm.torrent_engine import seeding_session_start

        self.assertEqual(seeding_session_start(self._entry()), "")

class TestAnimePaheBacklogProvenance(unittest.TestCase):
    """AnimePahe writes a comment above each backlog URL; it must mark the source."""

    def setUp(self):
        import tempfile
        from my_idm.database import Database
        from my_idm.manager import DownloadManager

        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(":memory:")
        self.db.open()
        self.manager = DownloadManager(self.db)

    def tearDown(self):
        self.manager.stop()
        self.db.close()
        self.tmp.cleanup()

    def _write_backlog(self, text):
        import os
        path = os.path.join(self.tmp.name, "backlog.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_animepahe_comment_marks_source(self):
        path = self._write_backlog(
            "# My-IDM Backlog File - Generated by AnimePahe Downloader\n"
            "\n"
            "# AnimePahe Download\n"
            "https://host.example/episode.mkv\n"
        )
        self.manager.load_backlog(path)
        entry = self.db.get_all_downloads()[0]
        self.assertEqual(resolve_download_source(entry), ANIMEPAHE_SOURCE)

    def test_titled_animepahe_comment_marks_source(self):
        """The comment text is variable, so a substring match must be used."""
        path = self._write_backlog(
            "# Some Show - Episode 3 (ep03.mkv)\n"
            "https://host.example/ep03.mkv\n"
            "\n"
            "# AnimePahe Download\n"
            "https://host.example/ep04.mkv\n"
        )
        self.manager.load_backlog(path)
        entries = {e.url: e for e in self.db.get_all_downloads()}
        # Only the entry directly under the AnimePahe comment is marked; the
        # comment applies to the following line only.
        self.assertEqual(
            resolve_download_source(entries["https://host.example/ep04.mkv"]),
            ANIMEPAHE_SOURCE,
        )
        self.assertEqual(resolve_download_source(entries["https://host.example/ep03.mkv"]), "")

    def test_plain_backlog_entry_is_unmarked(self):
        path = self._write_backlog("https://host.example/plain.zip\n")
        self.manager.load_backlog(path)
        entry = self.db.get_all_downloads()[0]
        self.assertEqual(resolve_download_source(entry), "")

    def test_backlog_without_comments_unaffected(self):
        path = self._write_backlog("https://a.example/1.zip\nhttps://b.example/2.zip\n")
        self.manager.load_backlog(path)
        self.assertEqual(len(self.db.get_all_downloads()), 2)
        for entry in self.db.get_all_downloads():
            self.assertEqual(resolve_download_source(entry), "")


if __name__ == "__main__":
    unittest.main()
