"""Unit tests for Database schema, migrations, CRUD operations, queue order, and metadata persistence."""

import contextlib
import os
import sys
import tempfile
import threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
import sqlite3

from my_idm.database import Database, DownloadEntry, SegmentEntry, _MetadataDict


def _open_legacy_db(tmp_path: Path, columns_sql: str, rows=()):
    """Create a raw pre-migration sqlite file and hand back the path."""
    raw = sqlite3.connect(str(tmp_path))
    raw.row_factory = sqlite3.Row
    raw.execute(columns_sql)
    for row in rows:
        raw.execute(
            "INSERT INTO downloads (id, url, filename, status, total_size, "
            "downloaded_size) VALUES (?, ?, ?, 'paused', ?, ?)",
            row,
        )
    raw.commit()
    raw.close()
    return tmp_path


# A downloads table from before queue_order / torrent_info_hash / metadata_json existed.
_LEGACY_SCHEMA = """
    CREATE TABLE downloads (
        id              TEXT PRIMARY KEY,
        url             TEXT NOT NULL,
        filename        TEXT NOT NULL DEFAULT '',
        save_path       TEXT NOT NULL DEFAULT '',
        file_path       TEXT NOT NULL DEFAULT '',
        total_size      INTEGER NOT NULL DEFAULT 0,
        downloaded_size INTEGER NOT NULL DEFAULT 0,
        status          TEXT NOT NULL DEFAULT 'queued',
        download_type   TEXT NOT NULL DEFAULT 'http',
        num_segments    INTEGER NOT NULL DEFAULT 8,
        error_message   TEXT NOT NULL DEFAULT '',
        retry_count     INTEGER NOT NULL DEFAULT 0,
        max_retries     INTEGER NOT NULL DEFAULT 5,
        added_at        TEXT NOT NULL DEFAULT '',
        last_tried_at   TEXT NOT NULL DEFAULT '',
        completed_at    TEXT NOT NULL DEFAULT '',
        etag            TEXT NOT NULL DEFAULT '',
        content_hash    TEXT NOT NULL DEFAULT ''
    );
"""


class _TempDbMixin:
    """Per-test scratch directory plus a database handle closed on cleanup.

    Registering the cleanups with ``addCleanup`` (rather than a ``tearDown``
    that closes only on the success path) means a failing assert can no longer
    leave an open sqlite handle -- which on Windows makes the ``.db`` file
    undeletable and orphans it in %TEMP% forever.
    """

    def make_temp_db(self, name="test.db"):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        db_path = Path(tmp_dir.name) / name
        self.addCleanup(self._unlink_strict, db_path)
        return db_path

    @staticmethod
    def _unlink_strict(path: Path):
        """Remove the database file, surfacing a genuinely stuck handle.

        The old code swallowed every exception, which hid a leaked connection
        behind a silently orphaned file. Cleanup failures are re-raised so a
        real leak is visible instead of invisible.
        """
        for suffix in ("", "-wal", "-shm"):
            target = Path(str(path) + suffix)
            if not target.exists():
                continue
            try:
                os.remove(target)
            except PermissionError as exc:
                raise AssertionError(
                    f"{target} could not be removed; a sqlite handle is still open"
                ) from exc
        if path.exists():  # pragma: no cover - defensive
            raise AssertionError(f"{path} still exists after removal")

    def open_db(self, path):
        db = Database(path)
        db.open()
        self.addCleanup(db.close)
        return db

    def legacy_db(self, rows=(), columns_sql=_LEGACY_SCHEMA, name="legacy.db"):
        """Create a raw pre-migration sqlite file and return its path.

        The file lives in the per-test scratch directory, and the mixin's
        ``_unlink_strict`` cleanup removes it (loudly, on failure) whether or
        not the test passed.
        """
        return _open_legacy_db(self.make_temp_db(name), columns_sql, rows)


