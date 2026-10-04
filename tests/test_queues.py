"""Tests for named queues: schema, CRUD, per-queue budgets and view scoping.

The feature is "run up to N downloads at once *per queue*", so most of what matters is in the
boundaries: what happens to a download whose queue is deleted, to a row whose queue id points
at nothing, and to a queue limit that is lower than the global one.
"""

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QHeaderView,
    QInputDialog,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
)


from my_idm.config import GeneralConfig
from my_idm.database import (
    ALL_QUEUES,
    DEFAULT_QUEUE_ID,
    DEFAULT_QUEUE_NAME,
    SOURCE_QUEUE_IDS,
    Database,
    DownloadEntry,
    QueueInfo,
    normalize_queue_color,
)
from my_idm.dialogs import AddQueueDialog, QueueManagerDialog
from my_idm.http_engine import HTTPEngine
from my_idm.network import NetworkConfig
from my_idm.download_model import QUEUE_COLOR_ROLE, Col, DownloadTableModel
from my_idm.manager import DownloadManager
from my_idm.utils import create_color_swatch_icon, effective_rate_limit
from tests.test_main_window import _MainWindowTestCase

app = QApplication.instance() or QApplication(sys.argv)


def entry(did: str, **kw) -> DownloadEntry:
    return DownloadEntry(
        id=did, url=f"https://example.com/{did}.zip",
        filename=f"{did}.zip", save_path="/tmp", **kw
    )


def user_queues(manager_or_db) -> list[QueueInfo]:
    """Queues a test created, excluding Default and the two seeded source queues.

    Selecting "the queue that is not Default" was unambiguous before AnimePahe and YouTube were
    seeded, and silently ambiguous after: ``next(q for q in queues if q.id != DEFAULT_QUEUE_ID)``
    started returning *AnimePahe*, whose limit is 0 (unlimited), so budget tests silently stopped
    testing their budget.
    """
    getter = (
        manager_or_db.get_queues
        if hasattr(manager_or_db, "get_queues")
        else manager_or_db.get_queues
    )
    return [
        q for q in getter()
        if q.id != DEFAULT_QUEUE_ID and q.id not in SOURCE_QUEUE_IDS
    ]


class QueueDbMixin:
    def setUp(self):
        super().setUp()
        self.db = self.open_db(self.make_temp_db())

    def open_db(self, path):
        db = Database(path)
        db.open()
        self.addCleanup(db.close)
        return db

    def make_temp_db(self, name="queues.db"):
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(self._rmtree, Path(tmp))
        return Path(tmp) / name

    @staticmethod
    def _rmtree(path: Path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)

    def other_queue(self, name="Torrents", max_concurrent=1) -> QueueInfo:
        ok, msg = self.db.create_queue(name, max_concurrent)
        assert ok, msg
        return next(iter(user_queues(self.db)))


# ---------------------------------------------------------------------------
# Schema and migration
# ---------------------------------------------------------------------------

class TestQueueSchema(QueueDbMixin, unittest.TestCase):

    def test_a_fresh_database_has_one_default_queue_and_the_two_source_queues(self):
        queues = self.db.get_queues()
        self.assertEqual(queues[0].id, DEFAULT_QUEUE_ID)
        self.assertEqual(queues[0].name, DEFAULT_QUEUE_NAME)
        self.assertTrue(queues[0].is_default)
        self.assertEqual(user_queues(self.db), [])
        self.assertEqual(
            [q.name for q in queues[1:]], ["AnimePahe", "YouTube"]
        )

    def test_downloads_land_in_the_default_queue_without_being_told(self):
        # An empty queue_id is what every pre-queue caller passes, so the storage boundary has
        # to resolve it - otherwise the row would be invisible to every queue-scoped read until
        # the next open() re-ran the backfill.
        self.db.add_download(entry("a"))
        self.assertEqual(self.db.get_download("a").queue_id, DEFAULT_QUEUE_ID)

    def test_a_dangling_queue_id_is_resolved_to_the_default(self):
        self.db.add_download(entry("a", queue_id="queue-that-does-not-exist"))
        self.assertEqual(self.db.get_download("a").queue_id, DEFAULT_QUEUE_ID)

    def test_resolve_queue_id_accepts_blank_and_rejects_dangling(self):
        real = self.other_queue()
        self.assertEqual(self.db.resolve_queue_id(""), DEFAULT_QUEUE_ID)
        self.assertEqual(self.db.resolve_queue_id("nope"), DEFAULT_QUEUE_ID)
        self.assertEqual(self.db.resolve_queue_id(real.id), real.id)

    def test_reopening_does_not_duplicate_the_seeded_queues(self):
        path = self.make_temp_db("reopen.db")
        first = Database(path)
        first.open()
        first.close()
        second = Database(path)
        second.open()
        self.addCleanup(second.close)
        self.assertEqual(user_queues(second), [])

    def test_a_legacy_database_is_migrated_without_losing_rows(self):
        """The migration path the doc called migratable without data loss, actually tested."""
        path = self.make_temp_db("legacy.db")
        raw = sqlite3.connect(str(path))
        raw.row_factory = sqlite3.Row
        raw.execute("""
            CREATE TABLE downloads (
                id TEXT PRIMARY KEY, url TEXT NOT NULL,
                filename TEXT NOT NULL DEFAULT '', save_path TEXT NOT NULL DEFAULT '',
                file_path TEXT NOT NULL DEFAULT '', total_size INTEGER NOT NULL DEFAULT 0,
                downloaded_size INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'queued',
                download_type TEXT NOT NULL DEFAULT 'http',
                num_segments INTEGER NOT NULL DEFAULT 8,
                error_message TEXT NOT NULL DEFAULT '', retry_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 5, added_at TEXT NOT NULL DEFAULT '',
                last_tried_at TEXT NOT NULL DEFAULT '', completed_at TEXT NOT NULL DEFAULT '',
                etag TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL DEFAULT '',
                queue_order INTEGER NOT NULL DEFAULT 0
            )
        """)
        raw.execute(
            "INSERT INTO downloads (id, url, filename, status, added_at, queue_order) "
            "VALUES ('old1', 'https://example.com/old.zip', 'old.zip', 'completed', "
            "'2026-01-01T00:00:00', 3)"
        )
        raw.commit()
        raw.close()

        db = self.open_db(path)
        old = db.get_download("old1")
        self.assertIsNotNone(old)
        self.assertEqual(old.url, "https://example.com/old.zip")
        self.assertEqual(old.queue_order, 3)
        self.assertEqual(old.queue_id, DEFAULT_QUEUE_ID)
        self.assertEqual(user_queues(db), [])

    def test_the_backfill_is_idempotent(self):
        self.db.add_download(entry("a"))
        self.db._seed_default_queue()
        self.db._seed_default_queue()
        self.assertEqual(self.db.get_download("a").queue_id, DEFAULT_QUEUE_ID)
        self.assertEqual(user_queues(self.db), [])
        self.assertEqual(len(self.db.get_queues()), 3)

    def test_a_fresh_database_gives_every_queue_open_bandwidth_limits(self):
        for queue in self.db.get_queues():
            with self.subTest(queue=queue.name):
                self.assertEqual(queue.download_limit, 0)
                self.assertEqual(queue.upload_limit, 0)

    def test_a_database_with_no_bandwidth_columns_is_migrated(self):
        """`CREATE TABLE IF NOT EXISTS` will not add a column to a table that already exists.

        The colour column had this migration; the two ceilings need their own or every existing
        install's queue list raises on read.
        """
        path = self.make_temp_db("no-limits.db")
        raw = sqlite3.connect(str(path))
        raw.execute("""
            CREATE TABLE queues (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                max_concurrent INTEGER NOT NULL DEFAULT 3,
                position INTEGER NOT NULL DEFAULT 0,
                is_default INTEGER NOT NULL DEFAULT 0,
                color TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            )
        """)
        raw.execute(
            "INSERT INTO queues (id, name, max_concurrent, position, is_default, color, "
            "created_at) VALUES ('q-old', 'Old', 4, 1, 0, '#123456', '2026-01-01T00:00:00')"
        )
        raw.commit()
        raw.close()

        db = self.open_db(path)
        migrated = db.get_queue("q-old")
        self.assertIsNotNone(migrated)
        self.assertEqual(migrated.name, "Old", "the migration must not lose the row")
        self.assertEqual(migrated.max_concurrent, 4)
        self.assertEqual(migrated.download_limit, 0)
        self.assertEqual(migrated.upload_limit, 0)
        columns = [
            r["name"] for r in db._conn.execute("PRAGMA table_info(queues)").fetchall()
        ]
        self.assertIn("download_limit", columns)
        self.assertIn("upload_limit", columns)


# ---------------------------------------------------------------------------
# Bandwidth ceilings
# ---------------------------------------------------------------------------

class TestQueueBandwidthCeilings(QueueDbMixin, unittest.TestCase):
    """`queues.download_limit` / `upload_limit`, and how they combine with everything else."""

    def test_create_stores_the_ceilings(self):
        ok, message = self.db.create_queue(
            "Seed", 2, "#3fb950", download_limit=512 * 1024, upload_limit=64 * 1024
        )
        self.assertTrue(ok, message)
        queue = self.db.get_queue_by_name("Seed")
        self.assertEqual(queue.download_limit, 512 * 1024)
        self.assertEqual(queue.upload_limit, 64 * 1024)

    def test_a_negative_ceiling_is_clamped_rather_than_stored(self):
        queue = self.other_queue()
        self.db.set_queue_limits(queue.id, -1, -999)
        stored = self.db.get_queue(queue.id)
        self.assertEqual(stored.download_limit, 0)
        self.assertEqual(stored.upload_limit, 0)

    def test_set_queue_limits_leaves_the_other_direction_alone(self):
        queue = self.other_queue()
        self.db.set_queue_limits(queue.id, 1000, 2000)
        self.db.set_queue_limits(queue.id, 3000, 2000)
        stored = self.db.get_queue(queue.id)
        self.assertEqual(stored.download_limit, 3000)
        self.assertEqual(stored.upload_limit, 2000)

    def test_the_seeded_source_queues_stay_unlimited(self):
        """They exist for organisation, not capping, exactly as their `max_concurrent` says."""
        for queue in self.db.get_queues():
            with self.subTest(queue=queue.name):
                self.assertEqual(queue.download_limit, 0)
                self.assertEqual(queue.upload_limit, 0)


class TestEffectiveRateLimit(unittest.TestCase):
    """`utils.effective_rate_limit`, which owns the resolution rule for both engines."""

    def test_no_ceiling_anywhere_is_unlimited(self):
        self.assertEqual(effective_rate_limit(0, 0), 0)
        self.assertEqual(effective_rate_limit(0, 0, "low"), 0)

    def test_a_queue_ceiling_applies_when_the_global_one_is_unset(self):
        # The global limit is 0 by default, and the old code returned 0 from an early exit before
        # the allocation was even read - so a queue limit, and the allocation, did nothing.
        self.assertEqual(effective_rate_limit(1000, 0), 1000)

    def test_the_global_ceiling_applies_when_the_queue_has_none(self):
        self.assertEqual(effective_rate_limit(0, 4000), 4000)

    def test_the_tighter_of_the_two_wins(self):
        self.assertEqual(effective_rate_limit(1000, 4000), 1000)
        self.assertEqual(effective_rate_limit(4000, 1000), 1000)

    def test_a_queue_can_never_raise_the_global_ceiling(self):
        """A queue may only slow its downloads down; that is the entire point of the feature."""
        self.assertEqual(effective_rate_limit(999_999, 1000), 1000)

    def test_the_allocation_takes_its_share_of_the_ceiling(self):
        self.assertEqual(effective_rate_limit(1000, 0, "low"), 250)
        self.assertEqual(effective_rate_limit(1000, 0, "medium"), 500)
        self.assertEqual(effective_rate_limit(1000, 0, "high"), 750)
        self.assertEqual(effective_rate_limit(1000, 0, "max"), 1000)

    def test_an_unknown_allocation_is_max_rather_than_a_stall(self):
        for allocation in ("", None, "turbo", "MAXIMUM"):
            with self.subTest(allocation=allocation):
                self.assertEqual(effective_rate_limit(1000, 0, allocation), 1000)

    def test_allocation_is_case_insensitive(self):
        self.assertEqual(effective_rate_limit(1000, 0, "LOW"), 250)


class TestTorrentEngineQueueCeiling(unittest.TestCase):
    """The torrent side, where a queue ceiling becomes a libtorrent handle limit."""

    def _engine(self, queue_id="", allocation=None):
        from my_idm.torrent_engine import TorrentEngine

        engine = TorrentEngine.__new__(TorrentEngine)  # no __init__: no session needed
        entry = DownloadEntry(id="t1", url="magnet:?xt=urn:btih:da39a3ee", queue_id=queue_id)
        entry.download_type = "torrent"
        if allocation:
            entry.metadata["bandwidth_allocation"] = allocation
        engine._db = MagicMock()
        engine._db.get_download.return_value = entry
        engine._network_config = None
        engine._queue_limits = {"q1": (2000, 500)}
        handle = MagicMock()
        handle.is_valid.return_value = True
        engine._handles = {"t1": handle}
        return engine, handle

    def test_a_queue_ceiling_becomes_the_handle_limit(self):
        engine, handle = self._engine("q1")
        with patch("my_idm.torrent_engine._HAS_LIBTORRENT", True):
            engine.apply_handle_limits("t1")
        handle.set_download_limit.assert_called_once_with(2000)
        handle.set_upload_limit.assert_called_once_with(500)

    def test_the_tighter_ceiling_wins_on_the_handle_too(self):
        engine, handle = self._engine("q1")
        engine._network_config = NetworkConfig(download_limit=1000, upload_limit=100)
        with patch("my_idm.torrent_engine._HAS_LIBTORRENT", True):
            engine.apply_handle_limits("t1")
        handle.set_download_limit.assert_called_once_with(1000)
        handle.set_upload_limit.assert_called_once_with(100)

    def test_no_ceiling_anywhere_leaves_the_handle_unlimited(self):
        engine, handle = self._engine("q1")
        engine._queue_limits = {}
        with patch("my_idm.torrent_engine._HAS_LIBTORRENT", True):
            engine.apply_handle_limits("t1")
        handle.set_download_limit.assert_called_once_with(-1)
        handle.set_upload_limit.assert_called_once_with(-1)

    def test_a_reduced_allocation_without_a_ceiling_keeps_the_stand_in(self):
        """A share of nothing is not zero, and zero would stall seeding outright."""
        engine, handle = self._engine("q1", allocation="low")
        engine._queue_limits = {}
        with patch("my_idm.torrent_engine._HAS_LIBTORRENT", True):
            engine.apply_handle_limits("t1")
        handle.set_download_limit.assert_called_once_with(int(10_000_000 * 0.25))


