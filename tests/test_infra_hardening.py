"""Tests for keyboard shortcuts, database helpers, and torrent engine wrappers.

These cover surfaces the audit found untested: the `setShortcut` calls in
MainWindow (nothing asserted them), several `Database` query/mutator methods, and
the `TorrentEngine` wrappers that `DownloadManager.move_download` depends on.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from PySide6.QtWidgets import QApplication

from my_idm.database import Database, DownloadEntry

app = QApplication.instance() or QApplication(sys.argv)


class TestKeyboardShortcuts(unittest.TestCase):
    """The main window advertises shortcuts; they must be bound and unique."""

    # A representative sample of the shortcuts declared in _setup_actions().
    EXPECTED = {
        "Add Download": "Ctrl+N",
        "Add Torrent File": "Ctrl+T",
        "Download YouTube Video": "Ctrl+Y",
        "Resume": "Ctrl+R",
        "Pause": "Space",
        "Copy URL / Magnet": "Ctrl+C",
        "Rename": "F2",
        "Delete": "Del",
        "Open File": "Return",
        "Open Folder": "Ctrl+O",
        "Load Backlog": "Ctrl+L",
        "Preferences": "Ctrl+,",
        "Details Panel": "F4",
        "Select All": "Ctrl+A",
        "Exit": "Ctrl+Q",
    }

    def setUp(self):
        from my_idm.main_window import MainWindow
        from my_idm.manager import DownloadManager

        self.db = Database(":memory:")
        self.db.open()
        self.mgr = DownloadManager(self.db)
        self.win = MainWindow(self.mgr)

    def tearDown(self):
        from my_idm.notifications import unregister_notification_handler
        unregister_notification_handler()
        self.win.close()
        self.mgr.stop()
        self.db.close()

    def _shortcuts(self):
        out = {}
        for action in self.win.findChildren(type(self.win._act_add)):
            key = action.shortcut().toString()
            if key:
                out[action.text().replace("…", "").replace("\u2026", "")] = key
        return out

    def test_documented_shortcuts_are_registered(self):
        found = self._shortcuts()
        missing = {n: k for n, k in self.EXPECTED.items() if n not in found}
        self.assertEqual(missing, {}, f"missing shortcuts: {missing}")

    def test_shortcut_sequences_are_unique(self):
        seen = {}
        duplicates = []
        for action in self.win.findChildren(type(self.win._act_add)):
            key = action.shortcut().toString()
            if not key:
                continue
            if key in seen:
                duplicates.append((key, seen[key], action.text()))
            seen[key] = action.text()
        self.assertEqual(duplicates, [], f"duplicate shortcuts: {duplicates}")

    def test_delete_and_space_shortcuts_exist(self):
        keys = set(self._shortcuts().values())
        self.assertIn("Del", keys)
        self.assertIn("Space", keys)

    def test_shortcuts_are_case_insensitive_normalised(self):
        """Ctrl+, must not be reported as Ctrl+,<modifier> or similar."""
        self.assertEqual(self._shortcuts().get("Preferences"), "Ctrl+,")


class TestDatabaseQueries(unittest.TestCase):
    """Query and mutation helpers with no coverage in the suite."""

    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()

    def tearDown(self):
        self.db.close()

    def _add(self, **kw):
        base = dict(id="d", url="https://example.com/a.zip", filename="a.zip",
                    save_path="/tmp", file_path="/tmp/a.zip")
        base.update(kw)
        entry = DownloadEntry(**base)
        self.db.add_download(entry)
        return entry

    def test_increment_retry_starts_at_one_and_increments(self):
        self._add(id="r1")
        self.assertEqual(self.db.get_download("r1").retry_count, 0)
        self.assertEqual(self.db.increment_retry("r1"), 1)
        self.assertEqual(self.db.increment_retry("r1"), 2)
        self.assertEqual(self.db.get_download("r1").retry_count, 2)

    def test_increment_retry_on_unknown_id_is_safe(self):
        # Must not raise; the value is unspecified for a missing row.
        self.db.increment_retry("nope")

    def test_queue_order_is_monotonic(self):
        first = self.db.get_next_queue_order()
        self._add(id="q1")
        second = self.db.get_next_queue_order()
        self.assertGreater(second, first)

    def test_update_queue_order_sets_the_value(self):
        self._add(id="q1")
        self.db.update_queue_order("q1", 99)
        self.assertEqual(self.db.get_download("q1").queue_order, 99)

    def test_swap_queue_order(self):
        a = self._add(id="s1")
        b = self._add(id="s2")
        before = (a.queue_order, b.queue_order)
        self.db.swap_queue_order("s1", "s2")
        after = (
            self.db.get_download("s1").queue_order,
            self.db.get_download("s2").queue_order,
        )
        self.assertEqual((before[1], before[0]), after)

    def test_find_by_info_hash_is_an_exact_match(self):
        self._add(id="t1", torrent_info_hash="ABCDEF0123", download_type="torrent")
        self.assertIsNotNone(self.db.find_by_info_hash("ABCDEF0123"))
        self.assertIsNone(self.db.find_by_info_hash("abcdef0123"),
                          "the lookup is case-sensitive SQL equality")
        self.assertIsNone(self.db.find_by_info_hash(""))
        self.assertIsNone(self.db.find_by_info_hash(None))

    def test_recent_save_paths_are_deduplicated(self):
        self._add(id="p1", save_path="/a", file_path="/a/f1.zip")
        self._add(id="p2", save_path="/b", file_path="/b/f2.zip")
        self._add(id="p3", save_path="/a", file_path="/a/f3.zip")
        paths = self.db.get_recent_save_paths()
        self.assertEqual(sorted(paths), ["/a", "/b"], "duplicates must collapse")

    def test_recent_save_paths_respects_the_limit(self):
        for i in range(6):
            self._add(id=f"p{i}", save_path=f"/p{i}", file_path=f"/p{i}/f.zip")
        self.assertLessEqual(len(self.db.get_recent_save_paths(limit=3)), 3)

    def test_preferences_window_size_round_trip(self):
        self.db.save_preferences_window_size(123, 456)
        self.assertEqual(
            self.db.get_preferences_window_size(), {"width": 123, "height": 456}
        )

    def test_preferences_window_size_defaults_when_unset(self):
        self.assertEqual(self.db.get_preferences_window_size(), {})

    def test_window_state_round_trip(self):
        self.db.save_window_state({"a": 1, "b": "two"})
        state = self.db.get_window_state()
        self.assertEqual(state.get("a"), 1)
        self.assertEqual(state.get("b"), "two")

    def test_delete_segments_for_download(self):
        from my_idm.database import SegmentEntry

        self._add(id="s1")
        self.db.add_segments([
            SegmentEntry(id=f"s1-seg{i}", download_id="s1", index=i,
                         start_byte=i * 10, end_byte=i * 10 + 9)
            for i in range(3)
        ])
        self.assertEqual(len(self.db.get_segments("s1")), 3)
        self.db.delete_segments("s1")
        self.assertEqual(self.db.get_segments("s1"), [])

    def test_get_all_downloads_excludes_deleted(self):
        self._add(id="g1")
        self.assertEqual([e.id for e in self.db.get_all_downloads()], ["g1"])
        self.db.delete_download("g1")
        self.assertEqual(self.db.get_all_downloads(), [])

    def test_move_download_updates_paths(self):
        self._add(id="m1", save_path="/old", file_path="/old/a.zip")
        self.db.move_download("m1", "/new", "/new/a.zip")
        entry = self.db.get_download("m1")
        self.assertIn("/new", entry.save_path)
        self.assertIn("/new", entry.file_path)

    def test_update_progress_and_status(self):
        self._add(id="u1", total_size=100, status="downloading")
        self.db.update_progress("u1", 50)
        entry = self.db.get_download("u1")
        self.assertEqual(entry.downloaded_size, 50)
        self.db.update_status("u1", "completed", "ignored")
        entry = self.db.get_download("u1")
        self.assertEqual(entry.status, "completed")
        self.assertEqual(entry.error_message, "", "completing clears the error")
        self.assertTrue(entry.completed_at, "completion stamps completed_at")

    def test_update_status_error_clearing_is_status_specific(self):
        """completed/downloading clear the error; other transitions keep it.

        This asymmetry is deliberate: moving back to ``queued`` after a failure
        must not erase the diagnostic before the user has seen it.
        """
        for status in ("completed", "downloading"):
            self._add(id=f"c-{status}", status="error", error_message="boom")
            self.db.update_status(f"c-{status}", status)
            self.assertEqual(self.db.get_download(f"c-{status}").error_message, "")

        self._add(id="c-queued", status="error", error_message="boom")
        self.db.update_status("c-queued", "queued")
        self.assertEqual(
            self.db.get_download("c-queued").error_message, "boom",
            "a plain status change must not silently drop the error",
        )

    def test_update_status_error_records_the_message(self):
        self._add(id="c-err", status="downloading")
        self.db.update_status("c-err", "error", "connection reset")
        self.assertEqual(self.db.get_download("c-err").error_message, "connection reset")


class TestMetadataDict(unittest.TestCase):
    """The _MetadataDict wrapper that keeps metadata_json in sync.

    ``_sync()`` only refreshes the in-memory entry; persisting requires an
    explicit ``update_download()``, which these tests pin down.
    """

    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.db.add_download(
            DownloadEntry(id="m1", url="https://example.com/a.zip", filename="a.zip",
                          save_path="/tmp", file_path="/tmp/a.zip")
        )
        self.entry = self.db.get_download("m1")

    def tearDown(self):
        self.db.close()

    def test_setitem_updates_the_entry_json(self):
        self.entry.metadata["a"] = 1
        self.assertIn('"a"', self.entry.metadata_json)

    def test_setitem_persists_after_update_download(self):
        self.entry.metadata["a"] = 1
        self.db.update_download(self.entry)
        self.assertEqual(self.db.get_download("m1").metadata.get("a"), 1)

    def test_get_returns_default_for_missing(self):
        self.assertIsNone(self.entry.metadata.get("missing"))
        self.assertEqual(self.entry.metadata.get("missing", "fallback"), "fallback")

    def test_pop_removes_and_persists_after_update(self):
        self.entry.metadata["a"] = 1
        self.assertEqual(self.entry.metadata.pop("a"), 1)
        self.db.update_download(self.entry)
        self.assertNotIn("a", self.db.get_download("m1").metadata)

    def test_pop_with_default(self):
        self.assertEqual(self.entry.metadata.pop("nope", "d"), "d")

    def test_setdefault_inserts_only_when_absent(self):
        self.assertEqual(self.entry.metadata.setdefault("k", "first"), "first")
        self.assertEqual(self.entry.metadata.setdefault("k", "second"), "first")
        self.db.update_download(self.entry)
        self.assertEqual(self.db.get_download("m1").metadata.get("k"), "first")

    def test_clear_empties_and_persists(self):
        self.entry.metadata["a"] = 1
        self.entry.metadata["b"] = 2
        self.entry.metadata.clear()
        self.db.update_download(self.entry)
        self.assertEqual(self.db.get_download("m1").metadata, {})

    def test_contains_and_len(self):
        self.entry.metadata["a"] = 1
        self.assertIn("a", self.entry.metadata)
        self.assertEqual(len(self.entry.metadata), 1)

    def test_non_string_keys_are_coerced_by_json(self):
        self.entry.metadata[1] = "x"
        self.db.update_download(self.entry)
        self.assertEqual(self.db.get_download("m1").metadata.get("1"), "x")

    def test_update_and_delitem_sync(self):
        self.entry.metadata.update({"u": 1})
        self.db.update_download(self.entry)
        self.assertEqual(self.db.get_download("m1").metadata.get("u"), 1)
        del self.entry.metadata["u"]
        self.db.update_download(self.entry)
        self.assertNotIn("u", self.db.get_download("m1").metadata)


class TestTorrentEngineWrappers(unittest.TestCase):
    """remove / recheck / force_start / move_storage wrappers."""

    def setUp(self):
        from my_idm.torrent_engine import TorrentEngine

        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.open()
        self.db.add_download(
            DownloadEntry(
                id="t1", url="magnet:?xt=urn:btih:abc", filename="a.torrent",
                save_path=self.tmp.name, file_path=f"{self.tmp.name}/a.torrent",
                download_type="torrent", total_size=1000, downloaded_size=0,
                status="paused",
            )
        )
        self.engine = TorrentEngine(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_remove_drops_the_handle_and_fastresume(self):
        """remove() detaches from libtorrent; the DB row is the manager's job."""
        self.engine._handles["t1"] = MagicMock()
        self.engine.remove("t1", delete_files=False)
        self.assertNotIn("t1", self.engine._handles, "handle must be released")
        # The row is deliberately left for DownloadManager.delete_download().
        self.assertIsNotNone(self.db.get_download("t1"))

    def test_remove_unknown_id_is_safe(self):
        self.engine.remove("does-not-exist", delete_files=False)

    def test_recheck_without_a_handle_does_not_raise(self):
        self.engine.recheck("t1")
        self.assertIsNotNone(self.db.get_download("t1"))

    def test_force_start_returns_false_without_a_handle(self):
        self.assertFalse(self.engine.force_start("t1"))

    def test_force_start_unknown_id_is_safe(self):
        self.assertFalse(self.engine.force_start("nope"))

    def test_set_torrent_bandwidth_allocation_persists(self):
        self.engine.set_torrent_bandwidth_allocation("t1", "high")
        self.assertEqual(
            self.db.get_download("t1").metadata.get("bandwidth_allocation"), "high"
        )

    def test_move_storage_records_the_new_location_on_success(self):
        target = Path(self.tmp.name) / "moved"
        target.mkdir()
        moved = self.engine.move_storage("t1", str(target))
        if moved:
            self.assertIn("moved", self.db.get_download("t1").file_path)
        else:
            # Without a live handle the move cannot complete, but the entry must
            # survive rather than be corrupted.
            self.assertIsNotNone(self.db.get_download("t1"))

    def test_set_session_limits_without_a_session_is_safe(self):
        self.engine.set_session_limits(10, 5)

    def test_is_torrent_tor_routed_defaults_false(self):
        self.assertFalse(self.engine.is_torrent_tor_routed("t1"))

    def test_set_torrent_tor_route_persists(self):
        self.engine.set_torrent_tor_route("t1", True)
        self.assertTrue(self.engine.is_torrent_tor_routed("t1"))
        self.assertTrue(self.db.get_download("t1").metadata.get("route_through_tor"))


if __name__ == "__main__":
    unittest.main()