class TestDatabase(_TempDbMixin, unittest.TestCase):

    def setUp(self):
        self.db = self.open_db(self.make_temp_db())

    def test_add_and_get_download(self):
        """Adding a download persists all fields and retrieves it accurately."""
        entry = DownloadEntry(
            id="dl-1",
            url="https://example.com/file.zip",
            filename="file.zip",
            save_path="/downloads",
            total_size=1024,
            download_type="http",
            status="downloading",
        )
        self.db.add_download(entry)

        retrieved = self.db.get_download("dl-1")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.id, "dl-1")
        self.assertEqual(retrieved.url, "https://example.com/file.zip")
        self.assertEqual(retrieved.filename, "file.zip")
        self.assertEqual(retrieved.save_path, "/downloads")
        self.assertEqual(retrieved.total_size, 1024)
        self.assertEqual(retrieved.status, "downloading")
        self.assertEqual(retrieved.queue_order, 1)

    def test_queue_order_auto_increment(self):
        """New downloads automatically receive incrementing queue order positions."""
        e1 = DownloadEntry(id="d1", url="http://example.com/1.zip", filename="1.zip", save_path="/tmp")
        e2 = DownloadEntry(id="d2", url="http://example.com/2.zip", filename="2.zip", save_path="/tmp")
        e3 = DownloadEntry(id="d3", url="http://example.com/3.zip", filename="3.zip", save_path="/tmp")
        self.db.add_download(e1)
        self.db.add_download(e2)
        self.db.add_download(e3)

        self.assertEqual(self.db.get_download("d1").queue_order, 1)
        self.assertEqual(self.db.get_download("d2").queue_order, 2)
        self.assertEqual(self.db.get_download("d3").queue_order, 3)

    def test_swap_queue_order(self):
        """Swapping queue orders updates both downloads atomically."""
        e1 = DownloadEntry(id="d1", url="http://example.com/1.zip", filename="1.zip", save_path="/tmp")
        e2 = DownloadEntry(id="d2", url="http://example.com/2.zip", filename="2.zip", save_path="/tmp")
        self.db.add_download(e1)
        self.db.add_download(e2)

        self.db.swap_queue_order("d1", "d2")
        self.assertEqual(self.db.get_download("d1").queue_order, 2)
        self.assertEqual(self.db.get_download("d2").queue_order, 1)

    def test_update_download(self):
        """Updating download entry fields persists changes."""
        entry = DownloadEntry(id="dl-up", url="https://example.com/up.bin", filename="up.bin", save_path="/tmp")
        self.db.add_download(entry)

        entry.status = "completed"
        entry.downloaded_size = 5000
        entry.total_size = 5000
        self.db.update_download(entry)

        updated = self.db.get_download("dl-up")
        self.assertEqual(updated.status, "completed")
        self.assertEqual(updated.downloaded_size, 5000)

    def test_delete_download(self):
        """Deleting a download removes it and cascades to segments."""
        entry = DownloadEntry(id="dl-del", url="https://example.com/del.bin", filename="del.bin", save_path="/tmp")
        self.db.add_download(entry)

        seg = SegmentEntry(id="seg-1", download_id="dl-del", index=0, start_byte=0, end_byte=100, downloaded_bytes=100, status="completed")
        self.db.add_segments([seg])

        self.assertIsNotNone(self.db.get_download("dl-del"))
        self.assertEqual(len(self.db.get_segments("dl-del")), 1)

        self.db.delete_download("dl-del")
        self.assertIsNone(self.db.get_download("dl-del"))
        self.assertEqual(len(self.db.get_segments("dl-del")), 0)

    def test_segments_operations(self):
        """Adding, getting, updating, and clearing segments."""
        entry = DownloadEntry(id="dl-seg", url="https://example.com/seg.bin", filename="seg.bin", save_path="/tmp")
        self.db.add_download(entry)

        segs = [
            SegmentEntry(id="s1", download_id="dl-seg", index=0, start_byte=0, end_byte=499, downloaded_bytes=200, status="downloading"),
            SegmentEntry(id="s2", download_id="dl-seg", index=1, start_byte=500, end_byte=999, downloaded_bytes=0, status="pending"),
        ]
        self.db.add_segments(segs)

        fetched = self.db.get_segments("dl-seg")
        self.assertEqual(len(fetched), 2)
        self.assertEqual(fetched[0].downloaded_bytes, 200)

        self.db.update_segment("s1", 500, "completed")
        fetched = self.db.get_segments("dl-seg")
        self.assertEqual(fetched[0].downloaded_bytes, 500)
        self.assertEqual(fetched[0].status, "completed")

        self.db.delete_segments("dl-seg")
        self.assertEqual(len(self.db.get_segments("dl-seg")), 0)

    def test_ui_state_persistence(self):
        """Saving and loading window/UI state via ui_state table."""
        self.assertEqual(self.db.get_window_state(), {})

        state_dict = {
            "width": 1280,
            "height": 720,
            "is_maximized": False,
            "sort_column": 9,
            "details_visible": True,
        }
        self.db.save_window_state(state_dict)

        loaded = self.db.get_window_state()
        self.assertEqual(loaded, state_dict)

    def test_metadata_dict_auto_sync_and_bytes_handling(self):
        """Mutating entry.metadata automatically serializes to metadata_json and safely converts bytes."""
        entry = DownloadEntry(id="meta-1", url="http://example.com/m.zip", filename="m.zip", save_path="/tmp")
        # Mutating dictionary
        entry.metadata["files"] = [{"name": "file1.txt", "size": 100}]
        entry.metadata["peers"] = [{"ip": "1.2.3.4", "client": b"qBittorrent/4.5"}]

        # Must auto-sync to metadata_json as clean JSON string
        self.assertIn('"file1.txt"', entry.metadata_json)
        self.assertIn('"qBittorrent/4.5"', entry.metadata_json)

        self.db.add_download(entry)
        saved = self.db.get_download("meta-1")
        self.assertEqual(saved.metadata["files"][0]["name"], "file1.txt")
        self.assertEqual(saved.metadata["peers"][0]["client"], "qBittorrent/4.5")

    def test_database_migration_from_legacy_schema(self):
        """Databases created with older schemas lacking newer columns migrate cleanly without crash."""
        tmp_path = self.legacy_db()
        raw_conn = sqlite3.connect(str(tmp_path))
        raw_conn.execute(
            "INSERT INTO downloads (id, url, filename) VALUES "
            "('legacy-1', 'http://example.com/leg.zip', 'leg.zip')"
        )
        raw_conn.commit()
        raw_conn.close()

        # Open via Database — must not throw OperationalError
        db = self.open_db(tmp_path)

        # Verify migrated fields
        download = db.get_download("legacy-1")
        self.assertIsNotNone(download)
        self.assertEqual(download.id, "legacy-1")
        self.assertEqual(download.queue_order, 0)
        self.assertEqual(download.torrent_info_hash, "")
        self.assertEqual(download.metadata, {})

        # Verify subsequent adds work and assign queue order
        new_e = DownloadEntry(id="legacy-2", url="http://example.com/leg2.zip", filename="leg2.zip", save_path="/tmp")
        db.add_download(new_e)
        self.assertEqual(db.get_download("legacy-2").queue_order, 1)

    def test_completed_download_progress_always_100_percent(self):
        """Completed or seeding downloads must always report 100% progress."""
        entry = DownloadEntry(
            id="comp-1",
            url="https://example.com/comp.zip",
            filename="comp.zip",
            save_path="/tmp",
            total_size=5000,
            downloaded_size=0,
            status="completed",
        )
        self.assertEqual(entry.progress, 100.0)

        entry_seeding = DownloadEntry(
            id="seed-1",
            url="magnet:?xt=urn:btih:123",
            filename="seed.torrent",
            save_path="/tmp",
            total_size=10000,
            downloaded_size=0,
            status="seeding",
        )
        self.assertEqual(entry_seeding.progress, 100.0)

    def test_database_init_self_heals_completed_downloaded_size(self):
        """On Database open, completed entries with 0 or partial downloaded_size are self-healed."""
        # Insert a completed row with downloaded_size = 0 directly into SQLite
        self.db._conn.execute(
            "INSERT INTO downloads (id, url, filename, total_size, downloaded_size, status) "
            "VALUES ('corrupt-1', 'https://example.com/test.zip', 'test.zip', 4096, 0, 'completed')"
        )
        self.db._conn.commit()

        # Re-open database to simulate application restart
        self.db.close()
        self.db.open()

        healed = self.db.get_download("corrupt-1")
        self.assertIsNotNone(healed)
        self.assertEqual(healed.downloaded_size, 4096)
        self.assertEqual(healed.total_size, 4096)
        self.assertEqual(healed.progress, 100.0)


    def test_migration_adds_last_seeded_without_touching_existing_rows(self):
        """last_seeded_at is appended to legacy DBs; no existing row is modified."""
        rows = [
            # 'paused' with a partial downloaded_size is deliberate: Database.open()
            # self-heals completed/seeding rows whose downloaded_size is incomplete,
            # which would otherwise mask whether this migration touched the data.
            (f"row-{i}", f"http://example.com/{i}.zip", f"{i}.zip", 2000 + i, 500 + i)
            for i in range(3)
        ]
        tmp_path = self.legacy_db(rows=rows)
        with contextlib.closing(sqlite3.connect(str(tmp_path))) as raw:
            raw.row_factory = sqlite3.Row
            before_cols = [r["name"] for r in raw.execute("PRAGMA table_info(downloads)")]
            before_rows = {
                r["id"]: dict(r) for r in raw.execute("SELECT * FROM downloads").fetchall()
            }

        self.assertNotIn("last_seeded_at", before_cols)

        db = self.open_db(tmp_path)
        after_cols = [r["name"] for r in db._conn.execute("PRAGMA table_info(downloads)")]

        # Columns added, and appended rather than inserted: every pre-existing
        # column keeps its relative order and the new ones land after them.
        self.assertIn("last_seeded_at", after_cols)
        self.assertIn("seeding_started_at", after_cols)
        self.assertLess(
            after_cols.index("last_seeded_at"),
            after_cols.index("seeding_started_at"),
            "columns are appended in declaration order",
        )
        self.assertEqual(after_cols[: len(before_cols)], before_cols)

        entries = {e.id: e for e in db.get_all_downloads()}
        self.assertEqual(len(entries), 3)
        for eid, pre in before_rows.items():
            entry = entries[eid]
            # New column defaults to empty for pre-existing rows.
            self.assertEqual(entry.last_seeded_at, "", eid)
            # Every pre-existing value survives untouched.
            for col, old in pre.items():
                self.assertEqual(getattr(entry, col), old, f"{eid}.{col}")

        # The new column round-trips once written.
        entries["row-0"].last_seeded_at = "2026-01-02T03:04:05+00:00"
        db.update_download(entries["row-0"])
        self.assertEqual(
            db.get_download("row-0").last_seeded_at, "2026-01-02T03:04:05+00:00"
        )
        self.assertEqual(db.get_download("row-1").last_seeded_at, "")

        # Re-opening is idempotent.
        db.close()
        db2 = self.open_db(tmp_path)
        self.assertEqual(db2.get_download("row-0").last_seeded_at, "2026-01-02T03:04:05+00:00")
        self.assertEqual(len(db2.get_all_downloads()), 3)

    def test_last_seeded_defaults_and_round_trip(self):
        db = self.open_db(":memory:")
        entry = DownloadEntry(id="seed-1", url="magnet:?xt=urn:btih:abc", filename="a.torrent")
        self.assertEqual(entry.last_seeded_at, "")
        self.assertEqual(entry.seeding_started_at, "")
        db.add_download(entry)
        self.assertEqual(db.get_download("seed-1").last_seeded_at, "")
        self.assertEqual(db.get_download("seed-1").seeding_started_at, "")

        entry.last_seeded_at = "2026-05-06T07:08:09+00:00"
        entry.seeding_started_at = "2026-05-06T07:09:10+00:00"
        db.update_download(entry)
        reloaded = db.get_download("seed-1")
        self.assertEqual(reloaded.last_seeded_at, "2026-05-06T07:08:09+00:00")
        self.assertEqual(reloaded.seeding_started_at, "2026-05-06T07:09:10+00:00")