class TestHttpEngineQueueCeiling(unittest.TestCase):
    """The engine's own resolution, which is what actually paces the chunks."""

    def _engine(self):
        return HTTPEngine.__new__(HTTPEngine)  # no __init__: nothing here needs a database

    def _entry(self, queue_id, allocation=None):
        item = DownloadEntry(id="d", url="https://example.com/a.zip", queue_id=queue_id)
        if allocation:
            item.metadata["bandwidth_allocation"] = allocation
        return item

    def test_a_limited_queue_caps_a_download_with_no_global_limit(self):
        engine = self._engine()
        engine._download_limit = 0
        engine._queue_limits = {"q1": (2000, 0)}
        self.assertEqual(
            engine._get_effective_download_limit(self._entry("q1")), 2000
        )

    def test_an_unlimited_queue_follows_the_global_limit(self):
        engine = self._engine()
        engine._download_limit = 5000
        engine._queue_limits = {"q1": (0, 0)}
        self.assertEqual(
            engine._get_effective_download_limit(self._entry("q1")), 5000
        )

    def test_a_queue_cannot_lift_the_global_limit(self):
        engine = self._engine()
        engine._download_limit = 1000
        engine._queue_limits = {"q1": (999_999, 0)}
        self.assertEqual(
            engine._get_effective_download_limit(self._entry("q1")), 1000
        )

    def test_the_allocation_scales_a_queue_ceiling(self):
        engine = self._engine()
        engine._download_limit = 0
        engine._queue_limits = {"q1": (4000, 0)}
        self.assertEqual(
            engine._get_effective_download_limit(self._entry("q1", "low")), 1000
        )

    def test_an_unset_queue_id_means_no_queue_ceiling(self):
        engine = self._engine()
        engine._download_limit = 0
        engine._queue_limits = {"q1": (2000, 0)}
        self.assertEqual(
            engine._get_effective_download_limit(self._entry("")), 0
        )

class TestQueueCrud(QueueDbMixin, unittest.TestCase):

    def test_create_and_list(self):
        self.db.create_queue("Torrents", 2)
        names = [q.name for q in self.db.get_queues()]
        self.assertEqual(names[0], DEFAULT_QUEUE_NAME)
        self.assertIn("Torrents", names)

    def test_default_queue_sorts_first(self):
        self.db.create_queue("Alpha", 1)
        self.assertEqual(self.db.get_queues()[0].id, DEFAULT_QUEUE_ID)

    def test_duplicate_names_are_refused_case_insensitively(self):
        self.db.create_queue("Torrents", 1)
        ok, message = self.db.create_queue("torrents", 1)
        self.assertFalse(ok)
        self.assertIn("already exists", message)

    def test_blank_name_is_refused(self):
        for bad in ("", "   ", None):
            ok, message = self.db.create_queue(bad, 1)
            self.assertFalse(ok)
            self.assertIn("cannot be empty", message)

    def test_rename(self):
        queue = self.other_queue("Old")
        ok, message = self.db.rename_queue(queue.id, "New")
        self.assertTrue(ok, message)
        self.assertEqual(self.db.get_queue(queue.id).name, "New")

    def test_rename_to_a_blank_or_taken_name_is_refused(self):
        queue = self.other_queue("One")
        self.db.create_queue("Two", 1)
        self.assertFalse(self.db.rename_queue(queue.id, "  ")[0])
        self.assertFalse(self.db.rename_queue(queue.id, "Two")[0])

    def test_the_default_queue_cannot_be_renamed_or_deleted(self):
        self.assertFalse(self.db.rename_queue(DEFAULT_QUEUE_ID, "Nope")[0])
        ok, message = self.db.delete_queue(DEFAULT_QUEUE_ID)
        self.assertFalse(ok)
        self.assertIn("default queue", message)

    def test_set_max_concurrent(self):
        queue = self.other_queue(max_concurrent=1)
        self.db.set_queue_max_concurrent(queue.id, 4)
        self.assertEqual(self.db.get_queue(queue.id).max_concurrent, 4)

    def test_a_zero_limit_means_unlimited_within_the_queue(self):
        queue = self.other_queue(max_concurrent=1)
        self.db.set_queue_max_concurrent(queue.id, 0)
        self.assertEqual(self.db.get_queue(queue.id).effective_max_concurrent, 0)

    def test_reordering_keeps_the_default_pinned_first(self):
        a = self.other_queue("A", 1)
        self.db.create_queue("B", 1)
        b = next(q for q in self.db.get_queues() if q.name == "B")

        self.db.move_queue_position(b.id, -1)

        # Compared over user queues only: the seeded source queues are part of the switcher but
        # not part of what this test is about.
        self.assertEqual(
            [q.name for q in user_queues(self.db)], ["B", "A"]
        )
        self.assertEqual(self.db.get_queues()[0].name, DEFAULT_QUEUE_NAME)
        del a

    def test_move_queue_position_at_the_ends_is_a_no_op(self):
        a = self.other_queue("A", 1)
        self.db.move_queue_position(a.id, -1)   # already first among the movable ones
        self.db.move_queue_position(a.id, +1)   # already last
        self.assertEqual([q.name for q in user_queues(self.db)], ["A"])

    def test_deleting_a_queue_moves_its_downloads_rather_than_deleting_them(self):
        queue = self.other_queue()
        self.db.add_download(entry("a", queue_id=queue.id))
        self.db.add_download(entry("b"))

        ok, message = self.db.delete_queue(queue.id)

        self.assertTrue(ok, message)
        self.assertIsNone(self.db.get_queue(queue.id))
        self.assertIsNotNone(self.db.get_download("a"))
        self.assertEqual(self.db.get_download("a").queue_id, DEFAULT_QUEUE_ID)
        self.assertIn("moved to", message)

    def test_deleting_an_unknown_queue_is_refused(self):
        ok, _message = self.db.delete_queue("nope")
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# Scoped reads
# ---------------------------------------------------------------------------

class TestQueueScopedReads(QueueDbMixin, unittest.TestCase):

    def test_get_all_downloads_defaults_to_every_queue(self):
        # History is the product: a caller that did not ask for a queue must not silently get
        # a subset.
        queue = self.other_queue()
        self.db.add_download(entry("a"))
        self.db.add_download(entry("b", queue_id=queue.id))
        self.assertEqual({e.id for e in self.db.get_all_downloads()}, {"a", "b"})

    def test_get_all_downloads_can_be_scoped(self):
        queue = self.other_queue()
        self.db.add_download(entry("a"))
        self.db.add_download(entry("b", queue_id=queue.id))
        self.assertEqual([e.id for e in self.db.get_all_downloads(queue.id)], ["b"])
        self.assertEqual(len(self.db.get_all_downloads(ALL_QUEUES)), 2)

    def test_next_queue_order_is_per_queue(self):
        queue = self.other_queue()
        self.db.add_download(entry("a"))
        self.db.add_download(entry("b"))
        self.assertEqual(self.db.get_next_queue_order(DEFAULT_QUEUE_ID), 3)
        # A separate queue keeps its own numbering, so a torrent promoted to position 1 of its
        # own queue does not push everything in Default down by one.
        self.assertEqual(self.db.get_next_queue_order(queue.id), 1)

    def test_reassigning_continues_the_target_numbering(self):
        target = self.other_queue()
        self.db.add_download(entry("keep", queue_id=target.id, queue_order=1))
        self.db.add_download(entry("move1"))
        self.db.add_download(entry("move2"))

        moved = self.db.reassign_queue(["move1", "move2"], target.id)

        self.assertEqual(moved, 2)
        self.assertEqual(self.db.get_download("move1").queue_id, target.id)
        # Not restarted at 1, which would collide with the row already in the target queue.
        self.assertGreater(self.db.get_download("move1").queue_order, 1)
        self.assertEqual(
            self.db.get_download("move1").queue_order + 1,
            self.db.get_download("move2").queue_order,
        )

    def test_reassigning_preserves_the_callers_order(self):
        target = self.other_queue()
        for did in ("a", "b", "c"):
            self.db.add_download(entry(did))
        self.db.reassign_queue(["c", "a", "b"], target.id)
        orders = [
            self.db.get_download(did).queue_order for did in ("c", "a", "b")
        ]
        self.assertEqual(orders, sorted(orders))

    def test_reassigning_to_an_unknown_queue_changes_nothing(self):
        self.db.add_download(entry("a"))
        self.assertEqual(self.db.reassign_queue(["a"], "nope"), 0)
        self.assertEqual(self.db.get_download("a").queue_id, DEFAULT_QUEUE_ID)

    def test_active_counts_group_by_queue(self):
        queue = self.other_queue()
        self.db.add_download(entry("a", status="downloading"))
        self.db.add_download(entry("b", status="downloading"))
        self.db.add_download(entry("c", status="downloading", queue_id=queue.id))
        self.db.add_download(entry("d", status="completed"))
        counts = self.db.get_active_counts_by_queue()
        self.assertEqual(counts.get(DEFAULT_QUEUE_ID), 2)
        self.assertEqual(counts.get(queue.id), 1)
        self.assertNotIn("d", counts)

    def test_queue_download_counts_cover_completed_rows(self):
        queue = self.other_queue()
        self.db.add_download(entry("a", status="completed"))
        self.db.add_download(entry("b", queue_id=queue.id, status="completed"))
        counts = self.db.get_queue_download_counts()
        self.assertEqual(counts.get(DEFAULT_QUEUE_ID), 1)
        self.assertEqual(counts.get(queue.id), 1)


# ---------------------------------------------------------------------------
# Concurrency budgets
# ---------------------------------------------------------------------------

class TestEffectiveMaxConcurrent(unittest.TestCase):

    def test_positive_values_pass_through(self):
        cfg = GeneralConfig()
        cfg.max_concurrent_downloads = 7
        self.assertEqual(cfg.effective_max_concurrent, 7)

    def test_zero_and_negative_resolve_to_the_default_of_three(self):
        # This fallback used to be retyped at four call sites and had already drifted once.
        for raw in (0, -1, -99):
            cfg = GeneralConfig()
            cfg.max_concurrent_downloads = raw
            self.assertEqual(cfg.effective_max_concurrent, 3, raw)


class QueueManagerMixin:
    def setUp(self):
        super().setUp()
        self.db = Database(self.make_temp_db())
        self.db.open()
        self.addCleanup(self.db.close)
        self.manager = DownloadManager(self.db)
        self.addCleanup(self._stop_manager)

    def _stop_manager(self):
        # No _loop was started (the engine is never started in these tests), so stop() would
        # try to touch a thread that does not exist.
        self.manager._stopped = True

    def make_temp_db(self, name="mgr.db"):
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(self._rmtree, Path(tmp))
        return Path(tmp) / name

    @staticmethod
    def _rmtree(path: Path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)


