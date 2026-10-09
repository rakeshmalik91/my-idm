"""Unit tests for SHA-256 checksum calculation, persistence, and recheck verification."""

import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from PySide6.QtWidgets import QApplication

from my_idm.database import Database, DownloadEntry
from my_idm.manager import DownloadManager
from my_idm.utils import compute_file_sha256
from my_idm.details_panel import DetailsPanel

app = QApplication.instance() or QApplication([])


class TestChecksum(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmppath = Path(self.tmpdir.name)

        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)

        self.manager = DownloadManager(self.db)
        self.addCleanup(self.manager.stop)

    def test_compute_file_sha256(self):
        """compute_file_sha256 produces exact hex digest."""
        test_file = self.tmppath / "test_file.bin"
        data = b"Hello, My-IDM checksum verification test!"
        test_file.write_bytes(data)

        expected = hashlib.sha256(data).hexdigest()
        actual = compute_file_sha256(test_file)
        self.assertEqual(actual, expected)

    def test_recheck_passes_on_valid_checksum(self):
        """recheck_download verifies intact file against saved content_hash."""
        file_path = self.tmppath / "valid.iso"
        content = b"Exact byte payload for validation test" * 100
        file_path.write_bytes(content)
        expected_hash = hashlib.sha256(content).hexdigest()

        entry = DownloadEntry(
            id="test-valid-1",
            url="https://example.com/valid.iso",
            filename="valid.iso",
            save_path=str(self.tmppath),
            file_path=str(file_path),
            total_size=len(content),
            downloaded_size=len(content),
            status="completed",
            content_hash=expected_hash,
        )
        self.db.add_download(entry)

        self.manager.recheck_download("test-valid-1")

        updated = self.db.get_download("test-valid-1")
        self.assertEqual(updated.status, "completed")
        self.assertEqual(updated.content_hash, expected_hash)

    def test_recheck_fails_on_corrupted_file(self):
        """recheck_download detects file tampering/corruption when checksum does not match."""
        file_path = self.tmppath / "corrupted.iso"
        original_content = b"Original clean content payload" * 50
        file_path.write_bytes(original_content)
        original_hash = hashlib.sha256(original_content).hexdigest()

        entry = DownloadEntry(
            id="test-corrupted-1",
            url="https://example.com/corrupted.iso",
            filename="corrupted.iso",
            save_path=str(self.tmppath),
            file_path=str(file_path),
            total_size=len(original_content),
            downloaded_size=len(original_content),
            status="completed",
            content_hash=original_hash,
        )
        self.db.add_download(entry)

        # Corrupt file on disk (same length, different bytes — simulates AV disinfection/alteration)
        corrupted_content = b"Tampered malware disinfection!" * 50
        self.assertEqual(len(corrupted_content), len(original_content))
        file_path.write_bytes(corrupted_content)

        self.manager.recheck_download("test-corrupted-1")

        updated = self.db.get_download("test-corrupted-1")
        self.assertEqual(updated.status, "error")
        self.assertIn("Checksum verification failed", updated.error_message)

    def test_recheck_backfills_missing_checksum(self):
        """recheck_download computes and backfills content_hash if empty."""
        file_path = self.tmppath / "legacy.iso"
        content = b"Legacy file downloaded before checksums" * 20
        file_path.write_bytes(content)
        expected_hash = hashlib.sha256(content).hexdigest()

        entry = DownloadEntry(
            id="test-legacy-1",
            url="https://example.com/legacy.iso",
            filename="legacy.iso",
            save_path=str(self.tmppath),
            file_path=str(file_path),
            total_size=len(content),
            downloaded_size=len(content),
            status="completed",
            content_hash="",
        )
        self.db.add_download(entry)

        self.manager.recheck_download("test-legacy-1")

        updated = self.db.get_download("test-legacy-1")
        self.assertEqual(updated.status, "completed")
        self.assertEqual(updated.content_hash, expected_hash)

    def test_async_compute_and_save_checksum(self):
        """_compute_and_save_checksum calculates hash asynchronously and emits signal."""
        file_path = self.tmppath / "async.bin"
        data = b"Testing asynchronous checksum worker" * 500
        file_path.write_bytes(data)
        expected_hash = hashlib.sha256(data).hexdigest()

        entry = DownloadEntry(
            id="test-async-1",
            url="https://example.com/async.bin",
            filename="async.bin",
            save_path=str(self.tmppath),
            file_path=str(file_path),
            total_size=len(data),
            downloaded_size=len(data),
            status="completed",
            content_hash="",
        )
        self.db.add_download(entry)

        received_signals = []
        self.manager.checksum_computed.connect(lambda did, chk: received_signals.append((did, chk)))

        self.manager._compute_and_save_checksum("test-async-1")

        # Wait up to 3 seconds for background worker
        for _ in range(30):
            QApplication.processEvents()
            time.sleep(0.1)
            updated = self.db.get_download("test-async-1")
            if updated and updated.content_hash == expected_hash:
                break
        QApplication.processEvents()

        updated = self.db.get_download("test-async-1")
        self.assertEqual(updated.content_hash, expected_hash)
        self.assertTrue(len(received_signals) > 0)
        self.assertEqual(received_signals[0], ("test-async-1", expected_hash))

    def test_details_panel_displays_hash(self):
        """DetailsPanel overview and files inspection reflect computed content_hash."""
        file_path = self.tmppath / "display.iso"
        data = b"Display checksum test"
        file_path.write_bytes(data)
        expected_hash = hashlib.sha256(data).hexdigest()

        entry = DownloadEntry(
            id="test-display-1",
            url="https://example.com/display.iso",
            filename="display.iso",
            save_path=str(self.tmppath),
            file_path=str(file_path),
            total_size=len(data),
            downloaded_size=len(data),
            status="completed",
            content_hash=expected_hash,
        )
        self.db.add_download(entry)

        panel = DetailsPanel(self.manager)
        self.addCleanup(panel.deleteLater)

        panel.set_download_id("test-display-1")

        # Check Overview hash
        self.assertEqual(panel._ov_hash.text(), expected_hash)
        self.assertFalse(panel._ov_hash_copy_btn.isHidden())

        # Check Files tab representation
        files = self.manager.get_download_files("test-display-1", entry=entry)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["checksum"], expected_hash)


if __name__ == "__main__":
    unittest.main()
