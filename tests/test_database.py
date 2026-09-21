"""Unit tests for Database schema, migrations, CRUD operations, queue order, and metadata persistence."""

import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
import sqlite3

from my_idm.database import Database, DownloadEntry, SegmentEntry, _MetadataDict


class TestDatabase(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test.db"
        self.db = Database(self.db_path)
        self.db.open()

    def tearDown(self):
        self.db.close()
        self.tmp_dir.cleanup()

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
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        tmp_path = Path(tmp.name)

        try:
            # Create a legacy table without queue_order, torrent_info_hash, metadata_json
            raw_conn = sqlite3.connect(str(tmp_path))
            raw_conn.execute("""
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
            """)
            raw_conn.execute("INSERT INTO downloads (id, url, filename) VALUES ('legacy-1', 'http://example.com/leg.zip', 'leg.zip')")
            raw_conn.commit()
            raw_conn.close()

            # Open via Database — must not throw OperationalError
            db = Database(tmp_path)
            db.open()

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

            db.close()
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

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


if __name__ == "__main__":
    unittest.main()
