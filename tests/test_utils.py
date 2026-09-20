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
from my_idm.utils import extract_source_domain, get_unique_filename, send_to_trash

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


class TestExtractSourceDomain(unittest.TestCase):
    """Tests for source website domain extraction from URLs and magnet links."""

    def test_http_https_domain_extraction(self):
        """Extract clean hostname from http/https URLs."""
        self.assertEqual(
            extract_source_domain("https://releases.ubuntu.com/24.04/ubuntu.iso"),
            "releases.ubuntu.com",
        )
        self.assertEqual(
            extract_source_domain("https://www.youtube.com/watch?v=123"),
            "youtube.com",
        )
        self.assertEqual(
            extract_source_domain("http://mirror.archlinux.org/iso/archlinux.iso"),
            "mirror.archlinux.org",
        )

    def test_custom_ports_and_ftp(self):
        """Handles custom ports and FTP protocol."""
        self.assertEqual(
            extract_source_domain("http://myfiles.org:8080/files/archive.zip"),
            "myfiles.org",
        )
        self.assertEqual(
            extract_source_domain("ftp://ftp.gnu.org/gnu/emacs/emacs-29.1.tar.gz"),
            "ftp.gnu.org",
        )

    def test_magnet_tracker_and_webseed(self):
        """Extracts domain from tracker (tr) or webseed (ws) query parameters in magnet links."""
        magnet_tr = (
            "magnet:?xt=urn:btih:da39a3ee5e6b4b0d3255bfef95601890afd80709"
            "&dn=Ubuntu&tr=http%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce"
        )
        self.assertEqual(extract_source_domain(magnet_tr), "tracker.opentrackr.org")

        magnet_ws = (
            "magnet:?xt=urn:btih:da39a3ee5e6b4b0d3255bfef95601890afd80709"
            "&dn=Linux&ws=https%3A%2F%2Fseed.kernel.org%2Flinux.iso"
        )
        self.assertEqual(extract_source_domain(magnet_ws), "seed.kernel.org")

    def test_empty_or_local_urls(self):
        """Returns empty string for local paths, empty URLs, or trackerless magnets."""
        self.assertEqual(extract_source_domain(""), "")
        self.assertEqual(extract_source_domain(None), "")
        self.assertEqual(extract_source_domain("C:/Downloads/torrent.torrent"), "")
        self.assertEqual(
            extract_source_domain("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"),
            "",
        )


class TestToInt(unittest.TestCase):
    """Tests for safe integer coercion utility to_int."""

    def test_to_int_with_integers(self):
        from my_idm.utils import to_int
        self.assertEqual(to_int(0), 0)
        self.assertEqual(to_int(42), 42)
        self.assertEqual(to_int(-10), -10)

    def test_to_int_with_collections(self):
        from my_idm.utils import to_int
        self.assertEqual(to_int([]), 0)
        self.assertEqual(to_int(["peer1", "peer2"]), 2)
        self.assertEqual(to_int(("a", "b", "c")), 3)
        self.assertEqual(to_int({"key1": "val1"}), 1)
        self.assertEqual(to_int({1, 2, 3, 4}), 4)

    def test_to_int_with_strings(self):
        from my_idm.utils import to_int
        self.assertEqual(to_int("123"), 123)
        self.assertEqual(to_int("0"), 0)
        self.assertEqual(to_int("invalid"), 0)
        self.assertEqual(to_int("invalid", default=99), 99)

    def test_to_int_with_none_and_other_types(self):
        from my_idm.utils import to_int
        self.assertEqual(to_int(None), 0)
        self.assertEqual(to_int(None, default=-1), -1)
        self.assertEqual(to_int(object()), 0)


class TestSendToTrash(unittest.TestCase):
    """Tests for send_to_trash utility moving files and folders to trash."""

    def test_send_file_to_trash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "sample_to_trash.txt"
            test_file.write_text("Hello Trash")
            self.assertTrue(test_file.exists())

            result = send_to_trash(test_file)
            self.assertTrue(result)
            self.assertFalse(test_file.exists())

    def test_send_directory_to_trash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sub_dir = Path(tmpdir) / "folder_to_trash"
            sub_dir.mkdir()
            (sub_dir / "nested_file.bin").write_bytes(b"content" * 20)
            self.assertTrue(sub_dir.exists())

            result = send_to_trash(sub_dir)
            self.assertTrue(result)
            self.assertFalse(sub_dir.exists())

    def test_send_nonexistent_path_returns_true(self):
        non_existent = Path(tempfile.gettempdir()) / "non_existent_never_existed_123.bin"
        self.assertFalse(non_existent.exists())
        self.assertTrue(send_to_trash(non_existent))

    def test_send_empty_path_returns_false(self):
        self.assertFalse(send_to_trash(""))
        self.assertFalse(send_to_trash(None))


if __name__ == "__main__":
    unittest.main()