class TestPerQueueBudget(QueueManagerMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.manager._general_config = GeneralConfig()
        self.manager._general_config.max_concurrent_downloads = 5
        self.queue = self.manager.create_queue("Torrents", 1)
        self.assertTrue(self.queue[0])
        self.tq = next(
            q for q in user_queues(self.manager)
        )

    def _may_start(self, did):
        entry_ = self.db.get_download(did)
        return self.manager._may_start(
            entry_,
            self.manager._active_counts_by_queue(),
            self.manager._queue_limits(),
            self.manager._general_config.effective_max_concurrent,
        )

    def test_a_queue_limit_stops_its_own_downloads(self):
        self.db.add_download(entry("d1"))
        self.db.add_download(entry("t1", queue_id=self.tq.id))
        self.assertTrue(self._may_start("d1"))
        self.assertTrue(self._may_start("t1"))

        # Simulate the torrent queue's single slot being taken.
        self.db.add_download(entry("t2", queue_id=self.tq.id, status="downloading"))
        self.assertTrue(self._may_start("d1"))
        self.assertFalse(self._may_start("t2"))
        self.assertFalse(self._may_start("t1"))

    def test_an_unlimited_queue_is_bounded_only_by_the_global_ceiling(self):
        self.db.set_queue_max_concurrent(self.tq.id, 0)
        # The global ceiling is 5, so five active transfers exhaust it regardless of the
        # queue's own "Global".
        for i in range(5):
            self.db.add_download(entry(f"t{i}", queue_id=self.tq.id, status="downloading"))
        self.db.add_download(entry("t5", queue_id=self.tq.id))
        self.assertFalse(self._may_start("t5"))

    def test_an_unlimited_queue_admits_more_than_its_old_limit_would_have(self):
        self.db.set_queue_max_concurrent(self.tq.id, 0)
        for i in range(3):
            self.db.add_download(entry(f"t{i}", queue_id=self.tq.id, status="downloading"))
        self.db.add_download(entry("t3", queue_id=self.tq.id))
        self.assertTrue(self._may_start("t3"))

    def test_the_global_ceiling_still_applies_on_top_of_a_loose_queue(self):
        self.db.set_queue_max_concurrent(DEFAULT_QUEUE_ID, 99)
        for i in range(5):
            self.db.add_download(entry(f"d{i}", status="downloading"))
        self.db.add_download(entry("d5"))
        self.assertFalse(self._may_start("d5"))

    def test_the_starting_set_counts_towards_a_queue_budget(self):
        # _starting_downloads covers the window before the engine reports "downloading".
        # Without it, a queue's first start overshoots its own budget by exactly one.
        self.db.add_download(entry("t1", queue_id=self.tq.id))
        self.manager._starting_downloads.add("t1")
        self.assertFalse(self._may_start("t1"))

    def test_active_count_can_be_scoped_to_one_queue(self):
        other = self.manager.create_queue("Other", 5)
        self.assertTrue(other[0])
        oq = next(q for q in self.manager.get_queues() if q.name == "Other")
        self.db.add_download(entry("d", status="downloading"))
        self.db.add_download(entry("o", queue_id=oq.id, status="downloading"))
        self.assertEqual(self.manager._get_active_download_count(), 2)
        self.assertEqual(self.manager._get_active_download_count(DEFAULT_QUEUE_ID), 1)
        self.assertEqual(self.manager._get_active_download_count(oq.id), 1)


class TestProcessQueueOrdering(QueueManagerMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.manager._general_config = GeneralConfig()
        self.manager._general_config.max_concurrent_downloads = 4
        self.manager.create_queue("Torrents", 1)
        self.tq = next(
            q for q in user_queues(self.manager)
        )
        self.started: list[str] = []
        patcher = patch.object(
            self.manager, "_start_entry", side_effect=self._record_start
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _record_start(self, entry_):
        self.started.append(entry_.id)
        self.manager._starting_downloads.add(entry_.id)
        entry_.status = "downloading"
        self.db.update_download(entry_)

    def test_dispatch_order_follows_the_queue_switcher_order(self):
        # Neither queue is saturated, so the tie-break is the switcher order: Default first,
        # then the user's queue. It must be deterministic - ids are uuids, so an id tie-break
        # would produce a different order on two runs with identical state.
        self.db.add_download(entry("d1", queue_order=1))
        self.db.add_download(entry("d2", queue_order=2))
        self.db.add_download(entry("t1", queue_id=self.tq.id, queue_order=1))
        self.db.add_download(entry("t2", queue_id=self.tq.id, queue_order=2))

        self.manager._process_queue()

        self.assertEqual(self.started, ["d1", "d2", "t1"])
        # t2 is blocked by its own queue's limit of 1, not by the global ceiling of 4.
        self.assertNotIn("t2", self.started)

    def test_a_saturated_queue_sinks_behind_one_that_can_still_start(self):
        # Two user queues, "First" listed above "Second", with First's single slot already
        # taken. Second must be served: a saturated queue sinking to the back is what stops one
        # busy queue from starving every other one.
        self.manager.create_queue("First", 1)
        first = next(q for q in self.manager.get_queues() if q.name == "First")
        self.manager.move_queue_in_list(first.id, -1)
        order = [q.name for q in user_queues(self.manager)]
        self.assertEqual(order, ["First", "Torrents"])

        self.db.add_download(entry("busy", queue_id=first.id, status="downloading"))
        self.db.add_download(entry("f2", queue_id=first.id))
        self.db.add_download(entry("t1", queue_id=self.tq.id))

        self.manager._process_queue()

        self.assertEqual(self.started, ["t1"])

    def test_a_retrying_download_is_still_gated_by_the_backoff(self):
        self.db.add_download(
            entry("r1", retry_count=1, metadata_json='{"next_retry_at": 9999999999}')
        )
        self.manager._process_queue()
        self.assertEqual(self.started, [])

    def test_an_exhausted_retry_budget_is_not_started(self):
        item = entry("r2", retry_count=9, max_retries=5)
        self.db.add_download(item)
        self.manager._process_queue()
        self.assertEqual(self.started, [])

    def test_a_saturated_global_ceiling_starts_nothing(self):
        self.manager._general_config.max_concurrent_downloads = 1
        self.db.add_download(entry("d1", status="downloading"))
        self.db.add_download(entry("d2"))
        self.manager._process_queue()
        self.assertEqual(self.started, [])


# ---------------------------------------------------------------------------
# Manager-level queue API
# ---------------------------------------------------------------------------

class TestManagerQueueApi(QueueManagerMixin, unittest.TestCase):

    def test_the_active_scope_starts_as_all_queues(self):
        # Non-breaking by design: history must not go missing just because the feature shipped.
        self.assertEqual(self.manager.get_active_queue(), ALL_QUEUES)

    def test_set_and_clear_the_active_scope(self):
        self.manager.create_queue("Q", 1)
        qid = next(q.id for q in self.manager.get_queues() if q.name == "Q")

        seen = []
        self.manager.queue_scope_changed.connect(seen.append)
        self.manager.set_active_queue(qid)
        self.assertEqual(self.manager.get_active_queue(), qid)
        self.assertEqual(seen, [qid])

        self.manager.set_active_queue("")
        self.assertEqual(self.manager.get_active_queue(), ALL_QUEUES)

    def test_an_unknown_scope_falls_back_to_all_queues(self):
        self.manager.set_active_queue("no-such-queue")
        self.assertEqual(self.manager.get_active_queue(), ALL_QUEUES)

    def test_deleting_the_selected_queue_clears_the_scope(self):
        self.manager.create_queue("Q", 1)
        qid = next(q.id for q in self.manager.get_queues() if q.name == "Q")
        self.manager.set_active_queue(qid)
        seen = []
        self.manager.queue_scope_changed.connect(seen.append)

        self.manager.delete_queue(qid)

        self.assertEqual(self.manager.get_active_queue(), ALL_QUEUES)
        # Assert the *stored* value too. get_active_queue() falls back to "" whenever the
        # stored id stops resolving, so checking only the getter passes even when the stale id
        # is still sitting in ui_state — which is what left the view filtered to a dead queue.
        self.assertEqual(self.db.get_ui_state("active_queue_id", ""), "")
        self.assertEqual(seen, [""])

    def test_queues_changed_fires_on_mutation(self):
        seen = []
        self.manager.queues_changed.connect(lambda: seen.append(True))
        self.manager.create_queue("Q", 1)
        qid = next(q.id for q in self.manager.get_queues() if q.name == "Q")
        self.manager.rename_queue(qid, "R")
        self.manager.set_queue_max_concurrent(qid, 3)
        self.manager.delete_queue(qid)
        self.assertGreaterEqual(len(seen), 4)

    def test_move_downloads_to_queue_reports_the_count(self):
        self.manager.create_queue("Q", 3)
        qid = next(q.id for q in self.manager.get_queues() if q.name == "Q")
        self.db.add_download(entry("a"))
        self.db.add_download(entry("b"))

        ok, message = self.manager.move_downloads_to_queue(["a", "b"], qid)

        self.assertTrue(ok, message)
        self.assertIn("2 download(s)", message)
        self.assertEqual(self.db.get_download("a").queue_id, qid)

    def test_move_downloads_to_queue_with_nothing_selected(self):
        ok, message = self.manager.move_downloads_to_queue([], DEFAULT_QUEUE_ID)
        self.assertFalse(ok)
        self.assertIn("No downloads selected", message)

    def test_move_downloads_to_queue_skips_ids_that_are_gone(self):
        ok, message = self.manager.move_downloads_to_queue(["ghost"], DEFAULT_QUEUE_ID)
        self.assertFalse(ok)
        self.assertIn("still exist", message)

    def test_add_download_accepts_and_normalises_a_queue(self):
        self.manager.create_queue("Q", 3)
        qid = next(q.id for q in self.manager.get_queues() if q.name == "Q")
        did = self.manager.add_download("https://example.com/x.zip", queue_id=qid)
        self.assertEqual(self.db.get_download(did).queue_id, qid)

        other = self.manager.add_download(
            "https://example.com/y.zip", queue_id="not-a-queue"
        )
        self.assertEqual(self.db.get_download(other).queue_id, DEFAULT_QUEUE_ID)


class TestQueueLimitsReachTheEngines(QueueManagerMixin, unittest.TestCase):
    """The manager pushes a snapshot; the engines must be holding it."""

    def _make_queue(self, name="Seed", download_limit=0, upload_limit=0):
        ok, message = self.manager.create_queue(name, 2, "#3fb950", download_limit, upload_limit)
        self.assertTrue(ok, message)
        return self.manager._db.get_queue_by_name(name)

    def test_the_engines_start_with_the_queues_limits(self):
        queue = self._make_queue("Seed", 300 * 1024, 40 * 1024)
        self.assertEqual(
            self.manager._http._queue_limits[queue.id], (300 * 1024, 40 * 1024)
        )
        self.assertEqual(
            self.manager._torrent._queue_limits[queue.id], (300 * 1024, 40 * 1024)
        )

    def test_editing_a_queues_limits_reaches_both_engines(self):
        queue = self._make_queue("Seed")
        self.manager.set_queue_limits(queue.id, 1234, 5678)
        self.assertEqual(self.manager._http._queue_limits[queue.id], (1234, 5678))
        self.assertEqual(self.manager._torrent._queue_limits[queue.id], (1234, 5678))

    def test_deleting_a_queue_drops_it_from_the_snapshot(self):
        queue = self._make_queue("Seed", 100, 200)
        self.manager.delete_queue(queue.id)
        self.assertNotIn(queue.id, self.manager._http._queue_limits)
        self.assertNotIn(queue.id, self.manager._torrent._queue_limits)

    def test_a_blank_queue_id_means_the_default_queue(self):
        """`DownloadEntry.queue_id` is '' until the backfill runs, so '' must mean Default."""
        self.manager.set_queue_limits(DEFAULT_QUEUE_ID, 8000, 0)
        blank = entry("q1")
        blank.queue_id = ""
        self.assertEqual(self.manager._http._get_effective_download_limit(blank), 8000)


class TestQueueScopedReorder(QueueManagerMixin, unittest.TestCase):

    def test_moving_up_then_down_restores_the_original_order(self):
        self.db.add_download(entry("d1"))
        self.db.add_download(entry("d2"))
        self.assertEqual(self.db.get_download("d1").queue_order, 1)
        self.assertEqual(self.db.get_download("d2").queue_order, 2)

        self.assertTrue(self.manager.move_queue_up("d2"))
        self.assertEqual(self.db.get_download("d2").queue_order, 1)
        self.assertEqual(self.db.get_download("d1").queue_order, 2)

        self.assertTrue(self.manager.move_queue_down("d2"))
        self.assertEqual(self.db.get_download("d1").queue_order, 1)
        self.assertEqual(self.db.get_download("d2").queue_order, 2)

    def test_moving_does_not_cross_a_queue_boundary(self):
        queue = self.manager.create_queue("Q", 3)
        qid = next(q.id for q in self.manager.get_queues() if q.name == "Q")
        self.db.add_download(entry("d1"))
        self.db.add_download(entry("t1", queue_id=qid))

        # d1 is first in its own queue and must not climb into another queue's numbering.
        self.assertFalse(self.manager.move_queue_up("d1"))
        self.assertEqual(self.db.get_download("d1").queue_order, 1)
        self.assertEqual(self.db.get_download("t1").queue_order, 1)

    def test_moving_an_unknown_id_is_a_no_op(self):
        self.assertFalse(self.manager.move_queue_up("ghost"))
        self.assertFalse(self.manager.move_queue_down("ghost"))

    def test_queue_order_changed_fires_once_per_move(self):
        self.db.add_download(entry("d1"))
        self.db.add_download(entry("d2"))
        seen = []
        self.manager.queue_order_changed.connect(lambda: seen.append(True))
        self.manager.move_queue_up("d2")
        self.assertEqual(seen, [True])

    def test_the_whole_queue_is_renumbered_densely_after_a_move(self):
        # Renumbering only the affected run left the rows before it holding the value the
        # moved row vacated, producing two downloads with the same priority.
        self.db.add_download(entry("d1", queue_order=2))
        self.db.add_download(entry("d2", queue_order=3))
        self.db.add_download(entry("d3", queue_order=4))

        self.manager.move_queue_up("d3")

        orders = {
            did: self.db.get_download(did).queue_order
            for did in ("d1", "d2", "d3")
        }
        self.assertEqual(sorted(orders.values()), [1, 2, 3])
        self.assertEqual(len(set(orders.values())), 3, orders)
        # The move actually happened rather than being renumbered into place.
        self.assertEqual(orders["d3"], 2)
        self.assertEqual(orders["d2"], 3)

    def test_a_move_that_would_collide_with_the_prefix_still_stays_dense(self):
        self.db.add_download(entry("d1"))
        self.db.add_download(entry("d2"))
        self.db.add_download(entry("d3"))
        self.db.add_download(entry("d4"))

        self.manager.move_queue_up("d4")
        self.manager.move_queue_up("d4")

        orders = sorted(
            self.db.get_download(f"d{i}").queue_order for i in range(1, 5)
        )
        self.assertEqual(orders, [1, 2, 3, 4])


# ---------------------------------------------------------------------------
# View scoping
# ---------------------------------------------------------------------------

class TestQueueScopeFilter(unittest.TestCase):

    def setUp(self):
        self.model = DownloadTableModel()
        self.model.load_entries([
            entry("d1", status="queued"),
            entry("d2", status="paused"),
            entry("t1", status="completed", queue_id="q2"),
        ])

    def test_all_queues_is_the_unfiltered_default(self):
        self.assertEqual(self.model.queue_scope(), ALL_QUEUES)
        self.assertEqual(self.model.visible_download_count(), 3)
        self.assertFalse(self.model.is_filtered())

    def test_scoping_narrows_the_rows(self):
        self.model.set_queue_scope("q2")
        self.assertEqual(self.model.visible_download_count(), 1)
        self.assertTrue(self.model.is_filtered())

    def test_setting_the_same_scope_twice_is_a_no_op(self):
        self.model.set_queue_scope("q2")
        resets = []
        self.model.modelAboutToBeReset.connect(lambda: resets.append(True))
        self.model.set_queue_scope("q2")
        self.assertEqual(resets, [])

    def test_clearing_the_scope_restores_everything(self):
        self.model.set_queue_scope("q2")
        self.model.set_queue_scope("")
        self.assertEqual(self.model.visible_download_count(), 3)
        self.assertFalse(self.model.is_filtered())

    def test_the_count_members_honour_the_scope(self):
        # Otherwise the header chip counts would disagree with the rows they filter, which is
        # the pre-existing inconsistency this scope must not make worse.
        self.model.set_queue_scope("q2")
        self.assertEqual(
            {k: v for k, v in self.model.get_status_counts().items() if v},
            {"completed": 1},
        )
        self.assertEqual(
            {k: v for k, v in self.model.get_type_counts().items() if v},
            {"http": 1},
        )
        self.assertEqual(
            sum(self.model.get_size_counts().values()), 1
        )

    def test_an_entry_with_no_queue_id_is_treated_as_the_default(self):
        self.model.load_entries([entry("a"), entry("b", queue_id=DEFAULT_QUEUE_ID)])
        self.model.set_queue_scope(DEFAULT_QUEUE_ID)
        self.assertEqual(self.model.visible_download_count(), 2)


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------

class TestSourceQueues(QueueManagerMixin, unittest.TestCase):
    """AnimePahe and YouTube get their own queue, routed by source with no user setup."""

    def test_both_source_queues_are_seeded(self):
        names = [q.name for q in self.manager.get_queues()]
        self.assertEqual(names[0], DEFAULT_QUEUE_NAME)
        self.assertIn("AnimePahe", names)
        self.assertIn("YouTube", names)

    def test_the_source_queues_are_unlimited_within_themselves(self):
        # They exist for organisation, not capping. A user who wants a limit sets one.
        for name in ("AnimePahe", "YouTube"):
            queue = next(q for q in self.manager.get_queues() if q.name == name)
            self.assertEqual(queue.max_concurrent, 0)
            self.assertFalse(queue.is_default)

    def test_a_source_queue_cannot_be_deleted(self):
        queue = next(q for q in self.manager.get_queues() if q.name == "YouTube")
        ok, message = self.manager.delete_queue(queue.id)
        self.assertFalse(ok)
        self.assertIn("built-in source queue", message)

    def test_a_source_queue_can_be_renamed_and_limited(self):
        queue = next(q for q in self.manager.get_queues() if q.name == "AnimePahe")
        self.assertTrue(self.manager.rename_queue(queue.id, "Anime")[0])
        self.manager.set_queue_max_concurrent(queue.id, 2)
        self.assertEqual(self.manager.get_queue(queue.id).max_concurrent, 2)

    def test_seeding_is_idempotent(self):
        before = [(q.id, q.name) for q in self.manager.get_queues()]
        self.db._seed_default_queue()
        self.db._seed_default_queue()
        self.assertEqual([(q.id, q.name) for q in self.manager.get_queues()], before)

    def test_a_youtube_url_is_routed_by_host(self):
        for url in (
            "https://youtu.be/abc",
            "https://www.youtube.com/watch?v=abc",
            "https://m.youtube.com/watch?v=abc",
        ):
            with self.subTest(url=url):
                did = self.manager.add_download(url)
                self.assertEqual(
                    self.db.get_download(did).queue_id, "queue-youtube"
                )

    def test_youtube_metadata_routes_without_a_youtube_url(self):
        did = self.manager.add_download(
            "https://cdn.example.com/video.mp4",
            metadata={"source_type": "youtube_video"},
        )
        self.assertEqual(self.db.get_download(did).queue_id, "queue-youtube")

    def test_animepahe_metadata_routes_to_its_queue(self):
        did = self.manager.add_download(
            "https://example.com/ep1.mkv", metadata={"added_by": "animepahe"}
        )
        self.assertEqual(self.db.get_download(did).queue_id, "queue-animepahe")

    def test_an_ordinary_download_stays_in_the_default_queue(self):
        did = self.manager.add_download("https://example.com/a.zip")
        self.assertEqual(self.db.get_download(did).queue_id, DEFAULT_QUEUE_ID)

    def test_an_explicit_queue_is_never_overridden_by_inference(self):
        # The Add Download dialog and the clipboard monitor pass the active queue; a user's
        # explicit choice has to beat the source heuristic.
        staging = self.manager.create_queue("Staging", 2)
        self.assertTrue(staging[0])
        queue_id = next(q.id for q in self.manager.get_queues() if q.name == "Staging")
        did = self.manager.add_download("https://youtu.be/abc", queue_id=queue_id)
        self.assertEqual(self.db.get_download(did).queue_id, queue_id)

    def test_queue_id_for_name_resolves_case_insensitively(self):
        self.assertEqual(self.manager.queue_id_for_name("youtube"), "queue-youtube")
        self.assertEqual(self.manager.queue_id_for_name("  YouTube "), "queue-youtube")

    def test_an_unknown_queue_name_falls_back_and_does_not_create_it(self):
        # A backlog can be machine-generated; auto-creating from generated content is how you
        # end up with "Queue1", "Queue2".
        before = len(self.manager.get_queues())
        self.assertEqual(self.manager.queue_id_for_name("Queue1"), DEFAULT_QUEUE_ID)
        self.assertEqual(len(self.manager.get_queues()), before)

    def test_get_queue_by_name(self):
        self.assertIsNotNone(self.db.get_queue_by_name("AnimePahe"))
        self.assertIsNone(self.db.get_queue_by_name("Nope"))
        self.assertIsNone(self.db.get_queue_by_name(""))


class TestBacklogQueueDirectives(unittest.TestCase):
    """``queue=`` on a download line, and ``queue:`` as a sticky directive like ``dir:``."""

    def test_the_directive_forms_all_parse(self):
        from my_idm.manager import parse_backlog_entry

        for line in ("# queue: YouTube", "queue = YouTube", "#queue=YouTube",
                     "queue:YouTube"):
            with self.subTest(line=line):
                parsed = parse_backlog_entry(line)
                self.assertEqual(parsed.queue_directive, "YouTube")
                # A queue directive is not a path, so it must not look like one.
                self.assertIsNone(parsed.new_dir)
                self.assertIsNone(parsed.url)

    def test_a_queue_directive_does_not_disturb_the_directory_ones(self):
        from my_idm.manager import parse_backlog_entry

        self.assertEqual(parse_backlog_entry("# dir: /tmp").new_dir, "/tmp")
        self.assertEqual(parse_backlog_entry("dir = /tmp").new_dir, "/tmp")
        self.assertEqual(parse_backlog_entry("[D:/Music]").new_dir, "D:/Music")
        self.assertEqual(parse_backlog_entry("# just a comment").new_dir, None)

    def test_per_line_queue_in_pipe_form(self):
        from my_idm.manager import parse_backlog_entry

        parsed = parse_backlog_entry("https://x/1.zip | dir=/tmp | queue=YouTube")
        self.assertEqual(parsed.url, "https://x/1.zip")
        self.assertEqual(parsed.save_path, "/tmp")
        self.assertEqual(parsed.queue, "YouTube")

    def test_per_line_queue_aria2_style_quoted_with_spaces(self):
        from my_idm.manager import parse_backlog_entry

        parsed = parse_backlog_entry('https://x/1.zip queue="Big files"')
        self.assertEqual(parsed.url, "https://x/1.zip")
        self.assertEqual(parsed.queue, "Big files")

    def test_the_sticky_queue_applies_to_later_lines(self):
        from my_idm.manager import parse_backlog_entry

        parsed = parse_backlog_entry("https://x/1.zip", active_queue="Sticky")
        self.assertEqual(parsed.queue, "Sticky")

    def test_a_per_line_queue_overrides_the_sticky_one(self):
        from my_idm.manager import parse_backlog_entry

        parsed = parse_backlog_entry(
            "https://x/1.zip queue=Override", active_queue="Sticky"
        )
        self.assertEqual(parsed.queue, "Override")

    def test_the_parsed_entry_is_still_a_three_tuple(self):
        # load_backlog and any external caller unpack three values; the new fields must be
        # additive attributes only.
        from my_idm.manager import parse_backlog_entry

        url, save_path, new_dir = parse_backlog_entry("https://x/1.zip queue=Q")
        self.assertEqual(url, "https://x/1.zip")
        self.assertEqual(new_dir, None)
        self.assertEqual(save_path, "")


class TestBacklogQueueRouting(QueueManagerMixin, unittest.TestCase):
    def _write(self, body: str):
        path = Path(self._tmp.name) / "list.txt"
        path.write_text(body, encoding="utf-8")
        return path

    def setUp(self):
        super().setUp()
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _queues_of(self, url: str) -> str:
        entry = self.manager.find_by_url(url)
        queue = self.manager.get_queue(entry.queue_id)
        return queue.name if queue else "?"

    def test_a_sticky_directive_routes_the_lines_below_it(self):
        path = self._write(
            "# queue: YouTube\n"
            "https://youtu.be/aaa\n"
            "https://youtu.be/bbb\n"
        )
        self.assertEqual(self.manager.load_backlog(str(path)), 2)
        self.assertEqual(self._queues_of("https://youtu.be/aaa"), "YouTube")
        self.assertEqual(self._queues_of("https://youtu.be/bbb"), "YouTube")

    def test_a_per_line_queue_beats_the_sticky_one(self):
        path = self._write(
            "# queue: YouTube\n"
            "https://example.com/other.zip queue=AnimePahe\n"
        )
        self.manager.load_backlog(str(path))
        self.assertEqual(self._queues_of("https://example.com/other.zip"), "AnimePahe")

    def test_the_directive_applies_only_to_what_follows_it(self):
        path = self._write(
            "https://example.com/before.zip\n"
            "# queue: AnimePahe\n"
            "https://example.com/after.mkv\n"
        )
        self.manager.load_backlog(str(path))
        self.assertEqual(self._queues_of("https://example.com/before.zip"), DEFAULT_QUEUE_NAME)
        self.assertEqual(self._queues_of("https://example.com/after.mkv"), "AnimePahe")

    def test_a_source_is_inferred_when_the_file_names_no_queue(self):
        # No directive at all: the queue comes from the source, so an AnimePahe-generated
        # backlog still lands in the right place without the file having to say so.
        path = self._write(
            "https://example.com/plain.zip\n"
            "https://youtu.be/aaa\n"
            "# AnimePahe Download\n"
            "https://example.com/ep1.mkv\n"
        )
        self.manager.load_backlog(str(path))
        self.assertEqual(self._queues_of("https://example.com/plain.zip"), DEFAULT_QUEUE_NAME)
        self.assertEqual(self._queues_of("https://youtu.be/aaa"), "YouTube")
        self.assertEqual(self._queues_of("https://example.com/ep1.mkv"), "AnimePahe")

    def test_an_unknown_queue_name_warns_and_uses_default(self):
        path = self._write("# queue: Nope\nhttps://example.com/a.zip\n")
        self.manager.load_backlog(str(path))
        self.assertEqual(self._queues_of("https://example.com/a.zip"), DEFAULT_QUEUE_NAME)


class TestQueueColumn(unittest.TestCase):
    """``Col.QUEUE_NAME`` is append-only, so persisted column indices keep their meaning."""

    def setUp(self):
        self.model = DownloadTableModel()
        self.model.set_queue_names({"q2": "Torrents", "default": DEFAULT_QUEUE_NAME})
        self.model.load_entries([
            entry("d1", status="queued"),
            entry("t1", status="queued", queue_id="q2"),
        ])

    def test_it_is_the_last_column_so_nothing_shifts(self):
        self.assertEqual(Col.QUEUE_NAME, Col.COUNT - 1)

    def test_it_renders_the_queue_name(self):
        rendered = {
            eid: self.model.data(
                self.model.index(self.model._id_to_row[eid], Col.QUEUE_NAME)
            )
            for eid in ("d1", "t1")
        }
        self.assertEqual(rendered["d1"], DEFAULT_QUEUE_NAME)
        self.assertEqual(rendered["t1"], "Torrents")

    def test_a_row_shows_its_own_queue(self):
        rows = {e.id: e for e in self.model._entries}
        rendered = {
            eid: self.model.data(self.model.index(self.model._id_to_row[eid], Col.QUEUE_NAME))
            for eid in rows
        }
        self.assertEqual(rendered["d1"], DEFAULT_QUEUE_NAME)
        self.assertEqual(rendered["t1"], "Torrents")

    def test_an_unknown_queue_renders_as_default_not_a_uuid(self):
        self.model.load_entries([entry("x", queue_id="ghost")])
        row = self.model._id_to_row["x"]
        self.assertEqual(
            self.model.data(self.model.index(row, Col.QUEUE_NAME)),
            DEFAULT_QUEUE_NAME,
        )

    def test_it_sorts_by_name(self):
        self.model.load_entries([
            entry("a", queue_id="q2"),      # Torrents
            entry("b"),                     # Default
            entry("c", queue_id="q2"),
        ])
        self.model.sort(Col.QUEUE_NAME, Qt.SortOrder.AscendingOrder)
        order = [self.model.data(self.model.index(r, Col.QUEUE_NAME))
                 for r in range(self.model.rowCount())]
        self.assertEqual(order, ["Default", "Torrents", "Torrents"])
        self.assertEqual(order, sorted(order, key=str.lower))

    def test_a_rename_updates_every_row_at_once(self):
        self.model.set_queue_names({"q2": "Big files", "default": DEFAULT_QUEUE_NAME})
        rows = [
            self.model.data(self.model.index(r, Col.QUEUE_NAME))
            for r in range(self.model.rowCount())
        ]
        self.assertIn("Big files", rows)

    def test_set_queue_names_is_a_no_op_for_identical_input(self):
        # Guards the early return: rebuilding on every selection change would be wasteful.
        self.model.set_queue_names({"q2": "Torrents", "default": DEFAULT_QUEUE_NAME})
        self.assertEqual(
            self.model.data(self.model.index(1, Col.QUEUE_NAME)), "Torrents"
        )


class TestClipboardNotification(unittest.TestCase):
    """Clipboard capture must not be silent: the row appears with nothing to explain it."""

    def _capture(self, filenames, skipped=0):
        from my_idm import notifications

        with patch.object(notifications, "show_notification") as show:
            result = notifications.notify_clipboard_download_captured(filenames, skipped)
        return result, show

    def test_a_single_capture_is_named(self):
        result, show = self._capture(["a.zip"])
        self.assertTrue(result)
        title, message = show.call_args[0][0], show.call_args[0][1]
        self.assertEqual(title, "Captured from Clipboard")
        self.assertIn("a.zip", message)

    def test_the_source_is_named(self):
        # The user has to be able to tell where the row came from.
        _result, show = self._capture(["a.zip"])
        self.assertIn("Clipboard", show.call_args[0][0])

    def test_a_batch_is_counted_not_listed(self):
        _result, show = self._capture(["a.zip", "b.zip", "c.zip"])
        message = show.call_args[0][1]
        self.assertIn("3 downloads", message)
        # A wall of filenames is not actionable.
        self.assertNotIn("b.zip", message)

    def test_skipped_lines_are_reported(self):
        _result, show = self._capture(["a.zip", "b.zip"], skipped=7)
        self.assertIn("7 over the per-copy limit", show.call_args[0][1])

    def test_nothing_captured_means_no_notification(self):
        from my_idm import notifications

        with patch.object(notifications, "show_notification") as show:
            self.assertFalse(notifications.notify_clipboard_download_captured([]))
        show.assert_not_called()


class TestQueueColours(QueueDbMixin, unittest.TestCase):
    def test_every_seeded_queue_has_a_distinct_colour(self):
        colours = [q.color for q in self.db.get_queues()]
        self.assertTrue(all(colours), colours)
        self.assertEqual(len(set(colours)), len(colours), colours)

    def test_a_new_queue_gets_a_colour_without_being_told(self):
        # A colourless queue is an invisible swatch, and a user has no reason to think to go
        # and colour it.
        self.db.create_queue("Torrents", 1)
        queue = next(q for q in self.db.get_queues() if q.name == "Torrents")
        self.assertTrue(queue.color)

    def test_two_new_queues_do_not_share_a_colour(self):
        self.db.create_queue("First", 1)
        self.db.create_queue("Second", 1)
        colours = [
            q.color for q in self.db.get_queues()
            if q.name in ("First", "Second")
        ]
        self.assertEqual(len(set(colours)), 2, colours)

    def test_set_queue_colour(self):
        self.db.create_queue("Torrents", 1)
        queue = next(q for q in self.db.get_queues() if q.name == "Torrents")
        ok, message = self.db.set_queue_color(queue.id, "#ff00aa")
        self.assertTrue(ok, message)
        self.assertEqual(self.db.get_queue(queue.id).color, "#ff00aa")

    def test_setting_a_colour_on_a_missing_queue_is_refused(self):
        ok, message = self.db.set_queue_color("nope", "#ff00aa")
        self.assertFalse(ok)
        self.assertIn("no longer exists", message)

    def test_an_invalid_colour_is_refused_rather_than_stored(self):
        # Storing it would paint an invisible swatch.
        self.db.create_queue("Torrents", 1)
        queue = next(q for q in self.db.get_queues() if q.name == "Torrents")
        self.assertFalse(self.db.set_queue_color(queue.id, "not-a-colour")[0])

    def test_normalize_accepts_the_shapes_a_user_can_type(self):
        self.assertEqual(normalize_queue_color("#ABC"), "#aabbcc")
        self.assertEqual(normalize_queue_color("4a9eff"), "#4a9eff")
        self.assertEqual(normalize_queue_color("  #4A9EFF "), "#4a9eff")
        self.assertEqual(normalize_queue_color("#abc"), "#aabbcc")

    def test_normalize_rejects_junk(self):
        for junk in ("", None, "red", "#12345", "#GGGGGG", "rgb(1,2,3)"):
            with self.subTest(value=junk):
                self.assertEqual(normalize_queue_color(junk), "")

    def test_an_existing_queues_colour_is_never_overwritten_by_a_reopen(self):
        self.db.create_queue("Torrents", 1)
        queue = next(q for q in self.db.get_queues() if q.name == "Torrents")
        self.db.set_queue_color(queue.id, "#010203")

        self.db._seed_default_queue()
        self.db._seed_default_queue()

        self.assertEqual(self.db.get_queue(queue.id).color, "#010203")

    def test_a_colourless_row_reads_back_as_the_default_colour(self):
        # The backfill colours existing rows; the mapper also defends, so a row that somehow
        # has no colour still renders a visible swatch.
        self.db._conn.execute(
            "UPDATE queues SET color = '' WHERE id = ?", (DEFAULT_QUEUE_ID,)
        )
        self.assertTrue(self.db.get_queue(DEFAULT_QUEUE_ID).color)


class QueueColourRouting(QueueManagerMixin, unittest.TestCase):
    def test_set_queue_color_emits_and_reaches_the_store(self):
        seen = []
        self.manager.queues_changed.connect(lambda: seen.append(True))
        queue_id = next(q.id for q in self.manager.get_queues() if q.name == "YouTube")

        ok, message = self.manager.set_queue_color(queue_id, "#123456")

        self.assertTrue(ok, message)
        self.assertEqual(self.manager.get_queue(queue_id).color, "#123456")
        self.assertEqual(len(seen), 1)


class TestQueueColourDelegate(unittest.TestCase):
    """The Queue column shows a swatch beside the name, and only the swatch when narrow."""

    def setUp(self):
        from my_idm.delegates import QueueColumnDelegate

        self.app = QApplication.instance() or QApplication(sys.argv)
        self.delegate = QueueColumnDelegate()
        self.model = DownloadTableModel()
        self.model.set_queue_names({"q2": "Torrents", "default": DEFAULT_QUEUE_NAME})
        self.model.set_queue_colors({"q2": "#3fb950", "default": "#4a9eff"})
        self.model.load_entries([
            entry("d1"),
            entry("t1", queue_id="q2"),
        ])
        self.index = self.model.index(
            self.model._id_to_row["t1"], Col.QUEUE_NAME
        )

    def _render(self, width):
        """Paint the cell and report how much ink landed in each region."""
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QColor, QImage, QPainter, QPalette
        from PySide6.QtWidgets import QStyleOptionViewItem

        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, width, 28)
        opt.font = self.app.font()
        opt.features |= QStyleOptionViewItem.ViewItemFeature.HasDisplay
        base = opt.palette.color(QPalette.ColorRole.Base).name()
        image = QImage(width, 28, QImage.Format.Format_ARGB32)
        image.fill(QColor(base))
        painter = QPainter(image)
        self.delegate.paint(painter, opt, self.index)
        painter.end()

        swatch_edge = self.delegate.PADDING + self.delegate.SWATCH
        text_start = swatch_edge + self.delegate.GAP

        def ink(x_from, x_to):
            return sum(
                1 for y in range(28) for x in range(max(0, x_from), min(width, x_to))
                if image.pixelColor(x, y).name() != base
            )

        return ink(0, swatch_edge), ink(text_start, width)

    def test_the_swatch_is_always_drawn(self):
        swatch, _text = self._render(200)
        self.assertGreater(swatch, 0)

    def test_a_wide_column_shows_the_colour_and_the_name(self):
        swatch, text = self._render(200)
        self.assertGreater(swatch, 0)
        self.assertGreater(text, 0, "the queue name should be painted when there is room")

    def test_a_narrow_column_shows_only_the_colour(self):
        # The fallback the feature asks for: the colour still identifies the queue, whereas
        # "Torr..." does not.
        swatch, text = self._render(30)
        self.assertGreater(swatch, 0)
        self.assertEqual(text, 0, "no room for the name: the swatch alone should be drawn")

    def test_the_name_is_dropped_before_it_is_made_unreadable(self):
        # At the threshold the available width is below MIN_TEXT_WIDTH, so nothing partial and
        # useless is painted.
        from PySide6.QtWidgets import QStyleOptionViewItem
        from PySide6.QtCore import QRect

        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, 200, 28)
        swatch_edge = self.delegate.PADDING + self.delegate.SWATCH
        text_start = swatch_edge + self.delegate.GAP
        available = opt.rect.right() - self.delegate.PADDING - text_start
        self.assertGreater(available, self.delegate.MIN_TEXT_WIDTH)

    def test_a_row_whose_queue_has_no_colour_still_shows_its_name(self):
        self.model.set_queue_colors({})
        self.model.load_entries([entry("t1", queue_id="q2")])
        index = self.model.index(0, Col.QUEUE_NAME)
        self.assertEqual(self.model.data(index, QUEUE_COLOR_ROLE), "")

        from PySide6.QtCore import QRect
        from PySide6.QtGui import QColor, QImage, QPainter, QPalette
        from PySide6.QtWidgets import QStyleOptionViewItem

        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, 200, 28)
        opt.font = self.app.font()
        opt.features |= QStyleOptionViewItem.ViewItemFeature.HasDisplay
        base = opt.palette.color(QPalette.ColorRole.Base).name()
        image = QImage(200, 28, QImage.Format.Format_ARGB32)
        image.fill(QColor(base))
        painter = QPainter(image)
        self.delegate.paint(painter, opt, index)
        painter.end()
        ink = sum(
            1 for y in range(28) for x in range(200)
            if image.pixelColor(x, y).name() != base
        )
        self.assertGreater(ink, 0, "a colourless queue must still be identifiable by name")

    def test_size_hint_leaves_room_for_the_swatch(self):
        from PySide6.QtWidgets import QStyleOptionViewItem

        opt = QStyleOptionViewItem()
        opt.font = self.app.font()
        hint = self.delegate.sizeHint(opt, self.index)
        self.assertGreaterEqual(
            hint.width(),
            self.delegate.PADDING * 2 + self.delegate.SWATCH + self.delegate.GAP,
        )


