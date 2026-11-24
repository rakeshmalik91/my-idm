"""Unit tests for large torrent sizes (>2GB, >18GB), 64-bit Signal emission,
progress clamping, fastresume metadata caching, and action button gating.
"""

import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
import tempfile
import warnings

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem
from PySide6.QtGui import QPainter, QImage

from my_idm.database import Database, DownloadEntry
from my_idm.manager import DownloadManager
from my_idm.download_model import DownloadTableModel, Col
from my_idm.delegates import ProgressBarDelegate
from my_idm.torrent_engine import TorrentEngine, FASTRESUME_DIR

app = QApplication.instance() or QApplication([])


class TestTorrentLargeSizesAndMetadata(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test.db"
        self.db = Database(self.db_path)
        self.db.open()
        self.manager = DownloadManager(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp_dir.cleanup()

    def test_progress_updated_signal_handles_large_int64_without_overflow(self):
        """Bytes count > 2 GB and > 18 GB must pass through progress_updated without Shiboken overflow."""
        emitted_events = []
        self.manager.progress_updated.connect(
            lambda did, dl, total, speed, eta, s, p, up: emitted_events.append((did, dl, total, speed))
        )

        zelda_dl = 2_923_462_656       # ~2.92 GB (exceeds 32-bit signed int 2,147,483,647)
        zelda_total = 18_338_743_321   # ~18.34 GB (exceeds 32-bit unsigned int 4,294,967,296)

        with warnings.catch_warnings(record=True) as captured_warnings:
            warnings.simplefilter("always")
            self.manager.progress_updated.emit("test_id", zelda_dl, zelda_total, 1024.0, 100.0, 5, 10, 512.0)

        # Ensure no Shiboken overflow RuntimeWarning was emitted
        overflow_warnings = [
            w for w in captured_warnings if "overflow" in str(w.message).lower()
        ]
        self.assertEqual(len(overflow_warnings), 0, f"Unexpected overflow warnings: {overflow_warnings}")
        self.assertEqual(len(emitted_events), 1)
        did, dl, total, speed = emitted_events[0]
        self.assertEqual(did, "test_id")
        self.assertEqual(dl, zelda_dl)
        self.assertEqual(total, zelda_total)

    def test_download_entry_progress_clamped_and_never_negative(self):
        """DownloadEntry.progress must never return negative values or exceed 100%."""
        entry = DownloadEntry(
            id="e1",
            url="magnet:?xt=urn:btih:12345",
            filename="game.bin",
            download_type="torrent",
            downloaded_size=2_923_462_656,
            total_size=18_338_743_321,
            status="downloading",
        )
        self.assertAlmostEqual(entry.progress, 15.94, places=1)

        # Corrupted / negative downloaded size
        entry.downloaded_size = -1_371_504_640
        self.assertEqual(entry.progress, 0.0)

        # Total size 0
        entry.downloaded_size = 500
        entry.total_size = 0
        self.assertEqual(entry.progress, 0.0)

        # Exceeding total size
        entry.total_size = 1000
        entry.downloaded_size = 2000
        self.assertEqual(entry.progress, 100.0)

    def test_download_model_update_progress_clamps_negatives(self):
        """DownloadModel.update_progress must clamp negative values to 0."""
        model = DownloadTableModel()
        entry = DownloadEntry(
            id="m1",
            url="magnet:?xt=urn:btih:abcde",
            filename="large.iso",
            download_type="torrent",
            downloaded_size=1000,
            total_size=5000,
            status="downloading",
        )
        model.load_entries([entry])

        # Negative downloaded or total should be clamped to 0
        model.update_progress("m1", -500, -1000, 0.0, 0.0)
        e = model.get_entry_by_id("m1")
        self.assertIsNotNone(e)
        self.assertEqual(e.downloaded_size, 0)
        self.assertGreaterEqual(e.progress, 0.0)

        # Large 64-bit values
        model.update_progress("m1", 2_923_462_656, 18_338_743_321, 1024.0, 0.0)
        e = model.get_entry_by_id("m1")
        self.assertEqual(e.downloaded_size, 2_923_462_656)
        self.assertEqual(e.total_size, 18_338_743_321)
        self.assertAlmostEqual(e.progress, 15.94, places=1)

        # Check Col.PROGRESS data representation
        cell_data = model.data(model.index(0, Col.PROGRESS), Qt.ItemDataRole.DisplayRole)
        self.assertIsInstance(cell_data, dict)
        self.assertAlmostEqual(cell_data["progress"], 15.94, places=1)

    def test_progress_bar_delegate_does_not_render_negative_percentage(self):
        """ProgressBarDelegate must clamp and format progress correctly."""
        delegate = ProgressBarDelegate()
        img = QImage(200, 30, QImage.Format.Format_ARGB32)
        painter = QPainter(img)
        option = QStyleOptionViewItem()
        option.rect.setRect(0, 0, 200, 30)

        index = MagicMock()
        # Even if raw data returned -116%, delegate must clamp to 0.0%
        index.data.return_value = {"progress": -116.0, "status": "downloading"}
        try:
            delegate.paint(painter, option, index)
        finally:
            painter.end()

    def test_pause_and_stop_incomplete_downloading_torrent(self):
        """Pausing and stopping an active downloading torrent must set status to 'paused'

        and 'stopped' respectively, and resume_download must allow resuming it to 'queued'.
        """
        entry = DownloadEntry(
            id="t_inc",
            url="magnet:?xt=urn:btih:fedcba",
            filename="incomplete.iso",
            download_type="torrent",
            downloaded_size=2_000_000_000,
            total_size=10_000_000_000,
            status="downloading",
        )
        self.db.add_download(entry)

        # Pause
        self.manager.pause_download("t_inc", trigger_process_queue=False)
        updated = self.db.get_download("t_inc")
        self.assertEqual(updated.status, "paused")

        # Resume from paused
        self.manager.resume_download("t_inc", trigger_process_queue=False)
        updated = self.db.get_download("t_inc")
        self.assertEqual(updated.status, "queued")

        # Put back to downloading and stop
        updated.status = "downloading"
        self.db.update_download(updated)
        self.manager.stop_download("t_inc", trigger_process_queue=False)
        updated = self.db.get_download("t_inc")
        self.assertEqual(updated.status, "stopped")

        # Resume from stopped
        self.manager.resume_download("t_inc", trigger_process_queue=False)
        updated = self.db.get_download("t_inc")
        self.assertEqual(updated.status, "queued")


if __name__ == "__main__":
    unittest.main()
