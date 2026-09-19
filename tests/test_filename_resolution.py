"""Unit tests for filename resolution in My-IDM."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from my_idm.database import Database, DownloadEntry
from my_idm.http_engine import HTTPEngine
from my_idm.manager import DownloadManager
from my_idm.download_model import DownloadTableModel, Col
from my_idm.main_window import MainWindow

app = QApplication.instance() or QApplication([])


class TestFilenameResolution(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.manager = DownloadManager(self.db)

    def tearDown(self):
        self.db.close()

    def test_extract_filename_from_http_url_on_add(self):
        """HTTP URLs with filenames in the path immediately resolve filename on add."""
        url = "https://releases.ubuntu.com/24.04/ubuntu-24.04-desktop-amd64.iso"
        did = self.manager.add_download(url)
        entry = self.manager.get_entry(did)

        self.assertIsNotNone(entry)
        self.assertEqual(entry.filename, "ubuntu-24.04-desktop-amd64.iso")
        self.assertTrue(entry.file_path.endswith("ubuntu-24.04-desktop-amd64.iso"))

    def test_extract_filename_from_magnet_dn_on_add(self):
        """Magnet links with &dn= parameter immediately resolve filename on add."""
        magnet = "magnet:?xt=urn:btih:3b245504cf5f11bbdbe1201cea6a6bf45a0b77bc&dn=Blender+4.2.0"
        did = self.manager.add_download(magnet)
        entry = self.manager.get_entry(did)

        self.assertIsNotNone(entry)
        self.assertEqual(entry.filename, "Blender 4.2.0")

    def test_extract_filename_from_headers_standard(self):
        """Content-Disposition standard filename parsing."""
        headers = {"Content-Disposition": 'attachment; filename="report-2026.pdf"'}
        name = HTTPEngine._extract_filename_from_headers(headers)
        self.assertEqual(name, "report-2026.pdf")

    def test_extract_filename_from_headers_rfc5987(self):
        """Content-Disposition RFC 5987 UTF-8 encoded filename."""
        headers = {"Content-Disposition": "attachment; filename*=UTF-8''project%20archive.tar.gz"}
        name = HTTPEngine._extract_filename_from_headers(headers)
        self.assertEqual(name, "project archive.tar.gz")

    def test_extract_filename_from_headers_with_extra_params(self):
        """Content-Disposition with trailing parameters."""
        headers = {"Content-Disposition": 'attachment; filename="setup.exe"; size=123456; modification-date="..."'}
        name = HTTPEngine._extract_filename_from_headers(headers)
        self.assertEqual(name, "setup.exe")

    def test_extract_filename_from_final_url_fallback(self):
        """Fallback to final redirected URL if Content-Disposition missing."""
        headers = {}
        final_url = "https://cdn.example.org/storage/v2/archive_2026.zip?signature=xyz"
        name = HTTPEngine._extract_filename_from_headers(headers, final_url)
        self.assertEqual(name, "archive_2026.zip")

    def test_model_update_filename(self):
        """Calling model.update_filename dynamically updates name and file path."""
        model = DownloadTableModel()
        entry = DownloadEntry(
            id="test-1",
            url="https://example.com/download?id=123",
            save_path="C:/Downloads",
            filename="",  # Initially empty
        )
        model.load_entries([entry])

        # Initially shows URL snippet
        self.assertEqual(model.data(model.index(0, Col.NAME)), "https://example.com/download?id=123")

        # When resolved:
        model.update_filename("test-1", "resolved_document.pdf")
        self.assertEqual(model.data(model.index(0, Col.NAME)), "resolved_document.pdf")
        self.assertEqual(
            entry.file_path,
            str(Path("C:/Downloads") / "resolved_document.pdf"),
        )

    def test_main_window_receives_filename_resolved(self):
        """MainWindow updates model when manager emits filename_resolved."""
        win = MainWindow(self.manager)
        try:
            entry = DownloadEntry(
                id="test-2",
                url="https://example.com/api/get",
                filename="",
            )
            win._model.load_entries([entry])

            # Simulate manager resolving filename
            self.manager.filename_resolved.emit("test-2", "dynamic_file.zip")
            self.assertEqual(win._model.get_entry(0).filename, "dynamic_file.zip")
            self.assertEqual(win._model.data(win._model.index(0, Col.NAME)), "dynamic_file.zip")
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