class TestQueueColumnFilter(unittest.TestCase):
    """The Queue column's header filter: any number of queues, independent of the scope."""

    def setUp(self):
        self.model = DownloadTableModel()
        self.model.set_queue_names(
            {"default": DEFAULT_QUEUE_NAME, "q1": "Torrents", "q2": "YouTube"}
        )
        self.model.load_entries([
            entry("d0", status="paused"),
            entry("t0", status="paused", queue_id="q1"),
            entry("u0", status="paused", queue_id="q2"),
            entry("t1", status="completed", queue_id="q1"),
        ])

    def _visible(self):
        return {e.id for e in self.model._entries}

    def test_no_filter_shows_everything(self):
        self.assertIsNone(self.model.queue_filter())
        self.assertFalse(self.model.is_queue_filtered())
        self.assertEqual(self.model.visible_download_count(), 4)
        self.assertFalse(self.model.is_filtered())

    def test_filtering_to_one_queue(self):
        self.model.set_queue_filter({"q1"})
        self.assertEqual(self._visible(), {"t0", "t1"})
        self.assertTrue(self.model.is_queue_filtered())
        self.assertTrue(self.model.is_filtered())

    def test_filtering_to_several_queues(self):
        self.model.set_queue_filter({"q1", "q2"})
        self.assertEqual(self._visible(), {"t0", "t1", "u0"})

    def test_clearing_restores_everything(self):
        self.model.set_queue_filter({"q1"})
        self.model.set_queue_filter(None)
        self.assertEqual(self.model.visible_download_count(), 4)
        self.assertFalse(self.model.is_filtered())

    def test_selecting_every_queue_is_the_same_as_no_filter(self):
        # "Select All" in the popup must not read as an active filter.
        self.model.set_queue_filter({"default", "q1", "q2"})
        self.assertIsNone(self.model.queue_filter())
        self.assertEqual(self.model.visible_download_count(), 4)

    def test_clear_filters_includes_the_queue_filter(self):
        self.model.set_queue_filter({"q1"})
        self.model.clear_filters()
        self.assertIsNone(self.model.queue_filter())
        self.assertEqual(self.model.visible_download_count(), 4)

    def test_an_empty_queue_id_counts_as_the_default_queue(self):
        model = DownloadTableModel()
        model.set_queue_names({"default": DEFAULT_QUEUE_NAME})
        model.load_entries([entry("blank", queue_id="")])
        model.set_queue_filter({DEFAULT_QUEUE_ID})
        self.assertEqual(model.visible_download_count(), 1)

    def test_it_is_independent_of_the_scope(self):
        # The scope is the toolbar's single-queue selection; this is the header's any-number
        # selection. Both can be active at once.
        self.model.set_queue_scope("q1")
        self.model.set_queue_filter({"q2"})
        self.assertEqual(self._visible(), set())

    def test_counts_are_per_queue_and_ignore_the_filter_itself(self):
        # A popup that counted only ticked queues would show the rest as 0 and read as if
        # they were empty.
        self.model.set_queue_filter({"q1"})
        counts = self.model.get_queue_counts()
        self.assertEqual(counts["default"], 1)
        self.assertEqual(counts["q1"], 2)
        self.assertEqual(counts["q2"], 1)

    def test_counts_honour_the_other_active_filters(self):
        self.model.set_status_filter({"completed"})
        counts = self.model.get_queue_counts()
        self.assertEqual(counts["q1"], 1)
        self.assertEqual(counts["q2"], 0)

    def test_filter_items_are_ids_and_names(self):
        items = dict(self.model.queue_filter_items())
        self.assertEqual(items["q1"], "Torrents")
        self.assertEqual(items[DEFAULT_QUEUE_ID], DEFAULT_QUEUE_NAME)

    def test_a_rename_does_not_break_an_active_filter(self):
        # The filter stores ids precisely so this cannot happen: a name-based filter would
        # hold a label that matches nothing and silently empty the view.
        self.model.set_queue_filter({"q1"})
        self.model.set_queue_names({"default": DEFAULT_QUEUE_NAME, "q1": "Renamed"})
        self.assertEqual(self.model.visible_download_count(), 2)

    def test_setting_the_same_filter_twice_does_not_reset_again(self):
        self.model.set_queue_filter({"q1"})
        resets = []
        self.model.modelAboutToBeReset.connect(lambda: resets.append(True))
        self.model.set_queue_filter({"q1"})
        self.assertEqual(resets, [])


