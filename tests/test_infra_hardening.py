"""Tests for keyboard shortcuts, database helpers, and torrent engine wrappers.

These cover surfaces the audit found untested: the `setShortcut` calls in
MainWindow (nothing asserted them), several `Database` query/mutator methods, and
the `TorrentEngine` wrappers that `DownloadManager.move_download` depends on.
"""

import ast
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from PySide6.QtGui import QAction
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
        # The key is the action's text, so it follows the label - see
        # test_the_toolbar_spells_the_label_out_in_full in tests/test_statistics.py.
        "Preferences": "Ctrl+,",
        "Details Panel": "F4",
        "Select All": "Ctrl+A",
        "Exit": "Ctrl+Q",
    }

    def setUp(self):
        from my_idm.notifications import unregister_notification_handler
        from my_idm.main_window import MainWindow
        from my_idm.manager import DownloadManager

        self.addCleanup(unregister_notification_handler)
        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)
        self.mgr = DownloadManager(self.db)
        self.addCleanup(self.mgr.stop)
        self.win = MainWindow(self.mgr)
        self.addCleanup(self._destroy_window, self.win)

    @staticmethod
    def _destroy_window(win):
        """Actually destroy the window, not just hide it.

        ``MainWindow.closeEvent`` ignores the close event and hides the window
        whenever ``close_to_tray`` / ``enable_system_tray`` are on (both default
        True), so a bare ``win.close()`` leaves the window, its 1 Hz
        ``_details_timer``, its 4 s ``_tor_availability_timer`` and its tray icon
        alive for the rest of the session.
        """
        timer = getattr(win, "_tor_availability_timer", None)
        if timer is not None:
            timer.stop()
        win._force_exit = True
        win.close()
        win.deleteLater()
        QApplication.processEvents()

    def _shortcuts(self):
        out = {}
        # Match QAction explicitly: findChildren(type(win._act_add)) would silently
        # stop covering any action declared through a different QAction subclass.
        for action in self.win.findChildren(QAction):
            key = action.shortcut().toString()
            if key:
                out[action.text().replace("…", "").replace("…", "")] = key
        return out

    def test_documented_shortcuts_are_registered(self):
        found = self._shortcuts()
        missing = {n: k for n, k in self.EXPECTED.items() if n not in found}
        self.assertEqual(missing, {}, f"missing shortcuts: {missing}")

    def test_shortcut_sequences_are_unique(self):
        seen = {}
        duplicates = []
        for action in self.win.findChildren(QAction):
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

    def test_the_prefs_shortcut_is_bound_to_both_prefs_entries(self):
        """The toolbar and the Tools menu each have their own action for one feature.

        A shortcut lives on exactly one ``QAction``, so if it were set on the menu's copy it
        would fire from the toolbar too, and if set on both it would be registered twice.
        ``test_shortcut_sequences_are_unique`` catches the duplicate case; this pins which
        action owns it. Both actions now read "Preferences…", so the owner is identified by
        identity rather than by label - matching on text would accept either one.
        """
        owners = [
            action
            for action in self.win.findChildren(QAction)
            if action.shortcut().toString() == "Ctrl+,"
        ]
        self.assertEqual(
            owners, [self.win._act_preferences],
            f"Ctrl+, is bound to {[a.text() for a in owners]}, "
            f"expected exactly the toolbar action",
        )
        self.assertEqual(self.win._act_preferences.shortcut().toString(), "Ctrl+,")
        self.assertTrue(self.win._act_tools_preferences.shortcut().isEmpty())
        self.assertTrue(self.win._act_tools_stats.shortcut().isEmpty())


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
        # Must not raise, must not invent a row, and must not touch the counter
        # of any existing download.
        self._add(id="keepme")
        before = self.db.get_download("keepme").retry_count
        self.db.increment_retry("nope")
        self.assertIsNone(self.db.get_download("nope"), "no row may be created")
        self.assertEqual(self.db.get_download("keepme").retry_count, before)
        self.assertEqual(
            [e.id for e in self.db.get_all_downloads()],
            ["keepme"],
            "a retry on a missing id must not resurrect deleted rows",
        )

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
        """A canonically lowercase info hash is the form that must round-trip.

        Torrent info hashes are canonically lowercase hex (libtorrent's
        ``info_hash()`` is lowercase), so this is the case production depends on
        for de-duplication.
        """
        self._add(id="t1", torrent_info_hash="abcdef0123", download_type="torrent")
        found = self.db.find_by_info_hash("abcdef0123")
        self.assertIsNotNone(found)
        self.assertEqual(found.id, "t1")
        self.assertEqual(found.torrent_info_hash, "abcdef0123")
        self.assertIsNone(self.db.find_by_info_hash(""))
        self.assertIsNone(self.db.find_by_info_hash(None))

    def test_find_by_info_hash_is_case_sensitive_known_limitation(self):
        """Documents a real defect rather than blessing it.

        ``Database.find_by_info_hash`` compares the raw hex with SQL equality
        (database.py:407-413), so a stored UPPERCASE hash is never found by its
        lowercase spelling and the torrent is silently re-added as a duplicate.
        Fixing this requires normalising on write *and* on lookup; until then
        this test records the limitation so it is impossible to miss.
        """
        self._add(id="t1", torrent_info_hash="ABCDEF0123", download_type="torrent")
        self.assertIsNotNone(self.db.find_by_info_hash("ABCDEF0123"))
        self.assertIsNone(
            self.db.find_by_info_hash("abcdef0123"),
            "KNOWN LIMITATION: find_by_info_hash is case-sensitive SQL equality, "
            "so an uppercase stored hash is invisible to the canonical lowercase "
            "spelling and the torrent gets re-added as a duplicate",
        )

    def test_recent_save_paths_are_deduplicated(self):
        self._add(id="p1", save_path="/a", file_path="/a/f1.zip")
        self._add(id="p2", save_path="/b", file_path="/b/f2.zip")
        self._add(id="p3", save_path="/a", file_path="/a/f3.zip")
        paths = self.db.get_recent_save_paths()
        self.assertEqual(sorted(paths), ["/a", "/b"], "duplicates must collapse")

    def test_recent_save_paths_respects_the_limit(self):
        # Explicit, ascending added_at values so the expected order is derived
        # from the contract (MAX(added_at) DESC), not from insertion timing.
        for i in range(6):
            self._add(
                id=f"p{i}",
                save_path=f"/p{i}",
                file_path=f"/p{i}/f.zip",
                added_at=f"2026-01-0{i + 1}T00:00:00+00:00",
            )
        self.assertEqual(
            self.db.get_recent_save_paths(limit=3),
            ["/p5", "/p4", "/p3"],
            "limit=3 must return exactly the 3 newest paths, newest first",
        )
        self.assertEqual(len(self.db.get_recent_save_paths(limit=3)), 3)
        self.assertEqual(
            self.db.get_recent_save_paths(limit=2),
            ["/p5", "/p4"],
            "the limit is exact, not a lower bound",
        )
        self.assertEqual(self.db.get_recent_save_paths(limit=0), [])

    def test_recent_save_paths_orders_by_latest_use_not_first_use(self):
        """A path reused later must outrank a path only ever seen earlier."""
        self._add(id="a1", save_path="/a", file_path="/a/f.zip",
                  added_at="2026-01-01T00:00:00+00:00")
        self._add(id="b1", save_path="/b", file_path="/b/f.zip",
                  added_at="2026-02-01T00:00:00+00:00")
        # A second download into /a is what pushes /a ahead of /b.
        self._add(id="a2", save_path="/a", file_path="/a/g.zip",
                  added_at="2026-03-01T00:00:00+00:00")
        self.assertEqual(
            self.db.get_recent_save_paths(),
            ["/a", "/b"],
            "MAX(added_at) per save_path must drive the ordering",
        )

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
            self.assertEqual(
                self.db.get_download(f"c-{status}").error_message, "",
                f"status={status} must clear the error",
            )

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
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.open()
        self.addCleanup(self.db.close)
        self.db.add_download(
            DownloadEntry(
                id="t1", url="magnet:?xt=urn:btih:abc", filename="a.torrent",
                save_path=self.tmp.name, file_path=f"{self.tmp.name}/a.torrent",
                download_type="torrent", total_size=1000, downloaded_size=0,
                status="paused",
            )
        )
        self.engine = TorrentEngine(self.db)

    def _snapshot(self, download_id="t1"):
        entry = self.db.get_download(download_id)
        return (
            entry.id, entry.status, entry.save_path, entry.file_path,
            entry.total_size, entry.downloaded_size, entry.retry_count,
            dict(entry.metadata),
        )

    def test_remove_drops_the_handle_and_fastresume(self):
        """remove() detaches from libtorrent; the DB row is the manager's job."""
        self.engine._handles["t1"] = MagicMock()
        before = self._snapshot()
        self.engine.remove("t1", delete_files=False)
        self.assertNotIn("t1", self.engine._handles, "handle must be released")
        # The row is deliberately left for DownloadManager.delete_download().
        after = self._snapshot()
        self.assertIsNotNone(after[0])
        self.assertEqual(after, before, "remove() must not rewrite the DB row")

    def test_remove_unknown_id_is_safe(self):
        before = self._snapshot("t1")
        self.engine.remove("does-not-exist", delete_files=False)
        self.assertNotIn("does-not-exist", self.engine._handles)
        self.assertEqual(
            self._snapshot("t1"), before,
            "removing an unknown id must leave every other row untouched",
        )

    def test_recheck_without_a_handle_does_not_raise(self):
        before = self._snapshot()
        self.engine.recheck("t1")
        self.assertEqual(
            self._snapshot(), before,
            "recheck() with no live handle must not flip the row to 'checking'",
        )
        self.assertEqual(
            self.db.get_download("t1").status, "paused",
            "the status may only be advanced once libtorrent owns a handle",
        )

    def test_force_start_returns_false_without_a_handle(self):
        before = self._snapshot()
        self.assertFalse(self.engine.force_start("t1"))
        self.assertEqual(self._snapshot(), before, "no handle means no state change")

    def test_force_start_unknown_id_is_safe(self):
        before = self._snapshot("t1")
        self.assertFalse(self.engine.force_start("nope"))
        self.assertEqual(self._snapshot("t1"), before)

    def test_set_torrent_bandwidth_allocation_persists(self):
        self.engine.set_torrent_bandwidth_allocation("t1", "high")
        self.assertEqual(
            self.db.get_download("t1").metadata.get("bandwidth_allocation"), "high"
        )

    def test_move_storage_records_the_new_location_on_success(self):
        """Without a live handle the move must not report success.

        ``TorrentEngine.move_storage`` only forwards to the libtorrent handle
        and returns ``None`` unconditionally; the DB record is written by
        ``DownloadManager.move_download`` after the on-disk move succeeds. So
        with no handle the honest assertion is "nothing happened", not a
        two-branch tautology that is true either way.
        """
        target = Path(self.tmp.name) / "moved"
        target.mkdir()
        before = self._snapshot()
        self.assertNotIn("t1", self.engine._handles, "precondition: no live handle")
        moved = self.engine.move_storage("t1", str(target))
        self.assertFalse(
            moved, "no handle -> move must not report success (got %r)" % (moved,)
        )
        self.assertEqual(
            self._snapshot(), before,
            "the DB row must survive a move that could not be performed",
        )

    def test_move_storage_with_handle_forwards_the_new_path(self):
        """With a live handle the new path reaches libtorrent verbatim."""
        target = Path(self.tmp.name) / "moved"
        target.mkdir()
        handle = MagicMock()
        self.engine._handles["t1"] = handle
        before = self._snapshot()
        self.engine.move_storage("t1", str(target))
        handle.move_storage.assert_called_once_with(str(target))
        self.assertEqual(
            self._snapshot(), before,
            "move_storage only talks to libtorrent; the DB row is the manager's job",
        )

    def test_move_storage_swallows_a_failing_handle(self):
        """A libtorrent error is logged, never raised, and never touches the DB."""
        target = Path(self.tmp.name) / "moved"
        target.mkdir()
        handle = MagicMock()
        handle.move_storage.side_effect = RuntimeError("libtorrent said no")
        self.engine._handles["t1"] = handle
        before = self._snapshot()
        try:
            self.engine.move_storage("t1", str(target))
        except RuntimeError as exc:  # pragma: no cover - the point of the test
            self.fail(f"move_storage must swallow engine errors, got {exc!r}")
        handle.move_storage.assert_called_once_with(str(target))
        self.assertEqual(self._snapshot(), before)

    def test_set_session_limits_without_a_session_is_safe(self):
        before = self._snapshot()
        self.engine.set_session_limits(10, 5)
        # No libtorrent session, but the NetworkConfig mirror is still updated.
        net_cfg = self.engine._network_config
        if net_cfg is None:
            self.assertIsNone(
                self.engine._session,
                "with no session configured there is nothing to rate-limit",
            )
        else:
            self.assertEqual(net_cfg.download_limit, 10)
            self.assertEqual(net_cfg.upload_limit, 5)
        self.assertEqual(self._snapshot(), before, "session limits are not per-download")

    def test_set_session_limits_mirrors_into_network_config(self):
        from my_idm.network import NetworkConfig

        self.engine._network_config = NetworkConfig()
        self.engine.set_session_limits(1024, 512)
        self.assertEqual(self.engine._network_config.download_limit, 1024)
        self.assertEqual(self.engine._network_config.upload_limit, 512)

    def test_is_torrent_tor_routed_defaults_false(self):
        self.assertFalse(self.engine.is_torrent_tor_routed("t1"))
        self.assertFalse(self.engine.is_torrent_tor_routed("does-not-exist"))

    def test_set_torrent_tor_route_persists(self):
        self.engine.set_torrent_tor_route("t1", True)
        self.assertTrue(self.engine.is_torrent_tor_routed("t1"))
        self.assertTrue(self.db.get_download("t1").metadata.get("route_through_tor"))
        # Setting the same value again is a no-op, not a redundant write.
        self.engine.set_torrent_tor_route("t1", True)
        self.assertTrue(self.db.get_download("t1").metadata.get("route_through_tor"))
        self.engine.set_torrent_tor_route("t1", False)
        self.assertFalse(self.engine.is_torrent_tor_routed("t1"))


