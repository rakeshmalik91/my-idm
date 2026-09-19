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
        self.assertFalse(header.cascadingSectionResizes(), "Cascading resizes must be False so resizing shifts subsequent columns")
        for col in range(Col.COUNT):
            mode = header.sectionResizeMode(col)
            self.assertEqual(
                mode, QHeaderView.ResizeMode.Interactive,
                f"Column {col} ({Col.HEADERS[col]}) should be Interactive, got {mode}"
            )

    def test_resizing_column_shifts_subsequent_columns(self):
        """Resizing a column must shift all subsequent columns right/left without compressing them."""
        header = self.win._table.horizontalHeader()
        self.win.show()

        # Record initial widths and positions
        initial_widths = [header.sectionSize(i) for i in range(Col.COUNT)]
        pos_col1_before = header.sectionViewportPosition(Col.SIZE)
        pos_col2_before = header.sectionViewportPosition(Col.PROGRESS)

        # Enlarge Col.NAME by 100px
        delta = 100
        new_name_w = initial_widths[Col.NAME] + delta
        header.resizeSection(Col.NAME, new_name_w)

        # Col.NAME should be enlarged
        self.assertEqual(header.sectionSize(Col.NAME), new_name_w)
        # Col.SIZE and Col.PROGRESS must retain their exact widths (NOT shrunk)
        self.assertEqual(header.sectionSize(Col.SIZE), initial_widths[Col.SIZE])
        self.assertEqual(header.sectionSize(Col.PROGRESS), initial_widths[Col.PROGRESS])
        # Col.SIZE and Col.PROGRESS positions must have shifted by delta
        self.assertEqual(header.sectionViewportPosition(Col.SIZE), pos_col1_before + delta)
        self.assertEqual(header.sectionViewportPosition(Col.PROGRESS), pos_col2_before + delta)

    def test_toolbar_has_no_logo(self):
        """Toolbar must not contain a logo widget."""
        from PySide6.QtWidgets import QToolBar, QLabel
        toolbars = self.win.findChildren(QToolBar)
        self.assertTrue(len(toolbars) > 0)
        main_tb = toolbars[0]
        # Verify no QLabel with pixmap in toolbar
        labels = main_tb.findChildren(QLabel)
        self.assertEqual(len(labels), 0, "Toolbar should not have a logo QLabel widget")

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

    def test_window_geometry_location_maximized_columns_in_db(self):
        """Window size, location, maximized state, and column lengths are persisted and restored via DB."""
        # Set size and location
        self.win.resize(1280, 720)
        self.win.move(150, 120)

        # Set column lengths
        self.win._table.setColumnWidth(Col.NAME, 360)
        self.win._table.setColumnWidth(Col.SIZE, 130)

        # Save to DB
        self.win._save_ui_state_to_db()

        # Verify DB directly
        db_state = self.db.get_window_state()
        self.assertIsNotNone(db_state)
        self.assertEqual(db_state["width"], 1280)
        self.assertEqual(db_state["height"], 720)
        self.assertEqual(db_state["x"], 150)
        self.assertEqual(db_state["y"], 120)
        self.assertFalse(db_state["is_maximized"])
        self.assertEqual(db_state["column_widths"][str(Col.NAME)], 360)
        self.assertEqual(db_state["column_widths"][str(Col.SIZE)], 130)

        # Create a second window backed by the same database and verify restoration
        win2 = MainWindow(self.manager)
        try:
            self.assertEqual(win2.width(), 1280)
            self.assertEqual(win2.height(), 720)
            self.assertEqual(win2.x(), 150)
            self.assertEqual(win2.y(), 120)
            self.assertEqual(win2._table.columnWidth(Col.NAME), 360)
            self.assertEqual(win2._table.columnWidth(Col.SIZE), 130)
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