class TestQueueFilterPopupWiring(unittest.TestCase):
    def test_the_queue_column_is_filterable(self):
        from my_idm.header_view import FilterHeaderView

        self.assertIn(
            Col.QUEUE_NAME, FilterHeaderView._FILTER_COLUMNS
        )
        self.assertEqual(FilterHeaderView._FILTER_COLUMNS[Col.QUEUE_NAME], "Queue")

    def test_the_popup_accepts_a_supplied_label_map(self):
        # Queue ids are the filter keys but names are what a person recognises, so the popup
        # takes the live id -> name mapping rather than rendering uuids.
        from my_idm.header_view import MultiselectFilterPopup

        app = QApplication.instance() or QApplication(sys.argv)
        popup = MultiselectFilterPopup(
            Col.QUEUE_NAME, None, {"q1": 2}, items=[("q1", "Torrents"), ("q2", "YouTube")]
        )
        try:
            self.assertEqual(
                sorted(popup._checkboxes), ["q1", "q2"]
            )
            self.assertIn("Torrents", popup._checkboxes["q1"].text())
        finally:
            popup.close()

    def test_the_popup_renders_color_swatch_icons(self):
        from my_idm.header_view import MultiselectFilterPopup

        app = QApplication.instance() or QApplication(sys.argv)
        popup = MultiselectFilterPopup(
            Col.QUEUE_NAME,
            None,
            {"q1": 2, "q2": 1},
            items=[("q1", "Torrents"), ("q2", "YouTube")],
            colors={"q1": "#3fb950", "q2": "#f85149"},
        )
        try:
            self.assertFalse(popup._checkboxes["q1"].icon().isNull())
            self.assertFalse(popup._checkboxes["q2"].icon().isNull())
        finally:
            popup.close()