class TestNoShadowedDefinitions(unittest.TestCase):
    """A definition repeated in one scope silently replaces the first one.

    ``StatisticsPopup._populate_grid`` was defined twice by an editing slip. The two
    bodies happened to be equivalent, so nothing misbehaved and no test failed - but the
    class is exactly where a stale first copy hides a real difference, and the only signal
    is a future edit touching the *second* copy and wondering why the first still exists.

    Scope matters: ``__init__`` and ``paintEvent`` legitimately repeat across classes, and
    a property plus its ``@name.setter`` is one logical definition, not a collision.
    """

    MODULES = (
        "my_idm.stats_dialog",
        "my_idm.settings_dialog",
        "my_idm.main_window",
        "my_idm.database",
        "my_idm.download_model",
        "my_idm.manager",
        "my_idm.http_engine",
        "my_idm.torrent_engine",
        "my_idm.utils",
        "my_idm.config",
        "my_idm.browser_server",
        "my_idm.details_panel",
    )

    @staticmethod
    def _is_setter(node) -> bool:
        """True for ``@name.setter``, which extends a property rather than shadowing it."""
        for deco in node.decorator_list:
            if (
                isinstance(deco, ast.Attribute)
                and deco.attr == "setter"
                and getattr(deco.value, "id", None) == node.name
            ):
                return True
        return False

    def _duplicates(self, body) -> list[str]:
        counts: dict[str, int] = {}
        for child in body:
            if not isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            if isinstance(child, ast.FunctionDef) and self._is_setter(child):
                continue
            counts[child.name] = counts.get(child.name, 0) + 1
        return sorted(name for name, count in counts.items() if count > 1)

    def test_no_module_rebinds_a_name_in_the_same_scope(self):
        import importlib

        for name in dict.fromkeys(self.MODULES):
            module = importlib.import_module(name)
            with self.subTest(module=name):
                tree = ast.parse(inspect.getsource(module))
                offenders = {}
                module_level = self._duplicates(tree.body)
                if module_level:
                    offenders["module scope"] = module_level
                for node in tree.body:
                    if isinstance(node, ast.ClassDef):
                        found = self._duplicates(node.body)
                        if found:
                            offenders[node.name] = found
                self.assertEqual(
                    offenders, {},
                    "a redefinition shadows the first definition silently",
                )

    def test_the_statistics_grid_is_defined_exactly_once(self):
        """The specific instance that motivated the check."""
        from my_idm.stats_dialog import StatisticsPopup

        self.assertEqual(
            inspect.getsource(StatisticsPopup).count("def _populate_grid"), 1
        )


