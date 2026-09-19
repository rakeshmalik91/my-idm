"""Tests for Batch 1 fixes and enhancements from TODO.md."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from my_idm.config import GeneralConfig
from my_idm.database import Database, DownloadEntry
from my_idm.dialogs import AddDownloadDialog
from my_idm.download_model import Col, DownloadTableModel
from my_idm.manager import DownloadManager


class TestBatch1Fixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not QApplication.instance():
            cls.app = QApplication([])
        else:
            cls.app = QApplication.instance()

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp_dir.name) / "test.db")
        self.db.open()
        self.manager = DownloadManager(self.db)
        QSettings("MyIDM", "My-IDM").clear()

    def tearDown(self):
        self.manager.stop()
        self.db.close()
        self.tmp_dir.cleanup()
        QSettings("MyIDM", "My-IDM").clear()

    def test_add_download_picks_default_folder(self):
        """Add download dialog picks default_save_path even when remember_last is default."""
        with tempfile.TemporaryDirectory() as custom_dir:
            cfg = GeneralConfig(default_save_path=custom_dir)
            cfg.save()

            dlg = AddDownloadDialog()
            self.assertEqual(dlg.save_path, custom_dir)
            self.assertEqual(dlg._save_edit.text(), custom_dir)

    def test_torrent_speed_shows_down_and_up(self):
        """Torrent speed column shows both down and up speed."""
        model = DownloadTableModel()
        entry = DownloadEntry(
            id="t1",
            filename="ubuntu.iso",
            url="magnet:?xt=urn:btih:abc",
            download_type="torrent",
            status="downloading",
            speed=1048576.0,      # 1 MB/s
            upload_speed=262144.0, # 256 KB/s
        )
        model.load_entries([entry])

        idx = model.index(0, Col.SPEED)
        display_val = model.data(idx, Qt.ItemDataRole.DisplayRole)
        self.assertIn("↓", display_val)
        self.assertIn("↑", display_val)
        self.assertIn("1.0 MiB/s", display_val)
        self.assertIn("256.0 KiB/s", display_val)

    def test_aggregate_speeds(self):
        """Model calculates aggregate download and upload speed."""
        model = DownloadTableModel()
        e1 = DownloadEntry(id="1", status="downloading", speed=1000.0, upload_speed=100.0)
        e2 = DownloadEntry(id="2", status="downloading", speed=2000.0, upload_speed=200.0)
        e3 = DownloadEntry(id="3", status="seeding", speed=0.0, upload_speed=300.0)
        e4 = DownloadEntry(id="4", status="paused", speed=500.0, upload_speed=500.0)

        model.load_entries([e1, e2, e3, e4])
        down, up = model.get_aggregate_speeds()
        self.assertEqual(down, 3000.0)
        self.assertEqual(up, 600.0)

    def test_antivirus_preserves_incomplete_status(self):
        """Scanning an incomplete file does not mark it completed."""
        test_file = Path(self.tmp_dir.name) / "incomplete.bin"
        test_file.write_bytes(b"partial content")

        entry = DownloadEntry(
            id="d1",
            url="https://example.com/incomplete.bin",
            filename="incomplete.bin",
            file_path=str(test_file),
            save_path=self.tmp_dir.name,
            total_size=1000000,
            downloaded_size=len(b"partial content"),
            status="paused",
        )
        self.db.add_download(entry)

        with patch("my_idm.manager.scan_file", return_value=(True, "Clean file")):
            self.manager.scan_download_file("d1")
            # Wait for background thread
            import time
            time.sleep(0.3)

            updated = self.db.get_download("d1")
            self.assertEqual(updated.status, "paused")
            self.assertNotEqual(updated.status, "completed")

    def test_torrent_recheck_transitions_out_of_checking(self):
        """When checking completes with 0 peers/seeds, status transitions to downloading (not stuck at checking)."""
        from my_idm.torrent_engine import TorrentEngine
        te = TorrentEngine(self.db)
        te._running = True
        te._session = MagicMock()

        entry = DownloadEntry(
            id="t_check",
            download_type="torrent",
            status="checking",
            total_size=10000,
            downloaded_size=0,
        )
        self.db.add_download(entry)

        mock_handle = MagicMock()
        mock_handle.status.return_value.is_paused = False
        te._handles["t_check"] = mock_handle

        status_updates = []
        te.set_callbacks(None, lambda did, stat, err: status_updates.append((did, stat)))

        with patch.object(te, "get_status", return_value={
            "total_size": 10000,
            "downloaded": 0,
            "progress": 0.0,
            "state": "downloading",
            "speed": 0.0,
            "upload_speed": 0.0,
            "seeds": 0,
            "peers": 0,
            "eta": 0,
            "name": "test_torrent",
        }):
            te.poll_all()

        updated = self.db.get_download("t_check")
        self.assertEqual(updated.status, "downloading")
        self.assertEqual(status_updates, [("t_check", "downloading")])

    def test_copy_url_to_clipboard(self):
        """MainWindow._on_copy_url successfully copies URL/magnet without NameError."""
        from my_idm.main_window import MainWindow
        from PySide6.QtGui import QGuiApplication

        cb = QGuiApplication.clipboard()
        orig = cb.text() if cb else ""

        entry = DownloadEntry(
            id="test_copy",
            url="magnet:?xt=urn:btih:fedcba9876543210&dn=real_movie",
            filename="real_movie",
            save_path=tempfile.gettempdir(),
            download_type="torrent",
            status="downloading",
        )
        self.db.add_download(entry)

        win = MainWindow(self.manager)
        try:
            win._table.selectRow(0)
            win._on_copy_url()
            clipboard = QGuiApplication.clipboard()
            self.assertEqual(clipboard.text(), entry.url)
            self.assertIn("Copied Magnet link", win._status_label.text())
        finally:
            win.close()
            if cb:
                cb.setText(orig)

    # -- recheck tests -------------------------------------------------------

    def test_recheck_file_not_found_resets_progress_to_zero(self):
        """Recheck with deleted file: resets downloaded_size to 0, status to queued,
        and emits both status_changed and progress_updated signals."""
        test_file = Path(self.tmp_dir.name) / "deleted.zip"
        # Create a 'completed' entry pointing to a file that doesn't exist
        entry = DownloadEntry(
            id="recheck-deleted",
            url="https://example.com/deleted.zip",
            filename="deleted.zip",
            file_path=str(test_file),
            save_path=self.tmp_dir.name,
            total_size=1_000_000,
            downloaded_size=1_000_000,
            status="completed",
            download_type="http",
        )
        self.db.add_download(entry)

        status_events = []
        progress_events = []
        self.manager.status_changed.connect(lambda did, st, err: status_events.append((did, st, err)))
        self.manager.progress_updated.connect(lambda did, dl, total, *_: progress_events.append((did, dl, total)))

        # File does NOT exist on disk — recheck should reset
        self.manager.recheck_download("recheck-deleted")

        # Check signals
        self.assertEqual(len(status_events), 1)
        self.assertEqual(status_events[0], ("recheck-deleted", "queued", "File not found"))

        self.assertEqual(len(progress_events), 1)
        self.assertEqual(progress_events[0][0], "recheck-deleted")
        self.assertEqual(progress_events[0][1], 0)  # downloaded = 0

        # Check DB
        updated = self.db.get_download("recheck-deleted")
        self.assertEqual(updated.downloaded_size, 0)
        self.assertEqual(updated.status, "queued")

    def test_recheck_partial_file_updates_progress(self):
        """Recheck with a partial file (pre-allocated, segments partially written):
        reports actual written bytes from segment records, not file size."""
        from my_idm.database import SegmentEntry

        test_file = Path(self.tmp_dir.name) / "partial.bin"
        # Simulate pre-allocation: file on disk is FULL SIZE but data is only 50% written
        test_file.write_bytes(b"\x00" * 1_000_000)

        entry = DownloadEntry(
            id="recheck-partial",
            url="https://example.com/partial.bin",
            filename="partial.bin",
            file_path=str(test_file),
            save_path=self.tmp_dir.name,
            total_size=1_000_000,
            downloaded_size=1_000_000,  # DB incorrectly says complete
            status="completed",
            download_type="http",
        )
        self.db.add_download(entry)

        # Add segment records reflecting only 500 KB actually written
        segs = [
            SegmentEntry(id="s1", download_id="recheck-partial", index=0,
                         start_byte=0, end_byte=499_999,
                         downloaded_bytes=500_000, status="completed"),
            SegmentEntry(id="s2", download_id="recheck-partial", index=1,
                         start_byte=500_000, end_byte=999_999,
                         downloaded_bytes=0, status="pending"),  # not written
        ]
        self.db.add_segments(segs)

        status_events = []
        progress_events = []
        self.manager.status_changed.connect(lambda did, st, err: status_events.append((did, st, err)))
        self.manager.progress_updated.connect(lambda did, dl, total, *_: progress_events.append((did, dl, total)))

        self.manager.recheck_download("recheck-partial")

        # status_changed: was completed, now paused (partial)
        self.assertEqual(len(status_events), 1)
        self.assertEqual(status_events[0][0], "recheck-partial")
        self.assertEqual(status_events[0][1], "paused")

        # progress_updated: actual bytes from segments = 500_000, NOT 1_000_000
        self.assertEqual(len(progress_events), 1)
        self.assertEqual(progress_events[0][1], 500_000)

        updated = self.db.get_download("recheck-partial")
        self.assertEqual(updated.downloaded_size, 500_000)
        self.assertEqual(updated.status, "paused")

    def test_recheck_preallocated_full_file_with_partial_segments_not_completed(self):
        """Regression: pre-allocated file has st_size == total_size, but segments
        show partial download. Recheck must NOT mark it completed."""
        from my_idm.database import SegmentEntry

        test_file = Path(self.tmp_dir.name) / "preallocated.zip"
        # File is pre-allocated to full size (mimics HTTP engine behaviour)
        test_file.write_bytes(b"\x00" * 2_000_000)

        entry = DownloadEntry(
            id="recheck-prealloc",
            url="https://example.com/preallocated.zip",
            filename="preallocated.zip",
            file_path=str(test_file),
            save_path=self.tmp_dir.name,
            total_size=2_000_000,
            downloaded_size=800_000,
            status="paused",
            download_type="http",
        )
        self.db.add_download(entry)

        segs = [
            SegmentEntry(id="p1", download_id="recheck-prealloc", index=0,
                         start_byte=0, end_byte=999_999,
                         downloaded_bytes=800_000, status="pending"),
            SegmentEntry(id="p2", download_id="recheck-prealloc", index=1,
                         start_byte=1_000_000, end_byte=1_999_999,
                         downloaded_bytes=0, status="pending"),
        ]
        self.db.add_segments(segs)

        status_events = []
        self.manager.status_changed.connect(lambda did, st, err: status_events.append((did, st, err)))

        self.manager.recheck_download("recheck-prealloc")

        # Must NOT be completed — only 800 KB of 2 MB written
        self.assertEqual(len(status_events), 1)
        self.assertNotEqual(status_events[0][1], "completed")
        self.assertEqual(status_events[0][1], "paused")

        updated = self.db.get_download("recheck-prealloc")
        self.assertNotEqual(updated.status, "completed")
        self.assertEqual(updated.downloaded_size, 800_000)

    def test_recheck_complete_file_confirms_completed(self):
        """Recheck with all segments completed: confirms completed status."""
        from my_idm.database import SegmentEntry

        test_file = Path(self.tmp_dir.name) / "complete.bin"
        test_file.write_bytes(b"B" * 1_000_000)

        entry = DownloadEntry(
            id="recheck-complete",
            url="https://example.com/complete.bin",
            filename="complete.bin",
            file_path=str(test_file),
            save_path=self.tmp_dir.name,
            total_size=1_000_000,
            downloaded_size=900_000,  # DB says incomplete
            status="paused",
            download_type="http",
        )
        self.db.add_download(entry)

        # All segments completed
        segs = [
            SegmentEntry(id="c1", download_id="recheck-complete", index=0,
                         start_byte=0, end_byte=499_999,
                         downloaded_bytes=500_000, status="completed"),
            SegmentEntry(id="c2", download_id="recheck-complete", index=1,
                         start_byte=500_000, end_byte=999_999,
                         downloaded_bytes=500_000, status="completed"),
        ]
        self.db.add_segments(segs)

        status_events = []
        progress_events = []
        self.manager.status_changed.connect(lambda did, st, err: status_events.append((did, st, err)))
        self.manager.progress_updated.connect(lambda did, dl, total, *_: progress_events.append((did, dl, total)))

        self.manager.recheck_download("recheck-complete")

        # Should confirm completed
        self.assertEqual(len(status_events), 1)
        self.assertEqual(status_events[0][1], "completed")

        # progress_updated should report full size (sum of all segments)
        self.assertEqual(len(progress_events), 1)
        self.assertEqual(progress_events[0][1], 1_000_000)

        updated = self.db.get_download("recheck-complete")
        self.assertEqual(updated.status, "completed")

    def test_recheck_file_not_found_model_progress_resets(self):
        """Verify the download model in-memory entry reflects 0 downloaded_size
        after recheck of a missing file (checks the full GUI update pipeline)."""
        from my_idm.download_model import DownloadTableModel, Col
        from PySide6.QtCore import Qt

        test_file = Path(self.tmp_dir.name) / "missing.pdf"
        entry = DownloadEntry(
            id="recheck-model",
            url="https://example.com/missing.pdf",
            filename="missing.pdf",
            file_path=str(test_file),
            save_path=self.tmp_dir.name,
            total_size=2_000_000,
            downloaded_size=2_000_000,
            status="completed",
            download_type="http",
        )
        self.db.add_download(entry)

        # Wire up model to manager signals (simulates what MainWindow does)
        model = DownloadTableModel()
        model.load_entries([entry])
        self.manager.status_changed.connect(
            lambda did, st, err: model.update_status(did, st, err)
        )
        self.manager.progress_updated.connect(
            lambda did, dl, total, spd, eta, s, p, up: model.update_progress(
                did, dl, total, spd, eta, s, p, up
            )
        )

        # File is missing on disk
        row = model._id_to_row["recheck-model"]
        # Before recheck: progress = 100%
        prog_before = model.data(model.index(row, Col.PROGRESS))
        self.assertAlmostEqual(prog_before["progress"], 100.0)

        # Trigger recheck
        self.manager.recheck_download("recheck-model")

        # After recheck: progress should be 0%
        prog_after = model.data(model.index(row, Col.PROGRESS))
        self.assertAlmostEqual(prog_after["progress"], 0.0)
        self.assertEqual(model.data(model.index(row, Col.STATUS)), "Queued")