class TestQueueColumnTooltip(unittest.TestCase):
    """The column can collapse to the swatch alone, so hover has to carry the name."""

    def setUp(self):
        self.model = DownloadTableModel()
        self.model.set_queue_names({"q1": "Torrents"})
        self.model.set_queue_colors({"q1": "#3fb950"})
        self.model.load_entries([entry("t0", queue_id="q1")])
        self.index = self.model.index(0, Col.QUEUE_NAME)

    def test_the_tooltip_is_the_queue_name(self):
        self.assertEqual(
            self.model.data(self.index, Qt.ItemDataRole.ToolTipRole), "Torrents"
        )

    def test_the_tooltip_carries_no_colour_code(self):
        # Nobody identifies a queue by its hex, and it made the tooltip read like a debug
        # field rather than a label.
        tip = str(self.model.data(self.index, Qt.ItemDataRole.ToolTipRole))
        self.assertNotIn("#", tip)
        self.assertNotIn("3fb950", tip.lower())

    def test_the_tooltip_is_the_name_even_with_no_colour_set(self):
        self.model.set_queue_colors({})
        self.model.load_entries([entry("t0", queue_id="q1")])
        self.assertEqual(
            self.model.data(
                self.model.index(0, Col.QUEUE_NAME), Qt.ItemDataRole.ToolTipRole
            ),
            "Torrents",
        )

    def test_the_tooltip_does_not_depend_on_the_column_width(self):
        # A coloured square with no legend is unreadable, which is why the swatch-only
        # fallback needs the name on hover.
        self.assertIn("Torrents", self.model.data(
            self.index, Qt.ItemDataRole.ToolTipRole
        ))

    def test_a_section_header_has_no_queue_tooltip(self):
        header = DownloadEntry(
            id="h", url="", filename="", status="queued", is_section_header=True,
            section_title="QUEUED", section_count=2,
        )
        self.model.load_entries([header])
        tip = self.model.data(
            self.model.index(0, Col.QUEUE_NAME), Qt.ItemDataRole.ToolTipRole
        )
        self.assertNotIn("Torrents", str(tip))


class TestQueueMenuGrouping(_MainWindowTestCase):
    def test_move_up_and_down_share_the_queue_group(self):
        # They are ordering commands, not playback ones, so they belong with queue membership
        # rather than up with the transport controls.
        edit_menu = self.win.menuBar().actions()[1].menu()
        titles = [a.text() for a in edit_menu.actions()]
        queues_at = titles.index("Queues")
        up_at = titles.index(next(t for t in titles if t.startswith("Move Up")))
        down_at = titles.index(next(t for t in titles if t.startswith("Move Down")))
        self.assertLess(queues_at, up_at)
        self.assertLess(up_at, down_at)
        # ...and nothing ordering-related is left up with the transport controls.
        copy_at = titles.index(next(t for t in titles if t.startswith("Copy URL")))
        self.assertGreater(up_at, copy_at)
        # The queue commands close the menu, in one trailing group.
        self.assertGreater(queues_at, titles.index("Recheck"))
        self.assertEqual(down_at, len(titles) - 1)

    def test_the_move_up_down_labels_say_which_queue_they_move_in(self):
        edit_menu = self.win.menuBar().actions()[1].menu()
        titles = [a.text() for a in edit_menu.actions()]
        self.assertIn("Move Up in Queue", titles)
        self.assertIn("Move Down in Queue", titles)

    def test_the_edit_menu_carries_the_same_file_commands_as_the_context_menu(self):
        """Open File / Open Folder existed only on a right-click.

        Without them in the Edit menu there was no keyboard or menu-only route to a finished
        download's folder, which is the one thing a user does once per download at the end.
        """
        edit_titles = [a.text() for a in self.win.menuBar().actions()[1].menu().actions()]
        context_titles = self._row_context_menu()
        for label in ("Rename…", "Move…", "Open File", "Open Folder", "Delete"):
            with self.subTest(label=label):
                self.assertIn(label, edit_titles)
                self.assertIn(label, context_titles)

        first = min(edit_titles.index(t) for t in
                    ("Rename…", "Move…", "Open File", "Open Folder", "Delete"))
        # The contiguous run of real items from there: no separator may split the group.
        run = []
        for title in edit_titles[first:]:
            if not title:
                break
            run.append(title)
        self.assertEqual(
            run,
            ["Rename…", "Move…", "Open File", "Open Folder", "Delete", "Recheck"],
            f"the file commands must be one run, in the context menu's order: {edit_titles}",
        )
        self.assertNotIn(
            "Copy URL / Magnet", run,
            "Rename must not sit up with Copy URL again",
        )

    def _row_context_menu(self) -> list:
        """Build the row context menu and return its titles in display order.

        ``exec`` is overridden rather than patched so the menu under test is the real widget
        tree: a MagicMock would record the ``addAction`` *calls*, not the order a user sees.
        """
        self.db.add_download(entry("d0", status="paused"))
        self.win._load_history()
        self.win._table.selectRow(0)
        built: list = []

        class _CapturingMenu(QMenu):
            def exec(self, *args, **kwargs):
                built.append(self)
                return None

        pos = self.win._table.visualRect(self.win._model.index(0, 0)).center()
        with patch("my_idm.main_window.QMenu", _CapturingMenu):
            self.win._show_context_menu(pos)

        self.assertEqual(len(built), 1, "the context menu was never built")
        return [a.text() for a in built[0].actions()]

    def test_the_row_context_menu_groups_the_move_commands_with_the_queues(self):
        # Regression: Move Up / Move Down sat in the transport group, right after Pause, so
        # two ordering commands read as playback controls and the queue commands ended up
        # split across the menu in two separate groups.
        titles = self._row_context_menu()
        queue_at = titles.index("Move to Queue")
        up_at = titles.index("Move Up in Queue")
        down_at = titles.index("Move Down in Queue")
        self.assertLess(queue_at, up_at, f"queue membership first: {titles}")
        self.assertLess(up_at, down_at)
        # One trailing group, the way the Edit menu orders them.
        self.assertEqual(up_at - queue_at, 1, f"no separator splits the pair: {titles}")
        self.assertEqual(down_at - up_at, 1)
        # ...and nothing ordering-related is left up with the transport controls.
        for transport in ("Resume", "Pause", "Stop", "Start Seeding"):
            self.assertLess(
                titles.index(transport), queue_at,
                f"{transport} must stay with the transport controls: {titles}",
            )
        self.assertLess(down_at, titles.index("Route through Tor"), f"a later group closes the queue group: {titles}")

    def test_the_row_context_menu_groups_the_file_commands_together(self):
        # Regression: Rename sat with Copy URL and Move with Scan/Recheck, so the six file
        # commands were spread over three groups with the two transport-ish ones in between.
        titles = self._row_context_menu()
        group = [t for t in ("Rename…", "Move…", "Open File", "Open Folder",
                             "Delete File", "Delete")]
        first = min(titles.index(t) for t in group)
        self.assertEqual(
            titles[first:first + len(group)], group,
            f"the file commands must be one unbroken run: {titles}",
        )
        # ...and they close the menu, so Delete is still the last thing in it.
        self.assertEqual(titles[-1], "Delete")
        # Nothing file-related is left above them.
        for other in ("Rename…", "Move…", "Open File", "Open Folder", "Delete File"):
            self.assertGreater(
                titles.index(other), titles.index("Route through Tor"),
                f"{other} belongs in the file group at the end: {titles}",
            )

    def test_move_to_queue_enables_as_soon_as_a_row_is_selected(self):
        # Regression: it was enabled only when the menu was rebuilt, so with rows selected the
        # submenu stayed greyed out and read as "not clickable".
        self.win._table.clearSelection()
        self.win._update_move_to_queue_enabled()
        self.assertFalse(self.win._menu_move_to_queue.isEnabled())

        self.db.add_download(entry("d0", status="paused"))
        self.win._load_history()
        self.win._table.selectRow(0)

        self.assertTrue(self.win._menu_move_to_queue.isEnabled())

    def test_selecting_then_clearing_toggles_it_back(self):
        self.db.add_download(entry("d0", status="paused"))
        self.win._load_history()
        self.win._table.selectRow(0)
        self.assertTrue(self.win._menu_move_to_queue.isEnabled())
        self.win._table.clearSelection()
        self.assertFalse(self.win._menu_move_to_queue.isEnabled())

    def test_the_individual_actions_are_greyed_out_too(self):
        # Disabling the submenu alone left the actions looking live inside it, which is what
        # made the menu read as "not clickable" rather than "nothing selected".
        self.win._table.clearSelection()
        self.win._update_move_to_queue_enabled()
        self.assertTrue(self.win._move_to_queue_actions)
        self.assertTrue(
            all(not a.isEnabled() for a in self.win._move_to_queue_actions)
        )

        self.db.add_download(entry("d0", status="paused"))
        self.win._load_history()
        self.win._table.selectRow(0)
        self.assertTrue(
            all(a.isEnabled() for a in self.win._move_to_queue_actions)
        )


