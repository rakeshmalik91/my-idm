"""Unit tests for SHA-256 checksum calculation, persistence, and recheck verification."""

import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from my_idm.database import Database, DownloadEntry
from my_idm.manager import DownloadManager
from my_idm.utils import compute_file_sha256
from my_idm.details_panel import DetailsPanel

app = QApplication.instance() or QApplication([])


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    """Wait until *predicate* holds, without pumping the Qt event loop.

    Every worker this module exercises runs on a daemon thread that writes its
    result to the database before it signals, so polling the DB is enough. Calling
    ``processEvents()`` instead would process whatever queued events the rest of
    the session left behind, which is the access-violation class this suite
    already knows about - see `.agents/workflows/testing.md`.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


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

        self.status_changes = []
        # DirectConnection: the workers under test run on daemon threads, and a
        # queued connection would only ever be delivered by processEvents(),
        # which this module deliberately does not call.
        self.manager.status_changed.connect(
            lambda did, st, msg: self.status_changes.append((did, st, msg)),
            Qt.ConnectionType.DirectConnection)
        self.checksums = []
        self.manager.checksum_computed.connect(
            lambda did, chk: self.checksums.append((did, chk)),
            Qt.ConnectionType.DirectConnection)

    def _add_completed_entry(self, eid, *, total=1000, downloaded=None, content_hash="",
                             download_type="http", filename=None, status="completed"):
        name = filename or f"{eid}.bin"
        file_path = self.tmppath / name
        file_path.write_bytes(b"x" * (total if downloaded is None else downloaded))
        entry = DownloadEntry(
            id=eid,
            url=f"https://example.com/{name}",
            filename=name,
            save_path=str(self.tmppath),
            file_path=str(file_path),
            total_size=total,
            downloaded_size=total if downloaded is None else downloaded,
            status=status,
            content_hash=content_hash,
            download_type=download_type,
        )
        self.db.add_download(entry)
        return entry

    def test_compute_file_sha256(self):
        """compute_file_sha256 produces exact hex digest."""
        test_file = self.tmppath / "test_file.bin"
        data = b"Hello, My-IDM checksum verification test!"
        test_file.write_bytes(data)

        expected = hashlib.sha256(data).hexdigest()
        actual = compute_file_sha256(test_file)
        self.assertEqual(actual, expected)

    def test_compute_file_sha256_streams_every_chunk_including_empty_files(self):
        """The read loop must cover multi-chunk, boundary and zero-byte payloads."""
        big = b"chunk-boundary payload " * 180_000  # ~4.3 MiB, several 1 MiB reads
        big_file = self.tmppath / "big.bin"
        big_file.write_bytes(big)
        self.assertEqual(compute_file_sha256(big_file), hashlib.sha256(big).hexdigest())

        exact = b"y" * (1024 * 1024 + 1)  # one full chunk plus a single trailing byte
        exact_file = self.tmppath / "exact.bin"
        exact_file.write_bytes(exact)
        self.assertEqual(compute_file_sha256(exact_file), hashlib.sha256(exact).hexdigest())

        # A tiny chunk_size forces many iterations, so an update hoisted out of
        # the loop or a first-chunk-only read cannot pass.
        self.assertEqual(
            compute_file_sha256(big_file, chunk_size=7),
            hashlib.sha256(big).hexdigest(),
        )

        empty = self.tmppath / "empty.bin"
        empty.write_bytes(b"")
        self.assertEqual(compute_file_sha256(empty), hashlib.sha256(b"").hexdigest())

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
        self.assertIn(
            ("test-corrupted-1", "error",
             "Checksum verification failed: file corrupted or modified on disk"),
            self.status_changes,
        )
        # The mismatch path must not also claim the download completed.
        self.assertNotIn("completed",
                         [s for _, s, _ in self.status_changes if _ == "test-corrupted-1"])

    def test_a_match_on_recheck_emits_nothing_and_a_backfill_emits_once(self):
        """Only a newly computed hash is worth a signal; verifying one is silent."""
        data = b"x" * 64  # the payload _add_completed_entry writes to disk
        self._add_completed_entry("match-1", total=len(data), downloaded=len(data),
                                  content_hash=hashlib.sha256(data).hexdigest())
        self._add_completed_entry("backfill-1", total=len(data), downloaded=len(data))

        self.manager.recheck_download("backfill-1")
        digest = hashlib.sha256(data).hexdigest()
        self.assertEqual(self.checksums, [("backfill-1", digest)])

        self.status_changes.clear()
        self.manager.recheck_download("match-1")
        self.assertEqual(self.checksums, [("backfill-1", digest)],
                         "a verified hash must not be re-announced")
        self.assertIn(("match-1", "completed", ""), self.status_changes)

    def test_recheck_of_a_partial_file_skips_the_hash_comparison(self):
        """A partial payload is not hashed at all, so a stale hash cannot fail it.

        Note the entry cannot be `completed`: `_row_to_entry` clamps
        `downloaded_size` up to `total_size` for a complete row, so a genuinely
        partial payload has to be entered as paused for the partial branch to be
        reachable at all.
        """
        partial_bytes = 30
        self._add_completed_entry(
            "partial-1", total=1000, downloaded=partial_bytes, status="paused",
            content_hash="0" * 64,  # deliberately wrong for the bytes on disk
        )

        with patch("my_idm.utils.compute_file_sha256") as mock_hash:
            self.manager.recheck_download("partial-1")
            mock_hash.assert_not_called()

        updated = self.db.get_download("partial-1")
        self.assertEqual(updated.status, "paused")
        self.assertEqual(updated.downloaded_size, partial_bytes)
        self.assertEqual(updated.content_hash, "0" * 64, "the stale hash is left alone")
        self.assertIn(("partial-1", "paused", f"Downloaded: {partial_bytes} / 1000"),
                      self.status_changes)
        self.assertFalse(any("Checksum verification failed" in m for _, _, m in self.status_changes))

    def test_recheck_without_a_file_resets_progress_and_stays_paused(self):
        """A missing file is a reset, not an error and not a resumed download."""
        entry = self._add_completed_entry("gone-1", total=500, content_hash="a" * 64)
        (self.tmppath / entry.filename).unlink()

        self.manager.recheck_download("gone-1")

        updated = self.db.get_download("gone-1")
        self.assertEqual(updated.status, "paused")
        self.assertEqual(updated.downloaded_size, 0)
        self.assertIn(("gone-1", "paused", "File not found"), self.status_changes)
        self.assertEqual(self.checksums, [])

    def test_compute_and_save_checksum_skips_torrents_and_unknown_ids(self):
        """Torrents carry their own piece hashes, and an unknown id is a no-op."""
        with patch("my_idm.utils.compute_file_sha256") as mock_hash:
            mock_hash.return_value = "f" * 64
            self._add_completed_entry("tor-1", total=10, download_type="torrent")
            self.manager._compute_and_save_checksum("tor-1")
            self.manager._compute_and_save_checksum("no-such-id")
            self.assertTrue(_wait_until(lambda: not any(
                t.name == "hash-tor-1" and t.is_alive() for t in threading.enumerate())))
            mock_hash.assert_not_called()
            self.assertEqual(self.checksums, [])
            self.assertEqual(self.db.get_download("tor-1").content_hash, "")

    def test_an_empty_file_is_hashed_rather_than_skipped(self):
        """0 bytes is still a payload: the empty digest is recorded, not omitted."""
        self._add_completed_entry("empty-1", total=0)
        self.manager._compute_and_save_checksum("empty-1")

        self.assertTrue(_wait_until(lambda: self.checksums))
        self.assertEqual(self.checksums[0][1], hashlib.sha256(b"").hexdigest())
        self.assertEqual(self.db.get_download("empty-1").content_hash, self.checksums[0][1])

    def test_the_checksum_worker_swallows_a_hash_failure(self):
        """A read error must not surface, must not signal, and must not corrupt state."""
        self._add_completed_entry("bad-1", total=10)
        with patch("my_idm.utils.compute_file_sha256", side_effect=OSError("disk gone")):
            self.manager._compute_and_save_checksum("bad-1")
            self.assertTrue(_wait_until(lambda: self.db.get_download("bad-1") is not None and
                                        self._hash_thread_finished("bad-1")))

        self.assertEqual(self.checksums, [])
        updated = self.db.get_download("bad-1")
        self.assertEqual(updated.content_hash, "")
        self.assertEqual(updated.status, "completed")

    def _hash_thread_finished(self, download_id: str) -> bool:
        return not any(t.name == f"hash-{download_id}" and t.is_alive()
                       for t in threading.enumerate())

    def test_a_verify_failure_still_completes_and_a_backfill_failure_completes_empty(self):
        """A hash that cannot be computed is not a corrupted download."""
        data = b"x" * 64
        # Verify path: a hash is recorded but cannot be recomputed.
        self._add_completed_entry("verify-fail", total=len(data), downloaded=len(data),
                                  content_hash="b" * 64)
        with patch("my_idm.utils.compute_file_sha256", side_effect=OSError("unreadable")):
            self.manager.recheck_download("verify-fail")
        updated = self.db.get_download("verify-fail")
        self.assertEqual(updated.status, "completed")
        self.assertEqual(updated.content_hash, "b" * 64)

        # Backfill path: no hash recorded and it cannot be computed either.
        self._add_completed_entry("backfill-fail", total=len(data), downloaded=len(data))
        self.checksums.clear()
        with patch("my_idm.utils.compute_file_sha256", side_effect=OSError("unreadable")):
            self.manager.recheck_download("backfill-fail")
        updated = self.db.get_download("backfill-fail")
        self.assertEqual(updated.status, "completed")
        self.assertEqual(updated.content_hash, "")
        self.assertEqual(self.checksums, [])

    def test_a_clean_scan_hashes_afterwards_and_a_threat_never_does(self):
        """The hash runs only once the file has passed antivirus, and not on a threat."""
        with patch("my_idm.manager.scan_file", return_value=(True, "clean")):
            self._add_completed_entry("scan-clean", total=64)
            self.manager._handle_completed_scan("scan-clean")
            self.assertTrue(_wait_until(
                lambda: self.db.get_download("scan-clean").content_hash != ""))

        updated = self.db.get_download("scan-clean")
        self.assertEqual(updated.status, "completed")
        self.assertEqual(updated.metadata.get("antivirus_scanned"), True)
        self.assertEqual(self.checksums[0][0], "scan-clean")

        self.checksums.clear()
        with patch("my_idm.manager.scan_file", return_value=(False, "Win.Test.EICAR")), \
             patch("my_idm.manager.quarantine_or_delete_file"):
            self._add_completed_entry("scan-threat", total=64)
            self.manager._handle_completed_scan("scan-threat")
            # The scan runs on a thread, so wait while the mock is still installed.
            self.assertTrue(_wait_until(
                lambda: self.db.get_download("scan-threat").status != "scanning"))

        self.assertEqual(self.checksums, [], "a quarantined file is never hashed")
        self.assertEqual(self.db.get_download("scan-threat").status, "threat_detected")

    def test_the_files_tab_shows_the_checksum_as_a_tooltip(self):
        """The Files row carries the digest, so it is visible without opening Overview."""
        self._add_completed_entry("tip-1", total=16, content_hash="c" * 64)

        panel = DetailsPanel(self.manager)
        self.addCleanup(panel.deleteLater)
        panel.set_download_id("tip-1")

        item = panel._tree_files.topLevelItem(0)
        self.assertIsNotNone(item)
        self.assertEqual(item.toolTip(4), "SHA-256: " + "c" * 64)
        self.assertIn("SHA-256: " + "c" * 64, item.toolTip(0))

    def test_the_panel_updates_when_the_manager_announces_the_checksum(self):
        """_on_checksum_computed fills the Overview row for the selected download."""
        self._add_completed_entry("live-1", total=16)

        panel = DetailsPanel(self.manager)
        self.addCleanup(panel.deleteLater)
        panel.set_download_id("live-1")
        self.assertEqual(panel._ov_hash.text(), "—")

        self.manager.checksum_computed.emit("live-1", "d" * 64)
        self.assertEqual(panel._ov_hash.text(), "d" * 64)
        self.assertFalse(panel._ov_hash_copy_btn.isHidden())

        # A different download's hash must not overwrite the open one.
        self.manager.checksum_computed.emit("other-id", "e" * 64)
        self.assertEqual(panel._ov_hash.text(), "d" * 64)

    def test_the_overview_hash_row_prefers_the_torrent_infohash(self):
        """infohash > content_hash > etag, and no hash hides the copy button."""
        entry = self._add_completed_entry("hash-order", total=16, content_hash="f" * 64)
        entry.torrent_info_hash = "a" * 40
        entry.etag = '"deadbeef"'
        self.db.update_download(entry)

        panel = DetailsPanel(self.manager)
        self.addCleanup(panel.deleteLater)
        panel.set_download_id("hash-order")
        self.assertEqual(panel._ov_hash.text(), "a" * 40)

        entry = self.db.get_download("hash-order")
        entry.torrent_info_hash = ""
        entry.content_hash = ""
        self.db.update_download(entry)
        panel.set_download_id("hash-order")
        self.assertEqual(panel._ov_hash.text(), '"deadbeef"')

        entry = self.db.get_download("hash-order")
        entry.etag = ""
        self.db.update_download(entry)
        panel.set_download_id("hash-order")
        self.assertEqual(panel._ov_hash.text(), "—")
        self.assertTrue(panel._ov_hash_copy_btn.isHidden())

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
        self.assertEqual(self.checksums, [("test-legacy-1", expected_hash)],
                         "the panel and the model must learn the new hash")

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

        # The setUp collector is a DirectConnection, so the signal is already
        # recorded by the time the database row lands - no event loop, and no
        # processEvents() poll that would run whatever the rest of the session
        # left queued behind it.
        self.manager._compute_and_save_checksum("test-async-1")

        self.assertTrue(_wait_until(
            lambda: self.db.get_download("test-async-1").content_hash == expected_hash))

        updated = self.db.get_download("test-async-1")
        self.assertEqual(updated.content_hash, expected_hash)
        self.assertEqual(self.checksums, [("test-async-1", expected_hash)])

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
