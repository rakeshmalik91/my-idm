"""Unit tests for DownloadManager lifecycle: startup auto-resume, queue ordering, error retries, and recheck verification."""

import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from unittest.mock import patch, MagicMock
from PySide6.QtWidgets import QApplication

from my_idm.database import Database, DownloadEntry, SegmentEntry
from my_idm.download_model import DownloadTableModel, Col
from my_idm.manager import DownloadManager

app = QApplication.instance() or QApplication([])


class TestManagerLifecycle(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test.db"
        self.db = Database(self.db_path)
        self.db.open()
        self.manager = DownloadManager(self.db)

    def tearDown(self):
        self.manager.stop()
        self.db.close()
        self.tmp_dir.cleanup()

    # -- Startup and shutdown ------------------------------------------------

    def test_startup_resumes_queued_and_interrupted_downloads(self):
        """Startup automatically resumes interrupted downloads and queued items."""
        e_active = DownloadEntry(id="d1", url="http://example.com/1.zip", filename="1.zip", save_path="/tmp", status="downloading")
        e_queued = DownloadEntry(id="d2", url="http://example.com/2.zip", filename="2.zip", save_path="/tmp", status="queued")
        e_paused = DownloadEntry(id="d3", url="http://example.com/3.zip", filename="3.zip", save_path="/tmp", status="paused")
        self.db.add_download(e_active)
        self.db.add_download(e_queued)
        self.db.add_download(e_paused)

        resumed = []
        with patch.object(self.manager, "resume_download", side_effect=lambda did: resumed.append(did)):
            self.manager.start()

        self.assertIn("d1", resumed)
        self.assertIn("d2", resumed)
        self.assertNotIn("d3", resumed)

    def test_clean_shutdown_marks_active_as_queued_for_auto_resume(self):
        """Active HTTP downloads are set to queued on stop() so next startup auto-resumes them."""
        import asyncio
        entry = DownloadEntry(
            id="dl-active-1",
            url="https://example.com/video.mp4",
            filename="video.mp4",
            save_path="C:/Downloads",
            status="downloading",
        )
        self.db.add_download(entry)

        async def run_test():
            self.manager._http._tasks["dl-active-1"] = asyncio.create_task(asyncio.sleep(10))
            await self.manager._http.stop()

        asyncio.run(run_test())
        updated = self.db.get_download("dl-active-1")
        self.assertEqual(updated.status, "queued")

    # -- Retries and resume --------------------------------------------------

    def test_manual_resume_resets_exhausted_retries(self):
        """Resuming an errored download resets retry_count to 0 and clears error message."""
        entry = DownloadEntry(
            id="dl-error-1",
            url="https://example.com/file.zip",
            filename="file.zip",
            save_path="C:/Downloads",
            status="error",
            retry_count=5,
            error_message="Single-stream download failed after 5 retries",
        )
        self.db.add_download(entry)
        self.assertEqual(self.db.get_download("dl-error-1").retry_count, 5)

        self.manager.resume_download("dl-error-1")
        updated = self.db.get_download("dl-error-1")
        self.assertEqual(updated.retry_count, 0)
        self.assertEqual(updated.error_message, "")
        self.assertIn(updated.status, ("queued", "downloading"))

    def test_manual_resume_paused_download(self):
        """Resuming a paused download changes state to queued/downloading."""
        entry = DownloadEntry(
            id="dl-paused-1",
            url="https://example.com/archive.tar",
            filename="archive.tar",
            save_path="C:/Downloads",
            status="paused",
        )
        self.db.add_download(entry)
        self.manager.resume_download("dl-paused-1")
        updated = self.db.get_download("dl-paused-1")
        self.assertIn(updated.status, ("queued", "downloading"))

    def test_force_start_download_lifecycle(self):
        """Force start immediately resets error/retries and sets status to downloading."""
        entry = DownloadEntry(
            id="dl-force-1",
            url="https://example.com/archive.zip",
            filename="archive.zip",
            save_path="C:/Downloads",
            status="error",
            retry_count=4,
            error_message="Connection timed out",
            download_type="http",
            total_size=1000,
        )
        self.db.add_download(entry)

        status_emitted = []
        self.manager.status_changed.connect(lambda did, st, msg: status_emitted.append((did, st)))

        self.manager.force_start_download("dl-force-1")
        updated = self.db.get_download("dl-force-1")
        self.assertEqual(updated.status, "downloading")
        self.assertEqual(updated.retry_count, 0)
        self.assertEqual(updated.error_message, "")
        self.assertIn(("dl-force-1", "downloading"), status_emitted)

    # -- Queue management ----------------------------------------------------

    def test_move_queue_up_and_down(self):
        """Move up and down swaps queue orders and emits queue_order_changed."""
        e1 = DownloadEntry(id="d1", url="http://example.com/1.zip", filename="1.zip", save_path="/tmp")
        e2 = DownloadEntry(id="d2", url="http://example.com/2.zip", filename="2.zip", save_path="/tmp")
        self.db.add_download(e1)
        self.db.add_download(e2)

        signals = []
        self.manager.queue_order_changed.connect(lambda: signals.append(True))

        self.manager.move_queue_up("d2")
        self.assertTrue(len(signals) > 0)
        self.assertEqual(self.db.get_download("d2").queue_order, 1)
        self.assertEqual(self.db.get_download("d1").queue_order, 2)

        self.manager.move_queue_down("d2")
        self.assertEqual(self.db.get_download("d2").queue_order, 2)
        self.assertEqual(self.db.get_download("d1").queue_order, 1)

    # -- File not found ------------------------------------------------------

    def test_mark_file_not_found(self):
        """mark_file_not_found sets status to 'file_not_found' and updates in DB."""
        e = DownloadEntry(id="d1", url="http://example.com/f1.zip", filename="f1.zip", save_path="/tmp", status="completed")
        self.db.add_download(e)

        statuses = []
        self.manager.status_changed.connect(lambda did, st, err: statuses.append((did, st)))
        self.manager.mark_file_not_found("d1")

        self.assertIn(("d1", "file_not_found"), statuses)
        self.assertEqual(self.db.get_download("d1").status, "file_not_found")

    # -- Recheck engine ------------------------------------------------------

    def test_recheck_missing_file_resets_progress_to_zero(self):
        """Recheck on a deleted file resets downloaded_size to 0 and status to queued."""
        test_file = Path(self.tmp_dir.name) / "deleted.zip"
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

        self.manager.recheck_download("recheck-deleted")

        self.assertEqual(len(status_events), 1)
        self.assertEqual(status_events[0], ("recheck-deleted", "queued", "File not found"))
        self.assertEqual(progress_events[0][1], 0)

        updated = self.db.get_download("recheck-deleted")
        self.assertEqual(updated.downloaded_size, 0)
        self.assertEqual(updated.status, "queued")

    def test_recheck_partial_preallocated_file_updates_from_segments(self):
        """Recheck with pre-allocated full size file reads actual written bytes from segments."""
        test_file = Path(self.tmp_dir.name) / "partial.bin"
        test_file.write_bytes(b"\x00" * 1_000_000)

        entry = DownloadEntry(
            id="recheck-partial",
            url="https://example.com/partial.bin",
            filename="partial.bin",
            file_path=str(test_file),
            save_path=self.tmp_dir.name,
            total_size=1_000_000,
            downloaded_size=1_000_000,
            status="completed",
            download_type="http",
        )
        self.db.add_download(entry)

        segs = [
            SegmentEntry(id="s1", download_id="recheck-partial", index=0,
                         start_byte=0, end_byte=499_999,
                         downloaded_bytes=500_000, status="completed"),
            SegmentEntry(id="s2", download_id="recheck-partial", index=1,
                         start_byte=500_000, end_byte=999_999,
                         downloaded_bytes=0, status="pending"),
        ]
        self.db.add_segments(segs)

        self.manager.recheck_download("recheck-partial")
        updated = self.db.get_download("recheck-partial")
        self.assertEqual(updated.downloaded_size, 500_000)
        self.assertEqual(updated.status, "paused")

    def test_recheck_complete_file_confirms_completed(self):
        """Recheck with all segments completed confirms completed status."""
        test_file = Path(self.tmp_dir.name) / "complete.bin"
        test_file.write_bytes(b"B" * 1_000_000)

        entry = DownloadEntry(
            id="recheck-complete",
            url="https://example.com/complete.bin",
            filename="complete.bin",
            file_path=str(test_file),
            save_path=self.tmp_dir.name,
            total_size=1_000_000,
            downloaded_size=900_000,
            status="paused",
            download_type="http",
        )
        self.db.add_download(entry)

        segs = [
            SegmentEntry(id="c1", download_id="recheck-complete", index=0,
                         start_byte=0, end_byte=499_999,
                         downloaded_bytes=500_000, status="completed"),
            SegmentEntry(id="c2", download_id="recheck-complete", index=1,
                         start_byte=500_000, end_byte=999_999,
                         downloaded_bytes=500_000, status="completed"),
        ]
        self.db.add_segments(segs)

        self.manager.recheck_download("recheck-complete")
        updated = self.db.get_download("recheck-complete")
        self.assertEqual(updated.status, "completed")

    def test_recheck_resets_file_not_found(self):
        """Rechecking a download that had file_not_found status resets it."""
        e = DownloadEntry(id="d1", url="http://example.com/f1.zip", filename="f1.zip", save_path="/tmp", status="file_not_found")
        self.db.add_download(e)

        with patch.object(self.manager, "resume_download"):
            self.manager.recheck_download("d1")

        self.assertNotEqual(self.db.get_download("d1").status, "file_not_found")

    def test_recheck_file_not_found_model_progress_resets(self):
        """Model in-memory entry reflects 0 progress after recheck of missing file."""
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

        model = DownloadTableModel()
        model.load_entries([entry])
        self.manager.status_changed.connect(lambda did, st, err: model.update_status(did, st, err))
        self.manager.progress_updated.connect(
            lambda did, dl, total, spd, eta, s, p, up: model.update_progress(did, dl, total, spd, eta, s, p, up)
        )

        row = model._id_to_row["recheck-model"]
        prog_before = model.data(model.index(row, Col.PROGRESS))
        self.assertAlmostEqual(prog_before["progress"], 100.0)

        self.manager.recheck_download("recheck-model")
        prog_after = model.data(model.index(row, Col.PROGRESS))
        self.assertAlmostEqual(prog_after["progress"], 0.0)
        self.assertEqual(model.data(model.index(row, Col.STATUS)), "Queued")


if __name__ == "__main__":
    unittest.main()