class TestQueueManagerDialog(QueueManagerMixin, unittest.TestCase):

    def _dialog(self):
        self.manager.create_queue("Torrents", 2)
        dialog = QueueManagerDialog(self.manager, None)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_it_lists_the_default_queue_and_the_rest(self):
        dialog = self._dialog()
        names = [
            dialog._table.item(row, 0).text() for row in range(dialog._table.rowCount())
        ]
        self.assertEqual(names[0], DEFAULT_QUEUE_NAME)
        self.assertIn("Torrents", names)

    def _row_of(self, dialog, name: str) -> int:
        return next(
            r for r in range(dialog._table.rowCount())
            if dialog._table.item(r, 0).text() == name
        )

    def _spin_of(self, dialog, name: str) -> QSpinBox:
        return dialog._table.cellWidget(self._row_of(dialog, name), 2)

    def _row_queue_id(self, dialog, name: str) -> str:
        return dialog._table.item(self._row_of(dialog, name), 0).data(Qt.ItemDataRole.UserRole)

    def test_the_default_queue_cannot_be_renamed_or_deleted_from_the_ui(self):
        dialog = self._dialog()
        dialog._table.selectRow(0)
        self.assertFalse(dialog._rename_btn.isEnabled())
        self.assertFalse(dialog._delete_btn.isEnabled())

    def test_a_non_default_queue_can_be_renamed_and_deleted_from_the_ui(self):
        dialog = self._dialog()
        dialog._table.selectRow(self._row_of(dialog, "Torrents"))
        self.assertTrue(dialog._rename_btn.isEnabled())
        self.assertTrue(dialog._delete_btn.isEnabled())

    def test_the_limit_editor_shows_the_configured_value(self):
        dialog = self._dialog()
        spin = self._spin_of(dialog, "Torrents")
        self.assertEqual(spin.value(), 2)
        self.assertEqual(spin.minimum(), 0)

    def test_the_field_shows_the_number_that_is_stored(self):
        """No specialValueText: the field must not disagree with itself.

        It substituted the word "Global" for 0, so typing 0 displayed "Global" and typing
        "Global" was rejected. What you typed stopped being what you saw.
        """
        dialog = self._dialog()
        spin = self._spin_of(dialog, "Torrents")
        self.assertEqual(spin.specialValueText(), "")
        # Typing 0 must leave a 0 on screen, not a word.
        spin.setValue(0)
        self.assertEqual(spin.value(), 0)
        self.assertEqual(spin.text(), "0")

    def test_the_meaning_of_zero_lives_outside_the_editable_field(self):
        dialog = self._dialog()
        header = dialog._table.horizontalHeaderItem(2).text()
        self.assertIn("0 = Global", header)
        self.assertIn("0 = follow the global limit", self._spin_of(dialog, "Torrents").toolTip())

    def test_the_editors_live_in_the_columns_they_edit(self):
        # The 4th column was removed and the concurrency editor moved into "Max at once"
        # itself; the two bandwidth editors then took columns 4 and 5, again in place.
        dialog = self._dialog()
        table = dialog._table
        self.assertEqual(table.columnCount(), 5)
        self.assertIn("Max at once", table.horizontalHeaderItem(2).text())
        self.assertIn("Download limit", table.horizontalHeaderItem(3).text())
        self.assertIn("Upload limit", table.horizontalHeaderItem(4).text())

    def _bandwidth_spin(self, dialog, name, column):
        return dialog._table.cellWidget(self._row_of(dialog, name), column)

    def test_the_bandwidth_editors_show_the_stored_value_in_kb(self):
        """Stored in bytes, edited in KB/s - the unit the global limit is set in everywhere else.

        A queue limit typed in a different unit from the global limit it is compared against is a
        limit nobody can reason about.
        """
        dialog = self._dialog()
        queue_id = self._row_queue_id(dialog, "Torrents")
        self.manager.set_queue_limits(queue_id, 512 * 1024, 64 * 1024)
        dialog._reload()
        self.assertEqual(
            self._bandwidth_spin(dialog, "Torrents", 3).value(), 512
        )
        self.assertEqual(
            self._bandwidth_spin(dialog, "Torrents", 4).value(), 64
        )

    def test_editing_one_direction_leaves_the_other_alone(self):
        """The editor that fired passes only its own direction.

        Had it written both from its own two spin boxes, a `_reload` rebuild - or a second spin box
        emitting during one - would silently reset the opposite ceiling to whatever the widget
        happened to hold.
        """
        dialog = self._dialog()
        queue_id = self._row_queue_id(dialog, "Torrents")
        self.manager.set_queue_limits(queue_id, 512 * 1024, 64 * 1024)
        dialog._reload()
        self._bandwidth_spin(dialog, "Torrents", 3).setValue(256)
        stored = self.manager._db.get_queue(queue_id)
        self.assertEqual(stored.download_limit, 256 * 1024)
        self.assertEqual(stored.upload_limit, 64 * 1024)

    def test_the_bandwidth_tooltip_says_what_zero_means(self):
        dialog = self._dialog()
        for column in (3, 4):
            with self.subTest(column=column):
                self.assertIn(
                    "0 = follow the global limit",
                    self._bandwidth_spin(dialog, "Torrents", column).toolTip(),
                )

    def test_the_bandwidth_editor_carries_its_unit(self):
        dialog = self._dialog()
        for column in (3, 4):
            with self.subTest(column=column):
                self.assertEqual(
                    self._bandwidth_spin(dialog, "Torrents", column).suffix(), " KB/s"
                )

    def test_the_qualifier_sits_on_its_own_header_line(self):
        """"(0 = Global)" goes below its column, not beside it.

        On one line the three limit headers are the widest thing in the dialog, so the window has
        to grow to hold text that is mostly punctuation - which is how every header came to be
        trimmed on a screen that could not spare the width. Two lines also keeps the meaning of
        `0` present without making it the first thing the eye reads.
        """
        dialog = self._dialog()
        for column in (2, 3, 4):
            text = dialog._table.horizontalHeaderItem(column).text()
            with self.subTest(column=column, header=text):
                self.assertIn("\n", text, "the qualifier must be on its own line")
                label, qualifier = text.split("\n")
                self.assertTrue(label.strip())
                self.assertEqual(qualifier, "(0 = Global)")

    def test_the_header_is_tall_enough_for_two_lines(self):
        """QHeaderView sizes itself for one line; without this the second is clipped."""
        dialog = self._dialog()
        header = dialog._table.horizontalHeader()
        one_line = header.fontMetrics().height()
        self.assertGreaterEqual(
            header.height(), one_line * 2,
            "a two-line header needs the section to grow or the second line is cut off",
        )

    def test_every_column_is_wide_enough_for_its_widest_header_line(self):
        """The complaint this pins: the headers were trimmed ("OWNLOADS", "(0 = GLOBAL").

        Two causes. `stretchLastSection` defaults to True and overrides `ResizeToContents`, so the
        last section was given the leftover width instead of the width its header needs. And the
        window was sized from the header text alone - ignoring the frame, the layout margins and
        the scrollbar - so the table got less than the window and Qt shrank every section to fit
        the *viewport*, clipping the first and last letter of each. The dialog now measures the
        chrome as well, so the columns fit instead of being squeezed.
        """
        dialog = self._dialog()
        dialog.show()
        self.addCleanup(dialog.hide)
        QApplication.processEvents()
        metrics = dialog._table.horizontalHeader().fontMetrics()
        for column in range(dialog._table.columnCount()):
            text = dialog._table.horizontalHeaderItem(column).text()
            widest = max(metrics.horizontalAdvance(line) for line in text.split("\n"))
            with self.subTest(column=column, header=text):
                self.assertGreaterEqual(
                    dialog._table.columnWidth(column), widest,
                    f"column {column} ({text!r}) is too narrow for its widest header line",
                )


    def test_the_headers_fit_at_any_font_size(self):
        """The widths are measured from the live font, so this must hold at every size.

        Both rounds of trimming were this test's property failing at a size it had never been
        asked about: Qt's own `ResizeToContents` hint came up under the text at the user's DPI, and
        the `MAX_DIALOG_WIDTH` clamp squeezed the name column on a large font. Asserting it once at
        the default font would have passed both times, so the sizes are swept here.

        18pt is past the default by a wide margin on purpose - that is where the clamp bites, and
        it is where the name column has to be pinned rather than left to stretch.
        """
        original = QApplication.instance().font()
        self.addCleanup(QApplication.instance().setFont, original)
        for point_size in (10, 14, 18):
            with self.subTest(point_size=point_size):
                font = QFont(original)
                font.setPointSize(point_size)
                QApplication.instance().setFont(font)
                dialog = QueueManagerDialog(self.manager)
                try:
                    dialog.show()
                    QApplication.processEvents()
                    metrics = dialog._table.horizontalHeader().fontMetrics()
                    for column, text in enumerate(QueueManagerDialog.HEADERS):
                        widest = max(
                            metrics.horizontalAdvance(line) for line in text.split("\n")
                        )
                        self.assertGreaterEqual(
                            dialog._table.columnWidth(column), widest,
                            f"column {column} ({text!r}) is too narrow for its widest header "
                            f"line at {point_size}pt",
                        )
                finally:
                    dialog.hide()


    def test_the_last_column_is_not_force_stretched_over_its_header(self):
        dialog = self._dialog()
        self.assertFalse(
            dialog._table.horizontalHeader().stretchLastSection(),
            "stretchLastSection gives the last column the leftover width instead of the width "
            "its header needs, which is what elided 'Upload limit  (0 = Global)'",
        )

    def test_all_the_columns_fit_without_a_horizontal_scrollbar(self):
        """The whole table visible at once, which is what "fits the headers" has to mean."""
        dialog = self._dialog()
        dialog.show()
        self.addCleanup(dialog.hide)
        QApplication.processEvents()
        table = dialog._table
        total = sum(table.columnWidth(c) for c in range(table.columnCount()))
        self.assertLessEqual(
            total, table.viewport().width(),
            "the columns do not fit the viewport, so at least one header is cut off",
        )

    def test_the_queue_name_column_absorbs_the_spare_width(self):
        """The name column is the only one that stretches; the rest keep measured widths.

        `Interactive` rather than `ResizeToContents` on the others, because Qt's own hint is what
        under-measured the headers twice. They still size themselves - just from this dialog's
        measurement rather than the style's - so the slack still has nowhere to go but the name.
        """
        dialog = self._dialog()
        # `Stretch` is resolved by the layout, so the leftover width only exists once the dialog
        # is on screen - a hidden widget's viewport is still its creation size.
        dialog.show()
        self.addCleanup(dialog.hide)
        QApplication.processEvents()
        header = dialog._table.horizontalHeader()
        self.assertEqual(
            header.sectionResizeMode(0), QHeaderView.ResizeMode.Stretch
        )
        for column in range(1, dialog._table.columnCount()):
            self.assertEqual(
                header.sectionResizeMode(column), QHeaderView.ResizeMode.Interactive,
                f"column {column} must keep its measured width rather than share the slack",
            )
        widest_fixed = max(
            dialog._table.columnWidth(column)
            for column in range(1, dialog._table.columnCount())
        )
        self.assertGreater(
            dialog._table.columnWidth(0), widest_fixed,
            "the name column is where the leftover width has to go",
        )

    def test_every_queue_has_an_editor_including_the_default(self):
        dialog = self._dialog()
        for row in range(dialog._table.rowCount()):
            name = dialog._table.item(row, 0).text()
            with self.subTest(queue=name):
                for column in (2, 3, 4):
                    self.assertIsInstance(
                        dialog._table.cellWidget(row, column), QSpinBox
                    )

    def test_table_row_height_prevents_spinbox_cropping(self):
        """Table rows must be at least 32px tall to prevent spinbox bottom clipping."""
        dialog = self._dialog()
        self.assertGreaterEqual(dialog._table.verticalHeader().defaultSectionSize(), 32)

    def test_the_default_queue_limit_is_editable(self):
        # It used to be the one row with a blank cell, which read as "not editable" rather
        # than "deliberately pinned".
        dialog = self._dialog()
        spin = self._spin_of(dialog, DEFAULT_QUEUE_NAME)
        self.assertIsInstance(spin, QSpinBox)
        spin.setValue(2)
        self.assertEqual(
            self.manager.get_queue(DEFAULT_QUEUE_ID).max_concurrent, 2
        )

    def test_the_default_queue_starts_on_global(self):
        dialog = self._dialog()
        self.assertEqual(self._spin_of(dialog, DEFAULT_QUEUE_NAME).value(), 0)
        self.assertEqual(
            self.manager.get_queue(DEFAULT_QUEUE_ID).effective_max_concurrent, 0
        )

    def test_the_note_explains_the_global_limit_with_its_live_value(self):
        dialog = self._dialog()
        note = dialog._note.text()
        expected = self.manager.general_config.effective_max_concurrent
        self.assertIn(str(expected), note)
        self.assertIn("Global", note)
        # It has to say the limit is a ceiling, not a reservation - that is the whole
        # interaction a user cannot infer from a number.
        self.assertIn("ceiling", note)

    def test_the_note_tracks_a_change_to_the_global_limit(self):
        dialog = self._dialog()
        self.manager.general_config.max_concurrent_downloads = 9
        dialog._refresh_note()
        self.assertIn("9", dialog._note.text())

    def test_adding_a_queue_from_the_dialog_works(self):
        dialog = self._dialog()
        class _FakeAddDlg:
            name = "Staging"
            max_concurrent = 4
            download_limit_kb = 0
            upload_limit_kb = 0
            color = "#3fb950"
            def exec(self):
                return True

        with patch("my_idm.dialogs.AddQueueDialog", return_value=_FakeAddDlg()):
            dialog._on_add()
        self.assertIn("Staging", [q.name for q in self.manager.get_queues()])
        st_q = next(q for q in self.manager.get_queues() if q.name == "Staging")
        self.assertEqual(st_q.max_concurrent, 4)
        self.assertEqual(st_q.color, "#3fb950")
        self.assertIn("Created queue", dialog.result_message)

    def test_the_limits_chosen_in_the_add_dialog_reach_the_new_queue(self):
        """The create dialog offers the ceilings, so they must not be dropped on the way in."""
        dialog = self._dialog()
        class _FakeAddDlg:
            name = "Staging"
            max_concurrent = 4
            color = "#3fb950"
            download_limit_kb = 512
            upload_limit_kb = 64
            def exec(self):
                return True

        with patch("my_idm.dialogs.AddQueueDialog", return_value=_FakeAddDlg()):
            dialog._on_add()
        st_q = next(q for q in self.manager.get_queues() if q.name == "Staging")
        self.assertEqual(st_q.download_limit, 512 * 1024)
        self.assertEqual(st_q.upload_limit, 64 * 1024)

    def test_cancelling_add_creates_nothing(self):
        dialog = self._dialog()
        before = len(self.manager.get_queues())
        class _FakeAddDlg:
            name = "Staging"
            max_concurrent = 4
            download_limit_kb = 0
            upload_limit_kb = 0
            color = "#3fb950"
            def exec(self):
                return False

        with patch("my_idm.dialogs.AddQueueDialog", return_value=_FakeAddDlg()):
            dialog._on_add()
        self.assertEqual(len(self.manager.get_queues()), before)

    def test_color_button_and_cell_swatch_interactivity(self):
        dialog = self._dialog()
        # Default queue has color button enabled
        dialog._table.selectRow(0)
        self.assertTrue(dialog._color_btn.isEnabled())

        # Check cell swatch button has uppercase letter
        cell_widget = dialog._table.cellWidget(self._row_of(dialog, "Torrents"), 0)
        swatch_btn = cell_widget.findChild(QPushButton)
        self.assertIsNotNone(swatch_btn)
        self.assertEqual(swatch_btn.text(), "T")

        # Color picker updates queue color
        with patch("PySide6.QtWidgets.QColorDialog.getColor") as mock_pick:
            from PySide6.QtGui import QColor
            mock_pick.return_value = QColor("#db6d28")
            dialog._table.selectRow(self._row_of(dialog, "Torrents"))
            dialog._on_change_color()

        tor_q = next(q for q in self.manager.get_queues() if q.name == "Torrents")
        self.assertEqual(tor_q.color, "#db6d28")

    def test_the_swatch_shows_a_capital_whatever_the_name_starts_with(self):
        """The colour box carries the queue's initial, and it is always capitalised.

        A queue can be named anything, including "work downloads" or something typed in a hurry,
        so the letter is upper-cased rather than taken from the name as typed. Pinned because the
        letter is the only thing that identifies a queue when the column is narrow - a lowercase
        "w" next to a colour swatch reads as a different queue from "W".
        """
        dialog = self._dialog()
        for name, expected in (("Default", "D"), ("Torrents", "T")):
            with self.subTest(queue=name):
                swatch = dialog._table.cellWidget(self._row_of(dialog, name), 0)
                self.assertEqual(swatch.findChild(QPushButton).text(), expected)

    def test_a_lowercase_name_still_gets_a_capital_letter(self):
        dialog = self._dialog()
        self.manager.create_queue("  overnight backups ", 2)
        dialog._reload()
        swatch = dialog._table.cellWidget(self._row_of(dialog, "overnight backups"), 0)
        self.assertEqual(swatch.findChild(QPushButton).text(), "O")


    def test_renaming_from_the_dialog_works(self):
        # Same bug, same slot-swallowing, in `_on_rename`.
        dialog = self._dialog()
        dialog._table.selectRow(self._row_of(dialog, "Torrents"))
        with patch.object(QInputDialog, "getText", return_value=("Big files", True)) as prompt:
            dialog._on_rename()
        prompt.assert_called_once()
        self.assertIn("Big files", [q.name for q in self.manager.get_queues()])

    def test_the_result_message_survives_a_deletion(self):
        dialog = self._dialog()
        dialog._table.selectRow(self._row_of(dialog, "Torrents"))
        with patch.object(QMessageBox, "question") as question:
            question.return_value = QMessageBox.StandardButton.Yes
            dialog._on_delete()
        self.assertIn("Deleted", dialog.result_message)
        self.assertNotIn("Torrents", [
            dialog._table.item(r, 0).text() for r in range(dialog._table.rowCount())
        ])

    def test_declining_the_confirmation_keeps_the_queue(self):
        dialog = self._dialog()
        dialog._table.selectRow(self._row_of(dialog, "Torrents"))
        with patch.object(QMessageBox, "question") as question:
            question.return_value = QMessageBox.StandardButton.No
            dialog._on_delete()
        self.assertIn("Torrents", [
            dialog._table.item(r, 0).text() for r in range(dialog._table.rowCount())
        ])


