"""Unit tests for utility functions and filename resolution."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from my_idm.config import GeneralConfig
from my_idm.database import Database
from my_idm.download_model import Col, DownloadTableModel
from my_idm.http_engine import HTTPEngine
from my_idm.manager import DownloadManager
from my_idm.utils import (
    extract_source_domain,
    get_unique_filename,
    sanitize_filename,
    send_to_trash,
    split_extension,
    unlock_path,
)

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
        # get_unique_filename() probes the real filesystem, so the manager must
        # never resolve against the user's ~/Downloads: a machine that already
        # has "ubuntu-24.04-desktop-amd64.iso" there would make add_download()
        # auto-number to "... (1).iso".
        self.save_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.save_dir.cleanup)

        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)

        self.manager = DownloadManager(self.db)
        self.addCleanup(self.manager.stop)
        self.manager.set_general_config(
            GeneralConfig(
                default_save_path=self.save_dir.name,
                remember_last_save_path=False,
            )
        )

    def test_extract_filename_from_http_url_on_add(self):
        """HTTP URLs with filenames in the path immediately resolve filename on add."""
        url = "https://releases.ubuntu.com/24.04/ubuntu-24.04-desktop-amd64.iso"
        did = self.manager.add_download(url)
        entry = self.manager.get_entry(did)

        self.assertIsNotNone(entry)
        self.assertEqual(entry.filename, "ubuntu-24.04-desktop-amd64.iso")
        self.assertTrue(entry.file_path.endswith("ubuntu-24.04-desktop-amd64.iso"))
        # The save path is the per-test temp dir, not the user's ~/Downloads.
        self.assertEqual(
            entry.save_path, self.save_dir.name.replace("\\", "/")
        )

    def test_extract_filename_from_magnet_dn_on_add(self):
        """Magnet links with &dn= parameter immediately resolve filename on add."""
        magnet = "magnet:?xt=urn:btih:3b245504cf5f11bbdbe1201cea6a6bf45a0b77bc&dn=Blender+4.2.0"
        did = self.manager.add_download(magnet)
        entry = self.manager.get_entry(did)

        self.assertIsNotNone(entry)
        self.assertEqual(entry.filename, "Blender 4.2.0")
        self.assertTrue(entry.file_path.endswith("Blender+4.2.0") or entry.file_path.endswith("Blender 4.2.0"))

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


class TestSanitizeFilename(unittest.TestCase):
    """sanitize_filename's stem-truncation, extension-preservation, and fallback rules.

    The module caps the stem at ``_MAX_FILENAME_STEM`` (150) characters; the
    original coverage only checked the 255-character ``max_length`` ceiling, so
    the real truncation rule was never pinned.
    """

    def test_short_extension_is_split_off(self):
        self.assertEqual(split_extension("movie.mkv"), ("movie", "mkv"))
        self.assertEqual(split_extension("archive.tar.gz"), ("archive.tar", "gz"))
        self.assertEqual(split_extension("no_extension"), ("no_extension", ""))

    def test_long_dotted_tail_is_not_an_extension(self):
        # "Arr. by J. Halvorsen [PIANO COVER]" must not be cut at the last dot.
        name = "Arr. by J. Halvorsen [PIANO COVER]"
        stem, ext = split_extension(name)
        self.assertEqual(stem, name)
        self.assertEqual(ext, "")
        self.assertEqual(sanitize_filename(name), name)

    def test_stem_is_truncated_to_the_module_cap_preserving_extension(self):
        from my_idm.utils import _MAX_FILENAME_STEM

        long_name = "a" * (_MAX_FILENAME_STEM + 50) + ".mkv"
        out = sanitize_filename(long_name)
        stem, ext = split_extension(out)
        self.assertEqual(ext, "mkv", "the extension must survive truncation")
        self.assertLessEqual(
            len(stem), _MAX_FILENAME_STEM, f"stem must be capped at {_MAX_FILENAME_STEM}"
        )
        self.assertTrue(out.endswith(".mkv"))
        self.assertLess(len(out), len(long_name))

    def test_max_length_below_the_cap_tightens_the_stem(self):
        out = sanitize_filename("b" * 200 + ".pdf", max_length=40)
        stem, ext = split_extension(out)
        self.assertEqual(ext, "pdf")
        # max_stem = max(1, min(150, max_length - (len(ext) + 1))) = 40 - 4 = 36
        self.assertEqual(len(stem), 36, out)
        self.assertEqual(len(out), 40, "max_length is the whole name, extension included")
        self.assertTrue(out.endswith(".pdf"))

    def test_max_length_counts_the_extension(self):
        out = sanitize_filename("c" * 200 + ".tar.gz", max_length=30)
        self.assertTrue(out.endswith(".gz"))
        # extension "gz" + the joining dot => 30 - 3 = 27 stem characters
        self.assertEqual(len(out), 30, out)

    def test_default_fallback_for_empty_and_unusable_names(self):
        self.assertEqual(sanitize_filename(""), "download")
        self.assertEqual(sanitize_filename("   "), "download")
        self.assertEqual(sanitize_filename("..."), "download")
        self.assertEqual(sanitize_filename("", fallback="fallback.bin"), "fallback.bin")
        self.assertEqual(sanitize_filename("...", fallback="fallback.bin"), "fallback.bin")
        # NOTE: a name made *entirely* of illegal characters is not empty after
        # substitution, so it becomes "_______" rather than the fallback. Pinned
        # so the behaviour is visible; see the report (production oddity).
        self.assertEqual(sanitize_filename("<>:\"|?*"), "_" * 7)

    def test_reserved_device_names_are_escaped(self):
        self.assertEqual(sanitize_filename("con.txt"), "_con.txt")
        self.assertEqual(sanitize_filename("LPT1.log"), "_LPT1.log")

    def test_illegal_characters_and_whitespace_are_collapsed(self):
        self.assertEqual(
            sanitize_filename("a<b>c:d.e|f?g*h.txt"), "a_b_c_d.e_f_g_h.txt"
        )
        self.assertEqual(
            sanitize_filename("  spaced    out   name.zip "), "spaced out name.zip"
        )

    def test_max_length_of_one_still_yields_a_usable_name(self):
        out = sanitize_filename("extremelylongname.zip", max_length=1)
        self.assertTrue(out)
        self.assertEqual(len(out.rpartition(".")[0]), 1, out)
        self.assertTrue(out.endswith(".zip"))


class TestUnlockPath(unittest.TestCase):
    """``unlock_path`` must leave a directory traversable.

    Regression test for a real Linux/macOS defect. ``unlock_path`` used to chmod its argument to
    ``S_IWRITE | S_IREAD``, which on Windows only clears the read-only attribute but on POSIX is
    mode ``0o600`` - no execute bit. Given a directory, that made it untraversable, so the next
    ``stat()`` / ``open()`` / ``shutil.move()`` of anything inside it raised
    ``PermissionError: [Errno 13]``. ``robust_move_download_files`` calls it on the destination
    directory and then immediately walks into it, so every directory move failed on the two CI
    runners while passing on Windows, where the same code is harmless.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_directory_is_still_traversable_afterwards(self):
        folder = self.root / "payload"
        folder.mkdir()
        (folder / "file.txt").write_text("data", encoding="utf-8")

        unlock_path(folder)

        # Reachable, listable, and readable - each of these is an independent way the missing
        # execute bit showed up.
        self.assertTrue((folder / "file.txt").is_file())
        self.assertEqual([p.name for p in folder.iterdir()], ["file.txt"])
        self.assertEqual((folder / "file.txt").read_text(encoding="utf-8"), "data")

    def test_a_read_only_directory_becomes_writable_and_still_traversable(self):
        folder = self.root / "locked"
        folder.mkdir()
        (folder / "file.txt").write_text("data", encoding="utf-8")
        folder.chmod(0o500)  # r-x: readable and traversable, not writable

        unlock_path(folder)

        (folder / "added.txt").write_text("new", encoding="utf-8")
        self.assertEqual((folder / "file.txt").read_text(encoding="utf-8"), "data")

    def test_a_read_only_file_becomes_writable(self):
        target = self.root / "locked.txt"
        target.write_text("data", encoding="utf-8")
        target.chmod(0o400)

        unlock_path(target)

        target.write_text("changed", encoding="utf-8")
        self.assertEqual(target.read_text(encoding="utf-8"), "changed")

    def test_a_missing_path_is_ignored(self):
        unlock_path(self.root / "never-existed")  # must not raise