class TestSanityTierIsNonInteractive(unittest.TestCase):
    """The basic sanity tier must not disturb the desktop it runs on.

    `pytest -m "not ui"` is meant to be safe to leave running in the background, which means
    it must not read or write the real clipboard and must not repaint the real taskbar. An
    earlier version of `isolate_system_clipboard` snapshotted, cleared and restored the OS
    clipboard on *every* one of 2400+ tests, which is exactly the interference this tier
    exists to avoid - so the invariant is pinned here rather than left to a code comment.
    """

    def test_a_non_ui_test_sees_a_fake_clipboard_not_the_operating_system_one(self):
        from PySide6.QtGui import QGuiApplication
        from tests import conftest

        clipboard = QGuiApplication.clipboard()
        self.assertIsInstance(clipboard, conftest._FakeClipboard)
        # And the fake is a working clipboard, so a round-trip inside one test still holds.
        clipboard.setText("https://example.com/a.zip")
        self.assertEqual(clipboard.text(), "https://example.com/a.zip")
        self.assertEqual(clipboard.text(), "https://example.com/a.zip")

    def test_the_ui_marker_is_registered(self):
        # An unregistered marker makes `-m ui` emit a warning and silently select everything,
        # which would quietly turn the two tiers back into one.
        import tomllib
        from pathlib import Path as _Path

        pyproject = _Path(__file__).resolve().parent.parent / "pyproject.toml"
        with open(pyproject, "rb") as handle:
            config = tomllib.load(handle)
        markers = config["tool"]["pytest"]["ini_options"]["markers"]
        self.assertTrue(
            any(marker.startswith("ui:") for marker in markers),
            f"the 'ui' marker is not registered: {markers}",
        )

    def test_every_declared_interactive_module_actually_exists(self):
        # A typo in the name would leave a real UI module unmarked, which is the failure that
        # matters: it puts window-popping tests back into the unattended tier.
        from tests import conftest

        tests_dir = Path(__file__).resolve().parent
        for name in conftest._INTERACTIVE_MODULES:
            with self.subTest(module=name):
                self.assertTrue(
                    (tests_dir / f"{name}.py").is_file(),
                    f"{name} is listed as interactive but tests/{name}.py does not exist",
                )

    def test_the_interactive_modules_are_not_vacuous(self):
        # The other direction: a name that matches nothing would be a silently dead entry.
        from tests import conftest

        self.assertGreaterEqual(len(conftest._INTERACTIVE_MODULES), 8)

    def test_the_taskbar_repaint_is_gated_on_ui_tests_being_present(self):
        # `_cleanup_windows_tray_ghosts` posts WM_MOUSEMOVE across Shell_TrayWnd, which makes
        # a real taskbar flicker. It must not run for the basic tier.
        from tests import conftest

        source = inspect.getsource(conftest.suppress_system_tray_notifications)
        self.assertIn("if _HAS_UI_TESTS:", source)
        # ...and the gate must come *before* the call, not after it.
        self.assertLess(
            source.index("if _HAS_UI_TESTS:"),
            source.index("_cleanup_windows_tray_ghosts()"),
        )


if __name__ == "__main__":
    unittest.main()