class TestDatabaseThreadSafety(_TempDbMixin, unittest.TestCase):
    """Regression tests for the connection shared by the GUI and worker threads.

    ``Database.open()`` uses ``check_same_thread=False`` because the Qt GUI thread and
    the asyncio / yt-dlp / antivirus worker threads all share one connection. Python's
    sqlite3 module serialises at the C level but not the Python-level transaction
    bookkeeping, so before the connection was wrapped in a lock these tests failed with
    ``sqlite3.OperationalError: cannot start a transaction within a transaction`` and
    ``sqlite3.ProgrammingError: Cannot operate on a closed database``.
    """

    def _seed(self, db, count=4):
        ids = []
        for i in range(count):
            entry = DownloadEntry(id=f"c-{i}", url=f"https://example.com/{i}.bin", filename=f"f{i}.bin")
            db.add_download(entry)
            ids.append(entry.id)
        return ids

    def test_concurrent_writes_from_many_threads_do_not_collide(self):
        """Every writer must succeed; a lost or interleaved transaction is a failure."""
        db = self.open_db(self.make_temp_db("concurrent.db"))
        ids = self._seed(db, count=4)
        errors = []
        start = threading.Barrier(8)

        def writer(download_id):
            try:
                start.wait(timeout=10)
                for step in range(60):
                    db.update_progress(download_id, step * 10, 1000)
                    db.update_status(download_id, "downloading")
            except BaseException as exc:  # noqa: BLE001 - the point is to record anything
                errors.append(f"{download_id}: {type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=writer, args=(ids[i % len(ids)],)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual([t.name for t in threads if t.is_alive()], [],
                         "a writer thread never finished; the connection lock deadlocked")
        self.assertEqual(errors, [], "concurrent writes raised; the connection is not serialised")

        for download_id in ids:
            self.assertIsNotNone(db.get_download(download_id), f"{download_id} vanished")

    def test_concurrent_reads_and_writes_do_not_collide(self):
        """Readers share the connection with writers and must not see a torn cursor."""
        db = self.open_db(self.make_temp_db("rw.db"))
        ids = self._seed(db, count=2)
        errors = []

        def reader():
            try:
                for _ in range(80):
                    for download_id in ids:
                        self.assertIsNotNone(db.get_download(download_id))
                    db.get_all_downloads()
            except BaseException as exc:  # noqa: BLE001
                errors.append(f"reader: {type(exc).__name__}: {exc}")

        def writer(download_id):
            try:
                for step in range(80):
                    db.update_progress(download_id, step, 1000)
            except BaseException as exc:  # noqa: BLE001
                errors.append(f"writer: {type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=reader) for _ in range(3)]
        threads += [threading.Thread(target=writer, args=(ids[i],)) for i in range(len(ids))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(errors, [], "a reader/writer overlap raised; reads are not serialised")

    def test_close_waits_for_an_in_flight_write_instead_of_raising(self):
        """close() must not land underneath a write another thread already started."""
        db = self.open_db(self.make_temp_db("closing.db"))
        download_id = self._seed(db, count=1)[0]
        started = threading.Event()
        errors = []

        def slow_writer():
            try:
                # Hold the connection for long enough that a close() certainly overlaps.
                for step in range(200):
                    db.update_progress(download_id, step, 1000)
                    if step == 2:
                        started.set()
            except BaseException as exc:  # noqa: BLE001
                errors.append(f"writer: {type(exc).__name__}: {exc}")

        thread = threading.Thread(target=slow_writer)
        thread.start()
        self.assertTrue(started.wait(timeout=10), "writer thread never started")
        db.close()  # must block until the in-flight write finishes, not raise
        thread.join(timeout=60)

        self.assertEqual(errors, [], "close() raced an in-flight write")

    def test_connection_context_manager_still_commits_and_rolls_back(self):
        """The lock wrapper must not swallow sqlite3's transaction semantics."""
        db = self.open_db(self.make_temp_db("txn.db"))
        download_id = self._seed(db, count=1)[0]

        with db._conn:
            db._conn.execute(
                "UPDATE downloads SET error_message = ? WHERE id = ?", ("committed", download_id)
            )
        self.assertEqual(db.get_download(download_id).error_message, "committed",
                         "a clean transaction context must commit")

        with contextlib.suppress(Exception):
            with db._conn:
                db._conn.execute(
                    "UPDATE downloads SET error_message = ? WHERE id = ?", ("rolled-back", download_id)
                )
                raise RuntimeError("force rollback")
        self.assertEqual(db.get_download(download_id).error_message, "committed",
                         "a failing transaction context must roll back")

    def test_same_thread_read_inside_a_write_does_not_self_deadlock(self):
        """The lock is reentrant, so one method may read and write on one thread."""
        db = self.open_db(self.make_temp_db("reentrant.db"))
        download_id = self._seed(db, count=1)[0]
        done = threading.Event()

        def worker():
            try:
                with db._conn:
                    db._conn.execute(
                        "UPDATE downloads SET error_message = 'x' WHERE id = ?", (download_id,)
                    )
                    assert db.get_download(download_id) is not None
            finally:
                done.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self.assertTrue(done.wait(timeout=10),
                        "a nested read inside a write on one thread deadlocked")


if __name__ == "__main__":
    unittest.main()
