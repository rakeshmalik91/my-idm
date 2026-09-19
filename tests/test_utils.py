"""Unit tests for utility functions and filename resolution."""

import sys
import tempfile
import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication

from my_idm.database import Database
from my_idm.download_model import Col, DownloadTableModel
from my_idm.http_engine import HTTPEngine
from my_idm.manager import DownloadManager
from my_idm.utils import get_unique_filename

app = QApplication.instance() or QApplication([])


class TestAutoNumbering(unittest.TestCase):
    """Tests for duplicate filename auto-numbering and collision avoidance."""

    def test_get_unique_filename_disk_collision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "sample.pdf").touch()

            fn1 = get_unique_filename(tmp, "sample.pdf")
            self.assertEqual(fn1, "sample (1).pdf")

            (tmp / "sample (1).pdf").touch()
            fn2 = get_unique_filename(tmp, "sample.pdf")
            self.assertEqual(fn2, "sample (2).pdf")

    def test_get_unique_filename_reserved_names(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            reserved = {"archive.tar.gz", "archive (1).tar.gz"}

            fn = get_unique_filename(tmp, "archive.tar.gz", reserved_names=reserved)
            self.assertEqual(fn, "archive (2).tar.gz")

    def test_get_unique_filename_no_collision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            fn = get_unique_filename(tmp, "newfile.txt")
            self.assertEqual(fn, "newfile.txt")


class TestFilenameResolution(unittest.TestCase):
    """Tests for URL, magnet, HTTP header Content-Disposition filename extraction."""

    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.manager = DownloadManager(self.db)

    def tearDown(self):
        self.manager.stop()
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
        from my_idm.database import DownloadEntry

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


if __name__ == "__main__":
    unittest.main()