class TestSendToTrash(unittest.TestCase):
    """send_to_trash's three-tier fallback, each tier asserted individually.

    ``send_to_trash`` tries ``QFile.moveToTrash`` first, then the ``send2trash``
    package, then falls back to *permanent* deletion. All three end with "the
    path no longer exists", so asserting only on that makes them
    indistinguishable and leaves the developer's Recycle Bin full of test
    files. Each test below neutralises the higher-priority tiers and asserts
    which one actually ran; nothing ever reaches a real trash can.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.trash_bin = Path(self.tmp.name) / "fake_recycle_bin"
        self.trash_bin.mkdir()
        # Any attempt to fall through to permanent deletion is a test bug, so
        # the higher tiers replace it with an explicit, loud failure instead.
        self.permanent_delete = []

    def _make_file(self, name="sample_to_trash.txt"):
        target = Path(self.tmp.name) / name
        target.write_text("Hello Trash")
        self.assertTrue(target.exists())
        return target

    def _make_dir(self, name="folder_to_trash"):
        target = Path(self.tmp.name) / name
        target.mkdir()
        (target / "nested_file.bin").write_bytes(b"content" * 20)
        self.assertTrue(target.exists())
        return target

    @staticmethod
    def _blocking_unlink(moved):
        def _unlink(self, *a, **k):
            moved.append(str(self))
            raise AssertionError(f"permanent delete reached for {self}")

        return _unlink

    # -- tier 1: Qt's native recycle bin ---------------------------------

    def test_send_file_to_trash(self):
        """Tier 1: the file reaches Qt's native recycle bin and is never deleted.

        Strictly stronger than the previous version, which called the real
        ``QFile.moveToTrash`` (polluting the developer's Recycle Bin) and only
        asserted ``not exists()`` -- which the other two tiers also satisfy.
        """
        target = self._make_file()
        escaped = []

        def fake_move_to_trash(path):
            # Simulate Qt really recycling the file.
            Path(path).rename(self.trash_bin / Path(path).name)
            return True

        with patch("PySide6.QtCore.QFile") as mock_qfile, \
             patch("send2trash.send2trash", side_effect=AssertionError("tier 2 must not run")), \
             patch.object(Path, "unlink", self._blocking_unlink(escaped)), \
             patch.object(shutil, "rmtree", side_effect=AssertionError("permanent delete reached")):
            mock_qfile.moveToTrash.side_effect = fake_move_to_trash
            result = send_to_trash(target)

        self.assertTrue(result, "send_to_trash must report success")
        self.assertFalse(target.exists(), "the file must be gone from its original path")
        self.assertTrue(
            (self.trash_bin / target.name).exists(),
            "the file must have landed in the (fake) recycle bin, not been deleted",
        )
        self.assertEqual(escaped, [], "no permanent delete may run")
        self.assertGreaterEqual(
            mock_qfile.moveToTrash.call_count, 1, "the Qt tier must be attempted"
        )
        self.assertEqual(mock_qfile.moveToTrash.call_args_list[0].args[0], str(target))

    def test_send_directory_to_trash(self):
        """Tier 1 for a directory: recycled, never rmtree'd."""
        target = self._make_dir()
        escaped = []

        def fake_move_to_trash(path):
            Path(path).rename(self.trash_bin / Path(path).name)
            return True

        with patch("PySide6.QtCore.QFile") as mock_qfile, \
             patch("send2trash.send2trash", side_effect=AssertionError("tier 2 must not run")), \
             patch.object(Path, "unlink", self._blocking_unlink(escaped)), \
             patch.object(shutil, "rmtree", side_effect=AssertionError("permanent delete reached")):
            mock_qfile.moveToTrash.side_effect = fake_move_to_trash
            result = send_to_trash(target)

        self.assertTrue(result, "send_to_trash must report success")
        self.assertFalse(target.exists())
        self.assertTrue((self.trash_bin / target.name).is_dir())
        self.assertEqual(escaped, [], "no permanent delete may run")
        self.assertGreaterEqual(mock_qfile.moveToTrash.call_count, 1)

    # -- tier 2: the send2trash package ----------------------------------

    def test_send_file_falls_back_to_send2trash_when_qt_declines(self):
        target = self._make_file()
        escaped = []

        def fake_send2trash(path):
            Path(path).rename(self.trash_bin / Path(path).name)

        with patch("PySide6.QtCore.QFile") as mock_qfile, \
             patch("send2trash.send2trash", side_effect=fake_send2trash) as mock_s2t, \
             patch.object(Path, "unlink", self._blocking_unlink(escaped)), \
             patch.object(shutil, "rmtree", side_effect=AssertionError("permanent delete reached")):
            mock_qfile.moveToTrash.return_value = False
            result = send_to_trash(target)

        self.assertTrue(result, "send_to_trash must report success")
        self.assertFalse(target.exists())
        self.assertTrue((self.trash_bin / target.name).exists())
        self.assertEqual(escaped, [], "no permanent delete may run")
        # Qt is tried with both the native and backslash-separated spelling
        # before falling through.
        self.assertEqual(mock_qfile.moveToTrash.call_count, 2, mock_qfile.moveToTrash.call_args_list)
        mock_s2t.assert_called_once_with(str(target))

    # -- tier 3: permanent deletion --------------------------------------

    def test_send_file_permanently_deletes_when_both_trash_tiers_fail(self):
        target = self._make_file()
        with patch("PySide6.QtCore.QFile") as mock_qfile, \
             patch("send2trash.send2trash", side_effect=OSError("no trash available")) as mock_s2t:
            mock_qfile.moveToTrash.return_value = False
            result = send_to_trash(target)

        self.assertTrue(result, "send_to_trash must report success once the file is gone")
        self.assertFalse(target.exists(), "the permanent-delete tier must have unlinked it")
        self.assertFalse(
            (self.trash_bin / target.name).exists(),
            "nothing may reach a recycle bin on this path",
        )
        self.assertEqual(mock_qfile.moveToTrash.call_count, 2)
        mock_s2t.assert_called_once_with(str(target))

    def test_send_directory_permanently_deletes_when_both_trash_tiers_fail(self):
        target = self._make_dir()
        real_rmtree = shutil.rmtree
        calls = []

        def spy_rmtree(path, *a, **k):
            calls.append(str(path))
            return real_rmtree(path, *a, **k)

        with patch("PySide6.QtCore.QFile") as mock_qfile, \
             patch("send2trash.send2trash", side_effect=OSError("no trash available")), \
             patch.object(shutil, "rmtree", side_effect=spy_rmtree):
            mock_qfile.moveToTrash.return_value = False
            result = send_to_trash(target)

        self.assertTrue(result, "send_to_trash must report success once the tree is gone")
        self.assertFalse(target.exists(), "the permanent-delete tier must have rmtree'd it")
        self.assertEqual(calls, [str(target)], "rmtree, not unlink, is the directory path")
        self.assertEqual(mock_qfile.moveToTrash.call_count, 2)

    # -- short-circuits ---------------------------------------------------

    def test_send_nonexistent_path_returns_true(self):
        non_existent = Path(self.tmp.name) / "non_existent_never_existed_123.bin"
        self.assertFalse(non_existent.exists())
        with patch("PySide6.QtCore.QFile") as mock_qfile, \
             patch("send2trash.send2trash", side_effect=AssertionError("nothing to trash")):
            self.assertTrue(send_to_trash(non_existent))
        mock_qfile.moveToTrash.assert_not_called()

    def test_send_empty_path_returns_false(self):
        with patch("PySide6.QtCore.QFile") as mock_qfile, \
             patch("send2trash.send2trash", side_effect=AssertionError("nothing to trash")):
            self.assertFalse(send_to_trash(""))
            self.assertFalse(send_to_trash(None))
        mock_qfile.moveToTrash.assert_not_called()


if __name__ == "__main__":
    unittest.main()

