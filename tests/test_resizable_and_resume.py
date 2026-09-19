"""Unit tests for column resizing and crash/kill/shutdown resume behavior."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from PySide6.QtWidgets import QApplication, QHeaderView
from PySide6.QtCore import Qt, QSettings

from my_idm.database import Database, DownloadEntry
from my_idm.download_model import Col
from my_idm.manager import DownloadManager
from my_idm.main_window import MainWindow

app = QApplication.instance() or QApplication([])


class TestResizableColumns(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.manager = DownloadManager(self.db)
        self.win = MainWindow(self.manager)

    def tearDown(self):
        self.win.close()
        self.db.close()
        QSettings("MyIDM", "My-IDM").clear()

    def test_all_columns_are_interactive_resizable(self):
        """Every column in the table must have ResizeMode.Interactive so users can drag borders."""
        header = self.win._table.horizontalHeader()
        self.assertFalse(header.stretchLastSection())
        for col in range(Col.COUNT):
            mode = header.sectionResizeMode(col)
            self.assertEqual(
                mode, QHeaderView.ResizeMode.Interactive,
                f"Column {col} ({Col.HEADERS[col]}) should be Interactive, got {mode}"
            )

    def test_column_width_can_be_resized(self):
        """Resizing a column changes its width properly."""
        header = self.win._table.horizontalHeader()
        header.resizeSection(Col.NAME, 350)
        self.assertEqual(self.win._table.columnWidth(Col.NAME), 350)

        header.resizeSection(Col.SAVE_PATH, 400)
        self.assertEqual(self.win._table.columnWidth(Col.SAVE_PATH), 400)

    def test_column_widths_persist_in_qsettings(self):
        """Header state is saved on close and restored on open."""
        header = self.win._table.horizontalHeader()
        header.resizeSection(Col.NAME, 380)

        # Simulate closeEvent
        settings = QSettings("MyIDM", "My-IDM")
        settings.setValue("header_state", header.saveState())

        # Create second window and check restore
        win2 = MainWindow(self.manager)
        try:
            self.assertEqual(win2._table.columnWidth(Col.NAME), 380)
        finally:
            win2.close()


class TestResumeAfterShutdownOrCrash(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.manager = DownloadManager(self.db)

    def tearDown(self):
        self.db.close()

    def test_manual_resume_resets_exhausted_retries(self):
        """Resuming an errored download with exhausted retries resets retry_count to 0."""
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

        # Before resume: retry_count is 5
        self.assertEqual(self.db.get_download("dl-error-1").retry_count, 5)

        # Manually resume
        self.manager.resume_download("dl-error-1")

        # After resume: retry_count is 0, error_message is cleared, status is queued/downloading
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

        # Should be marked queued for auto-resume
        updated = self.db.get_download("dl-active-1")
        self.assertEqual(updated.status, "queued")


if __name__ == "__main__":
    unittest.main()
