"""Unit tests for MainWindow, toolbar layout, column resizing, and UI interactions."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QHeaderView, QLabel, QToolBar, QToolButton

from my_idm.database import Database, DownloadEntry
from my_idm.download_model import Col
from my_idm.main_window import MainWindow
from my_idm.manager import DownloadManager

app = QApplication.instance() or QApplication([])


class TestMainWindowToolbar(unittest.TestCase):
    """Tests for toolbar actions, simplified buttons, and footer status."""

    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.manager = DownloadManager(self.db)
        self.win = MainWindow(self.manager)

    def tearDown(self):
        self.win.close()
        self.manager.stop()
        self.db.close()

    def test_merged_add_button_on_toolbar(self):
        """Toolbar should have a single merged Add Download button."""
        toolbar = self.win.findChild(QToolBar)
        self.assertIsNotNone(toolbar)

        actions = toolbar.actions()
        action_texts = [a.text() for a in actions if not a.isSeparator()]

        self.assertIn("➕ Add Download", action_texts)
        self.assertNotIn("📦 Add Torrent", action_texts)
        self.assertNotIn("📦 Add Torrent File…", action_texts)

    def test_removed_buttons_from_toolbar(self):
        """Open File, Open Folder, and Details Panel buttons should be removed from toolbar."""
        toolbar = self.win.findChild(QToolBar)
        self.assertIsNotNone(toolbar)

        actions = toolbar.actions()
        self.assertNotIn(self.win._act_open_file, actions)
        self.assertNotIn(self.win._act_open_folder, actions)
        self.assertNotIn(self.win._act_toggle_details, actions)

    def test_icon_only_buttons_on_toolbar(self):
        """Play and Pause must be icon-only on the toolbar, while Delete, Move, Recheck, and Preferences show text."""
        toolbar = self.win.findChild(QToolBar)
        self.assertIsNotNone(toolbar)

        for act in (
            self.win._act_resume,
            self.win._act_pause,
        ):
            btn = toolbar.widgetForAction(act)
            self.assertIsInstance(btn, QToolButton)
            self.assertEqual(
                btn.toolButtonStyle(),
                Qt.ToolButtonStyle.ToolButtonIconOnly,
                f"Action {act.text()} must be icon-only on the toolbar",
            )
            self.assertFalse(
                act.icon().isNull(),
                f"Action {act.text()} must have a non-null QIcon so Qt does not fall back to text",
            )

        for act in (
            self.win._act_delete,
            self.win._act_move,
            self.win._act_recheck,
            self.win._act_preferences,
        ):
            btn = toolbar.widgetForAction(act)
            self.assertIsInstance(btn, QToolButton)
            self.assertEqual(
                btn.toolButtonStyle(),
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon,
                f"Action {act.text()} should display text beside icon",
            )

    def test_toolbar_has_no_logo(self):
        """Toolbar must not contain a logo widget."""
        toolbars = self.win.findChildren(QToolBar)
        self.assertTrue(len(toolbars) > 0)
        main_tb = toolbars[0]
        labels = main_tb.findChildren(QLabel)
        self.assertEqual(len(labels), 0, "Toolbar should not have a logo QLabel widget")

    def test_footer_active_and_total_count(self):
        """Footer displays 'X Downloads, Y Active'."""
        self.assertIn("Downloads", self.win._count_label.text())
        self.assertIn("Active", self.win._count_label.text())


class TestMainWindowTableAndInteractions(unittest.TestCase):
    """Tests for table view, interactive resizing, geometry persistence, and actions."""

    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.manager = DownloadManager(self.db)
        self.win = MainWindow(self.manager)

    def tearDown(self):
        self.win.close()
        self.manager.stop()
        self.db.close()

    def test_all_columns_are_interactive_resizable(self):
        """Every column in the table must have ResizeMode.Interactive so users can drag borders."""
        header = self.win._table.horizontalHeader()
        self.assertFalse(header.stretchLastSection())
        self.assertFalse(
            header.cascadingSectionResizes(),
            "Cascading resizes must be False so resizing shifts subsequent columns",
        )
        for col in range(Col.COUNT):
            mode = header.sectionResizeMode(col)
            self.assertEqual(
                mode,
                QHeaderView.ResizeMode.Interactive,
                f"Column {col} ({Col.HEADERS[col]}) should be Interactive, got {mode}",
            )

    def test_resizing_column_shifts_subsequent_columns(self):
        """Resizing a column must shift all subsequent columns right/left without compressing them."""
        header = self.win._table.horizontalHeader()
        self.win.show()

        initial_widths = [header.sectionSize(i) for i in range(Col.COUNT)]
        pos_col1_before = header.sectionViewportPosition(Col.SIZE)
        pos_col2_before = header.sectionViewportPosition(Col.PROGRESS)

        delta = 100
        new_name_w = initial_widths[Col.NAME] + delta
        header.resizeSection(Col.NAME, new_name_w)

        self.assertEqual(header.sectionSize(Col.NAME), new_name_w)
        self.assertEqual(header.sectionSize(Col.SIZE), initial_widths[Col.SIZE])
        self.assertEqual(header.sectionSize(Col.PROGRESS), initial_widths[Col.PROGRESS])
        self.assertEqual(header.sectionViewportPosition(Col.SIZE), pos_col1_before + delta)
        self.assertEqual(header.sectionViewportPosition(Col.PROGRESS), pos_col2_before + delta)

    def test_column_width_can_be_resized(self):
        """Resizing a column changes its width properly."""
        header = self.win._table.horizontalHeader()
        header.resizeSection(Col.NAME, 350)
        self.assertEqual(self.win._table.columnWidth(Col.NAME), 350)

        header.resizeSection(Col.SAVE_PATH, 400)
        self.assertEqual(self.win._table.columnWidth(Col.SAVE_PATH), 400)

    def test_column_widths_persist_in_qsettings(self):
        """Header state is saved and restored."""
        header = self.win._table.horizontalHeader()
        header.resizeSection(Col.NAME, 380)

        settings = QSettings("MyIDM", "My-IDM")
        settings.setValue("header_state", header.saveState())

        win2 = MainWindow(self.manager)
        try:
            self.assertEqual(win2._table.columnWidth(Col.NAME), 380)
        finally:
            win2.close()

    def test_window_geometry_location_maximized_columns_in_db(self):
        """Window size, location, maximized state, and column lengths are persisted and restored via DB."""
        self.win.resize(1280, 720)
        self.win.move(150, 120)
        self.win._table.setColumnWidth(Col.NAME, 360)
        self.win._table.setColumnWidth(Col.SIZE, 130)

        self.win._save_ui_state_to_db()

        db_state = self.db.get_window_state()
        self.assertIsNotNone(db_state)
        self.assertEqual(db_state["width"], 1280)
        self.assertEqual(db_state["height"], 720)
        self.assertEqual(db_state["x"], 150)
        self.assertEqual(db_state["y"], 120)
        self.assertFalse(db_state["is_maximized"])
        self.assertEqual(db_state["column_widths"][str(Col.NAME)], 360)
        self.assertEqual(db_state["column_widths"][str(Col.SIZE)], 130)

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

    def test_double_click_calls_open_file(self):
        """Double clicking table view triggers file open."""
        with patch.object(self.win, "_on_open_file") as mock_open:
            index = self.win._model.index(0, 0)
            self.win._on_table_double_clicked(index)
            mock_open.assert_called_once()

    def test_open_file_missing_triggers_file_not_found(self):
        """Opening a missing file should mark status as file_not_found."""
        e = DownloadEntry(
            id="d1",
            url="http://example.com/nonexistent.zip",
            filename="nonexistent.zip",
            file_path="C:/nonexistent_file_path_12345.zip",
            save_path="C:/",
            status="completed",
        )
        self.db.add_download(e)
        self.win._load_history()

        self.win._table.selectRow(0)
        self.win._on_open_file()

        self.assertEqual(self.db.get_download("d1").status, "file_not_found")

    def test_copy_url_to_clipboard(self):
        """MainWindow._on_copy_url successfully copies URL/magnet without error."""
        cb = QGuiApplication.clipboard()
        orig = cb.text() if cb else ""

        entry = DownloadEntry(
            id="test_copy",
            url="magnet:?xt=urn:btih:fedcba9876543210&dn=real_movie",
            filename="real_movie",
            save_path=tempfile.gettempdir(),
            download_type="torrent",
            status="downloading",
        )
        self.db.add_download(entry)
        self.win._load_history()

        try:
            self.win._table.selectRow(0)
            self.win._on_copy_url()
            clipboard = QGuiApplication.clipboard()
            self.assertEqual(clipboard.text(), entry.url)
            self.assertIn("Copied Magnet link", self.win._status_label.text())
        finally:
            if cb:
                cb.setText(orig)

    def test_main_window_receives_filename_resolved(self):
        """MainWindow updates model when manager emits filename_resolved."""
        entry = DownloadEntry(
            id="test-2",
            url="https://example.com/api/get",
            filename="",
        )
        self.win._model.load_entries([entry])

        self.manager.filename_resolved.emit("test-2", "dynamic_file.zip")
        self.assertEqual(self.win._model.get_entry(0).filename, "dynamic_file.zip")
        self.assertEqual(self.win._model.data(self.win._model.index(0, Col.NAME)), "dynamic_file.zip")

    def test_force_start_action_and_context_menu(self):
        """Force start action is in Edit menu, context menu, and calls manager.force_start_download."""
        self.assertIsNotNone(self.win._act_force_start)
        self.assertEqual(self.win._act_force_start.text(), "Force Start")

        entry = DownloadEntry(
            id="d-fs-1",
            url="https://example.com/data.bin",
            filename="data.bin",
            status="paused",
        )
        self.db.add_download(entry)
        self.win._load_history()
        self.win._table.selectRow(0)

        with patch.object(self.manager, "force_start_download") as mock_fs:
            self.win._act_force_start.trigger()
            mock_fs.assert_called_once_with("d-fs-1")

    def test_context_menu_actions_have_icons_and_clean_text(self):
        """All context menu actions must have valid icons and clean text without leading emojis."""
        context_actions = [
            self.win._act_resume,
            self.win._act_force_start,
            self.win._act_pause,
            self.win._act_move_up,
            self.win._act_move_down,
            self.win._act_copy_url,
            self.win._act_scan_antivirus,
            self.win._act_recheck,
            self.win._act_move,
            self.win._act_open_file,
            self.win._act_open_folder,
            self.win._act_delete,
        ]
        for act in context_actions:
            self.assertFalse(act.icon().isNull(), f"Action '{act.text()}' must have a valid QIcon")
            self.assertTrue(
                act.text()[0].isalnum(),
                f"Action '{act.text()}' should have clean text without leading emoji",
            )


if __name__ == "__main__":
    unittest.main()