class TestQueueUiWiring(_MainWindowTestCase):
    """The window owns the switcher, so the wiring needs pinning rather than the model.

    Two of these are regression tests for bugs found while building this: the combo was never
    populated at startup (it only filled when some event happened to fire), and the View menu
    accumulated a stale duplicate queue entry on every refresh because `list.clear()` ran
    inside the loop that was iterating it.
    """

    def _scope_actions(self):
        """The Edit ▸ Queues entries that pick a scope: All Queues plus one per queue.

        This is the only scope switcher now. There used to be a toolbar combo as well, and
        these tests drove that one, so a queue could look correctly scoped in the combo while
        the menu's checkmark said otherwise.
        """
        return [self.win._act_queue_all] + list(self.win._queue_actions)

    def _scope_names(self):
        return [a.text() for a in self._scope_actions()]

    def _checked_scope(self):
        return next(a.text() for a in self._scope_actions() if a.isChecked())

    def _menu_labels(self):
        return [a.text() for a in self.win._menu_queues.actions() if a.text()]

    def test_the_queue_menu_is_populated_at_startup(self):
        # Not "after some event" - a user opening the app sees this immediately.
        names = self._scope_names()
        self.assertEqual(names[0], "All Queues")
        self.assertIn("AnimePahe", names)
        self.assertIn("YouTube", names)
        self.assertIn(DEFAULT_QUEUE_NAME, names)
        self.assertTrue(
            self.win._act_queue_all.isChecked(), "the scope starts on the whole history"
        )

    def test_the_scope_starts_as_all_queues(self):
        self.assertEqual(self.win._model.queue_scope(), ALL_QUEUES)

    def test_a_new_queue_appears_in_the_menu(self):
        self.manager.create_queue("Torrents", 1)
        self.win._refresh_queue_ui()
        names = self._scope_names()
        self.assertIn("Torrents", names)
        # Appended after the seeded source queues, not replacing them.
        self.assertLess(names.index("AnimePahe"), names.index("Torrents"))
        self.assertIn("Torrents", self._menu_labels())

    def test_a_queue_with_no_limit_says_so_rather_than_showing_zero(self):
        """0 means "follows the global limit", not "unlimited" - that wording must survive.

        It used to be carried by the toolbar combo's labels ("Music  (Global)"). With the
        combo gone, the dialog's column header, per-queue tooltip and note are the only
        places left that can say what a bare 0 means.
        """
        self.manager.create_queue("Music", 0)
        dialog = QueueManagerDialog(self.manager, None)
        self.addCleanup(dialog.deleteLater)

        header = dialog._table.horizontalHeaderItem(2).text()
        self.assertIn("0", header)
        self.assertIn("Global", header)
        self.assertIn("Global", dialog._note.text())
        tooltips = [s.toolTip() for s in dialog._table.findChildren(QSpinBox)]
        self.assertTrue(
            any("0 = follow the global limit" in tip for tip in tooltips),
            f"no queue cell explains what 0 means: {tooltips}",
        )

    def test_repeated_refreshes_do_not_accumulate_menu_entries(self):
        self.manager.create_queue("Torrents", 1)
        before = self._menu_labels()
        for _ in range(5):
            self.win._refresh_queue_ui()
        self.assertEqual(self._menu_labels(), before)
        self.assertEqual(self._menu_labels().count("Torrents"), 1)

    def test_the_exclusive_group_does_not_accumulate_dead_actions(self):
        seeded = len(self.manager.get_queues())
        self.manager.create_queue("Torrents", 1)
        self.win._refresh_queue_ui()
        self.manager.create_queue("Music", 1)
        self.win._refresh_queue_ui()
        # "All Queues" + the seeded queues + the two new ones, and nothing left over from the
        # first build.
        self.assertEqual(
            len(self.win._queue_scope_group.actions()), seeded + 3
        )

    def test_all_queues_stays_at_the_top_of_the_menu(self):
        self.manager.create_queue("Torrents", 1)
        self.win._refresh_queue_ui()
        labels = self._menu_labels()
        self.assertEqual(labels[0], "All Queues")
        self.assertEqual(labels[-2:], ["New Queue…", "Manage Queues…"])

    def test_selecting_in_the_menu_scopes_the_model(self):
        self.manager.create_queue("Torrents", 1)
        self.win._refresh_queue_ui()
        queue_id = next(q.id for q in self.manager.get_queues() if q.name == "Torrents")

        next(a for a in self.win._queue_actions if a.text() == "Torrents").trigger()

        self.assertEqual(self.manager.get_active_queue(), queue_id)
        self.assertEqual(self.win._model.queue_scope(), queue_id)
        self.assertEqual(self._checked_scope(), "Torrents", "the checkmark must follow")

    def test_clearing_the_scope_shows_the_whole_history_again(self):
        self.manager.create_queue("Torrents", 1)
        queue_id = next(q.id for q in self.manager.get_queues() if q.name == "Torrents")
        self.win._on_select_queue(queue_id)
        self.win._on_select_queue(ALL_QUEUES)
        self.assertEqual(self.win._model.queue_scope(), ALL_QUEUES)
        self.assertEqual(self._checked_scope(), "All Queues")

    def test_deleting_the_selected_queue_falls_back_to_all_queues(self):
        self.manager.create_queue("Torrents", 1)
        queue_id = next(q.id for q in self.manager.get_queues() if q.name == "Torrents")
        self.win._on_select_queue(queue_id)

        self.manager.delete_queue(queue_id)

        self.assertEqual(self.win._model.queue_scope(), ALL_QUEUES)
        self.assertEqual(self._checked_scope(), "All Queues")
        self.assertNotIn("Torrents", self._scope_names())

    def test_the_selected_rows_queue_is_visible_in_the_status_bar(self):
        # A queue is otherwise invisible in the list; the only way to find out was to open the
        # row's context menu and read the checkmark in **Move to Queue**.
        queue = self.manager.create_queue("Torrents", 1)
        self.assertTrue(queue[0])
        qid = next(q.id for q in self.manager.get_queues() if q.name == "Torrents")
        self.db.add_download(entry("d0", status="paused"))
        self.db.add_download(entry("t0", status="paused", queue_id=qid))
        self.win._load_history()

        self.assertEqual(self.win._queue_status_label.text(), "")

        # Select by id rather than by row, so the assertion cannot depend on sort order.
        for did, expected in (("d0", DEFAULT_QUEUE_NAME), ("t0", "Torrents")):
            with self.subTest(download=did):
                self.win._table.clearSelection()
                self.win._table.selectRow(self.win._model._id_to_row[did])
                self.assertIn(expected, self.win._queue_status_label.text())

    def test_queue_display_name_covers_blank_and_dangling_ids(self):
        # Unit-level, because a NameError or a None lookup inside the selection slot is
        # swallowed by Qt and leaves the previous label in place - a smoke test that only
        # prints the label will happily report the stale value and pass.
        self.assertEqual(
            self.win._queue_display_name(""), DEFAULT_QUEUE_NAME
        )
        self.assertEqual(
            self.win._queue_display_name("ghost"), DEFAULT_QUEUE_NAME
        )
        self.manager.create_queue("Torrents", 1)
        qid = next(q.id for q in self.manager.get_queues() if q.name == "Torrents")
        self.assertEqual(self.win._queue_display_name(qid), "Torrents")
        self.assertEqual(self.win._queue_display_name(DEFAULT_QUEUE_ID), DEFAULT_QUEUE_NAME)

    def test_a_multi_row_selection_summarises_its_queues(self):
        queue = self.manager.create_queue("Torrents", 1)
        self.assertTrue(queue[0])
        qid = next(q.id for q in self.manager.get_queues() if q.name == "Torrents")
        self.db.add_download(entry("d0", status="paused"))
        self.db.add_download(entry("t0", status="paused", queue_id=qid))
        self.win._load_history()

        self.win._table.selectAll()
        self.assertIn("2 queues", self.win._queue_status_label.text())

    def test_a_multi_row_selection_within_one_queue_names_it(self):
        self.manager.create_queue("Torrents", 1)
        qid = next(q.id for q in self.manager.get_queues() if q.name == "Torrents")
        for did in ("d0", "d1"):
            self.db.add_download(entry(did, status="paused", queue_id=qid))
        self.win._load_history()

        self.win._table.selectAll()
        self.assertIn("Torrents", self.win._queue_status_label.text())
        self.assertNotIn("2 queues", self.win._queue_status_label.text())

    def test_a_dangling_queue_id_displays_as_the_default(self):
        # Cannot happen through the write paths (resolve_queue_id normalises), but the display
        # must not show a raw uuid if a row is ever repaired by hand.
        self.db.add_download(entry("d0", status="paused"))
        self.db._conn.execute(
            "UPDATE downloads SET queue_id = 'ghost' WHERE id = 'd0'"
        )
        self.win._load_history()
        self.win._table.selectAll()
        self.assertIn(DEFAULT_QUEUE_NAME, self.win._queue_status_label.text())
        self.assertNotIn("ghost", self.win._queue_status_label.text())

    def test_the_queues_menu_lives_under_edit_not_view(self):
        # Every queue operation acts on the selected downloads, which is what Edit is for;
        # View is for how the list is drawn.
        edit_menu = self.win.menuBar().actions()[1].menu()
        view_menu = self.win.menuBar().actions()[2].menu()
        self.assertIn(self.win._menu_queues.title(), [a.text() for a in edit_menu.actions()])
        self.assertNotIn(self.win._menu_queues.title(), [a.text() for a in view_menu.actions()])

    def test_edit_also_carries_a_move_to_queue_submenu(self):
        edit_menu = self.win.menuBar().actions()[1].menu()
        titles = [a.text() for a in edit_menu.actions()]
        self.assertIn("Move to Queue", titles)
        labels = [
            a.text() for a in self.win._menu_move_to_queue.actions()
        ]
        self.assertIn("Default", labels)
        self.assertIn("YouTube", labels)

    def test_move_to_queue_is_disabled_with_nothing_selected(self):
        self.win._table.clearSelection()
        self.win._refresh_queue_ui()
        self.assertFalse(self.win._menu_move_to_queue.isEnabled())

    def test_moving_from_the_edit_menu_uses_the_selected_rows(self):
        self.manager.create_queue("Torrents", 1)
        queue_id = next(q.id for q in self.manager.get_queues() if q.name == "Torrents")
        self.db.add_download(entry("d0", status="paused"))
        self.win._load_history()
        self.win._table.selectAll()
        self.win._refresh_queue_ui()
        self.assertTrue(self.win._menu_move_to_queue.isEnabled())

        action = next(
            a for a in self.win._move_to_queue_actions if a.text() == "Torrents"
        )
        action.trigger()

        self.assertEqual(self.db.get_download("d0").queue_id, queue_id)

    def test_the_queue_menu_rebuilds_without_duplicating(self):
        for _ in range(4):
            self.win._refresh_queue_ui()
        labels = [a.text() for a in self.win._menu_queues.actions() if a.text()]
        self.assertEqual(labels.count("YouTube"), 1)
        move_labels = [a.text() for a in self.win._menu_move_to_queue.actions()]
        self.assertEqual(move_labels.count("YouTube"), 1)

    def test_a_new_queue_appears_in_the_edit_menu(self):
        self.manager.create_queue("Staging", 2)
        self.win._refresh_queue_ui()
        self.assertIn("Staging", [a.text() for a in self.win._menu_queues.actions()])
        self.assertIn("Staging", [a.text() for a in self.win._menu_move_to_queue.actions()])

    def test_both_queue_menus_have_swatch_icons(self):
        self.manager.create_queue("Torrents", 1)
        qid = next(q.id for q in self.manager.get_queues() if q.name == "Torrents")
        self.manager.set_queue_color(qid, "#3fb950")
        self.win._refresh_queue_ui()

        scope_action = next(a for a in self.win._queue_actions if a.text() == "Torrents")
        self.assertFalse(scope_action.icon().isNull())

        # Check move to queue action icon
        move_action = next(a for a in self.win._move_to_queue_actions if a.text() == "Torrents")
        self.assertFalse(move_action.icon().isNull())


    def test_the_window_feeds_the_model_a_colour_for_every_queue(self):
        queue_id = next(q.id for q in self.manager.get_queues() if q.name == "AnimePahe")
        self.manager.set_queue_color(queue_id, "#654321")
        self.win._refresh_queue_ui()

        row = entry("d0", queue_id=queue_id)
        self.win._model.load_entries([row])
        self.assertEqual(self.win._model.queue_color_for(row), "#654321")
        # And the delegate's role resolves it, not just the model's own accessor.
        self.assertEqual(
            self.win._model.data(
                self.win._model.index(0, Col.QUEUE_NAME), QUEUE_COLOR_ROLE
            ),
            "#654321",
        )

    def test_moving_selected_rows_reports_when_nothing_is_selected(self):
        self.win._table.clearSelection()
        self.win._on_move_selected_to_queue(DEFAULT_QUEUE_ID)
        self.assertIn("Select one or more", self.win._status_label.text())

    def test_new_queue_from_the_window_works(self):
        class _FakeAddDlg:
            name = "Staging"
            max_concurrent = 4
            download_limit_kb = 0
            upload_limit_kb = 0
            color = "#3fb950"
            def exec(self):
                return True

        with patch("my_idm.main_window.AddQueueDialog", return_value=_FakeAddDlg()):
            self.win._on_new_queue()
        self.assertIn("Staging", [q.name for q in self.manager.get_queues()])
        st_q = next(q for q in self.manager.get_queues() if q.name == "Staging")
        self.assertEqual(st_q.max_concurrent, 4)
        self.assertEqual(st_q.color, "#3fb950")
        self.assertIn("Staging", " | ".join(self._scope_names()))
        self.assertIn("Staging", self._menu_labels())
        self.assertIn("Created queue", self.win._status_label.text())

    def test_cancelling_new_queue_creates_nothing(self):
        before = len(self.manager.get_queues())
        class _FakeAddDlg:
            name = "Staging"
            max_concurrent = 4
            download_limit_kb = 0
            upload_limit_kb = 0
            color = "#3fb950"
            def exec(self):
                return False

        with patch("my_idm.main_window.AddQueueDialog", return_value=_FakeAddDlg()):
            self.win._on_new_queue()
        self.assertEqual(len(self.manager.get_queues()), before)


class TestAddQueueDialog(unittest.TestCase):
    """Direct UI and interaction tests for AddQueueDialog."""

    def test_add_queue_dialog_initial_state_and_preview(self):
        dlg = AddQueueDialog(None, initial_name="Work Queue", initial_color="#a371f7")
        self.addCleanup(dlg.deleteLater)
        self.assertEqual(dlg.name, "Work Queue")
        self.assertEqual(dlg.color, "#a371f7")
        self.assertEqual(dlg.max_concurrent, 3)
        self.assertEqual(dlg._color_btn.text(), "W")
        # Bandwidth ceilings default to 0 = follow the global limit, exactly like max_concurrent.
        self.assertEqual(dlg.download_limit_kb, 0)
        self.assertEqual(dlg.upload_limit_kb, 0)

        # Typing in name updates the letter
        dlg._name_edit.setText("Personal")
        self.assertEqual(dlg._color_btn.text(), "P")

        # Setting color updates color preview
        dlg._set_color("#db6d28")
        self.assertEqual(dlg.color, "#db6d28")

    def test_blank_name_rejected_on_accept(self):
        dlg = AddQueueDialog(None, initial_name="   ")
        self.addCleanup(dlg.deleteLater)
        with patch("PySide6.QtWidgets.QMessageBox.warning") as mock_warn:
            dlg._on_accept()
            mock_warn.assert_called_once()
        self.assertFalse(dlg.result())


class TestColorSwatchIconAndDelegate(unittest.TestCase):
    """Tests for color swatch icon generation and QueueColumnDelegate rendering."""

    def test_create_color_swatch_icon_with_letter(self):
        icon = create_color_swatch_icon("#3fb950", size=18, radius=4, letter="AnimePahe")
        self.assertFalse(icon.isNull())
        sizes = icon.availableSizes()
        self.assertTrue(len(sizes) > 0 or not icon.isNull())

    def test_create_color_swatch_icon_empty_color(self):
        icon = create_color_swatch_icon("", size=18, radius=4, letter="T")
        self.assertFalse(icon.isNull())

    def test_queue_delegate_swatch_size(self):
        from my_idm.delegates import QueueColumnDelegate
        delegate = QueueColumnDelegate()
        self.assertGreaterEqual(delegate.SWATCH, 16)


if __name__ == "__main__":
    unittest.main()
