"""Unit tests for MainWindow, toolbar layout, column resizing, and UI interactions."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

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

        self.assertIn("Add Download", action_texts)
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
        """Resume, Pause, Stop, Delete, Move, and Recheck must be icon-only on toolbar, while Preferences shows text."""
        toolbar = self.win.findChild(QToolBar)
        self.assertIsNotNone(toolbar)

        for act in (
            self.win._act_resume,
            self.win._act_pause,
            self.win._act_stop,
            self.win._act_delete,
            self.win._act_move,
            self.win._act_recheck,
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
        settings = QSettings("MyIDM", "My-IDM")
        settings.remove("header_state")

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
            self.assertTrue(win2._table.horizontalHeader().sectionsMovable())
        finally:
            win2.close()

    def test_column_ordering_by_dragging_and_persistence(self):
        """Columns are movable by dragging and their reordered visual positions persist."""
        header = self.win._table.horizontalHeader()
        self.assertTrue(header.sectionsMovable())
        self.assertTrue(header.isFirstSectionMovable())

        # Move section Col.NAME (1) to visual index 3
        with patch.object(self.win, "_save_ui_state_to_db", wraps=self.win._save_ui_state_to_db) as mock_save:
            header.moveSection(Col.NAME, 3)
            self.assertEqual(header.visualIndex(Col.NAME), 3)
            mock_save.assert_called()

        # When creating a second window, the moved visual index is restored
        win2 = MainWindow(self.manager)
        try:
            header2 = win2._table.horizontalHeader()
            self.assertTrue(header2.sectionsMovable())
            self.assertEqual(header2.visualIndex(Col.NAME), 3)
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


    def test_bandwidth_allocation_context_menu_and_action(self):
        """Bandwidth allocation updates entry metadata and calls engine."""
        entry = DownloadEntry(
            id="bw-test-1",
            url="https://example.com/file.zip",
            filename="file.zip",
            status="downloading",
        )
        self.db.add_download(entry)
        self.win._load_history()
        self.win._table.selectRow(0)

        self.win._on_set_bandwidth_allocation("low")
        self.assertEqual(self.manager.get_download_bandwidth_allocation("bw-test-1"), "low")

        self.win._on_set_bandwidth_allocation("high")
        self.assertEqual(self.manager.get_download_bandwidth_allocation("bw-test-1"), "high")

    def test_speed_limits_and_footer_context_menu(self):
        """Footer speed limit context menu adjusts global bandwidth limits and updates speed label."""
        from my_idm.main_window import SPEED_LIMIT_PRESETS
        labels = [label.lower().replace(" ", "") for label, _ in SPEED_LIMIT_PRESETS]
        expected_presets = [
            "unlimited", "1kbps", "2kbps", "5kbps", "10kbps", "50kbps",
            "100kbps", "200kbps", "500kbps", "1mbps", "2mbps", "5mbps",
            "10mbps", "100mbps",
        ]
        for ep in expected_presets:
            self.assertIn(ep, labels, f"Preset '{ep}' must be present in speed presets")

        # Set download limit to 1 MB/s
        self.win._set_speed_limit(1048576, is_upload=False)
        self.assertEqual(self.manager.network_config.download_limit, 1048576)

        # Set upload limit to 50 KB/s
        self.win._set_speed_limit(51200, is_upload=True)
        self.assertEqual(self.manager.network_config.upload_limit, 51200)

        # Speed label must reflect active limits
        lbl_text = self.win._speed_label.text()
        self.assertIn("Limit: 1.0 MiB/s", lbl_text)
        self.assertIn("Limit: 50.0 KiB/s", lbl_text)

    def test_menubar_actions_icons_and_alignment(self):
        """Menubar actions have icons to maintain uniform vertical text indentation."""
        menubar = self.win.menuBar()
        menus = {act.text(): act.menu() for act in menubar.actions()}

        # File menu
        file_menu = menus.get("&File")
        self.assertIsNotNone(file_menu)
        for act in file_menu.actions():
            if not act.isSeparator():
                self.assertFalse(act.icon().isNull(), f"Action '{act.text()}' in File menu must have an icon")

        # View menu
        view_menu = menus.get("&View")
        self.assertIsNotNone(view_menu)
        for act in view_menu.actions():
            if not act.isSeparator():
                if act.menu():
                    self.assertFalse(act.menu().icon().isNull(), f"Submenu '{act.text()}' in View menu must have an icon")
                else:
                    self.assertFalse(act.icon().isNull(), f"Action '{act.text()}' in View menu must have an icon")

        # Tools menu
        tools_menu = menus.get("&Tools")
        self.assertIsNotNone(tools_menu)
        for act in tools_menu.actions():
            if not act.isSeparator():
                self.assertFalse(act.icon().isNull(), f"Action '{act.text()}' in Tools menu must have an icon")
        self.assertFalse(self.win._act_tor.icon().isNull())
        self.assertEqual(self.win._act_tor.text(), "Tor: OFF")

        # Help menu
        help_menu = menus.get("&Help")
        self.assertIsNotNone(help_menu)
        for act in help_menu.actions():
            if not act.isSeparator():
                self.assertFalse(act.icon().isNull(), f"Action '{act.text()}' in Help menu must have an icon")

    def test_copy_multiple_urls_to_clipboard(self):
        """MainWindow._on_copy_url with multiple selected rows copies URLs joined by newline."""
        cb = QGuiApplication.clipboard()
        orig = cb.text() if cb else ""

        e1 = DownloadEntry(
            id="test_copy_1",
            url="https://example.com/file1.zip",
            filename="file1.zip",
            status="completed",
        )
        e2 = DownloadEntry(
            id="test_copy_2",
            url="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=test_mag",
            filename="test_mag",
            status="downloading",
        )
        self.db.add_download(e1)
        self.db.add_download(e2)
        self.win._load_history()

        try:
            self.win._table.selectAll()
            self.win._on_copy_url()
            copied_lines = cb.text().splitlines()
            self.assertEqual(set(copied_lines), {e1.url, e2.url})
            self.assertEqual(len(copied_lines), 2)
            self.assertIn("Copied 2 URLs/Magnets to clipboard", self.win._status_label.text())
        finally:
            if cb:
                cb.setText(orig)

    def test_rename_action_triggers_rename_download(self):
        """MainWindow._on_rename triggers manager.rename_download and updates model."""
        e = DownloadEntry(
            id="d_rename_1",
            url="https://example.com/old_name.iso",
            filename="old_name.iso",
            save_path=tempfile.gettempdir(),
            status="completed",
        )
        self.db.add_download(e)
        self.win._load_history()
        self.win._table.selectRow(0)

        mock_dlg = MagicMock()
        mock_dlg.exec.return_value = 1  # Accepted
        mock_dlg.new_name = "new_name.iso"
        with patch("my_idm.main_window.RenameDialog", return_value=mock_dlg):
            with patch.object(self.manager, "rename_download", return_value=(True, "")) as mock_ren:
                self.win._act_rename.trigger()
                mock_ren.assert_called_once_with("d_rename_1", "new_name.iso")

        # Emitting download_renamed updates table model row
        self.manager.download_renamed.emit("d_rename_1", "new_name.iso")
        self.assertEqual(self.win._model.get_entry(0).filename, "new_name.iso")
        self.assertEqual(self.win._model.data(self.win._model.index(0, Col.NAME)), "new_name.iso")

    def test_on_add_multiple_urls_queues_all(self):
        """MainWindow._on_add adds each URL from dlg.urls."""
        mock_dlg = MagicMock()
        mock_dlg.exec.return_value = 1  # Accepted
        mock_dlg.urls = [
            "https://example.com/batch1.zip",
            "https://example.com/batch2.zip",
        ]
        mock_dlg.save_path = "D:/Downloads"
        mock_dlg.num_segments = 8

        with patch("my_idm.main_window.AddDownloadDialog", return_value=mock_dlg) as mock_cls:
            mock_cls.DialogCode.Accepted = 1
            with patch.object(self.manager, "add_download") as mock_add:
                self.win._on_add()
                self.assertEqual(mock_add.call_count, 2)
                mock_add.assert_any_call("https://example.com/batch1.zip", "D:/Downloads", 8)
                mock_add.assert_any_call("https://example.com/batch2.zip", "D:/Downloads", 8)

    def test_speed_label_left_click_opens_menu(self):
        """Left clicking the footer speed label should invoke _show_speed_context_menu."""
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtCore import QPoint, QPointF, QEvent

        with patch.object(self.win, "_show_speed_context_menu") as mock_menu:
            press_event = QMouseEvent(
                QEvent.Type.MouseButtonPress,
                QPointF(5.0, 5.0),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            self.win._speed_label.mousePressEvent(press_event)
            mock_menu.assert_called_once_with(QPoint(5, 5))


class TestHeaderViewAndFiltering(unittest.TestCase):
    """Tests for FilterHeaderView, sort indicators, and multiselect filter popup."""

    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.manager = DownloadManager(self.db)
        self.win = MainWindow(self.manager)

    def tearDown(self):
        self.win.close()
        self.manager.stop()
        self.db.close()

    def test_header_sort_indicator_and_painting(self):
        header = self.win._header_view
        self.assertTrue(header.isSortIndicatorShown())

        # Test setSortIndicator
        header.setSortIndicator(Col.ADDED, Qt.SortOrder.DescendingOrder)
        self.assertEqual(header.sortIndicatorSection(), Col.ADDED)
        self.assertEqual(header.sortIndicatorOrder(), Qt.SortOrder.DescendingOrder)

        header.setSortIndicator(Col.NAME, Qt.SortOrder.AscendingOrder)
        self.assertEqual(header.sortIndicatorSection(), Col.NAME)
        self.assertEqual(header.sortIndicatorOrder(), Qt.SortOrder.AscendingOrder)

        # Test filter button rect geometry calculations
        name_rect = header._get_filter_btn_rect(Col.NAME)
        self.assertFalse(name_rect.isEmpty())
        self.assertEqual(name_rect.width(), 16)
        self.assertEqual(name_rect.height(), 16)

        status_rect = header._get_filter_btn_rect(Col.STATUS)
        self.assertFalse(status_rect.isEmpty())
        self.assertEqual(status_rect.width(), 16)
        self.assertEqual(status_rect.height(), 16)

        # Non-filterable column has empty rect
        size_rect = header._get_filter_btn_rect(Col.SIZE)
        self.assertTrue(size_rect.isEmpty())

    def test_header_filter_button_click_opens_popup(self):
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtCore import QPointF, QEvent
        header = self.win._header_view

        # Get the filter button rect for Col.STATUS
        btn_rect = header._get_filter_btn_rect(Col.STATUS)
        self.assertFalse(btn_rect.isEmpty())

        # Simulate left-click directly on the filter button
        click_pt = btn_rect.center()
        press_event = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(click_pt.x(), click_pt.y()),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        header.mousePressEvent(press_event)

        # Active popup should have been created
        self.assertIsNotNone(header._active_popup)
        popup = header._active_popup
        self.assertEqual(popup._column, Col.STATUS)
        popup.close()

    def test_multiselect_filter_popup_interactions(self):
        from my_idm.header_view import MultiselectFilterPopup
        counts = {"downloading": 2, "completed": 1, "paused": 0}
        popup = MultiselectFilterPopup(Col.STATUS, None, counts, self.win)

        changes = []
        popup.filter_changed.connect(lambda col, keys: changes.append((col, keys)))

        # Initially all checked
        for cb in popup._checkboxes.values():
            self.assertTrue(cb.isChecked())

        # Uncheck downloading
        popup._checkboxes["downloading"].setChecked(False)
        self.assertTrue(len(changes) > 0)
        last_col, last_keys = changes[-1]
        self.assertEqual(last_col, Col.STATUS)
        self.assertNotIn("downloading", last_keys)

        # Select all
        popup._select_all()
        last_col, last_keys = changes[-1]
        self.assertIsNone(last_keys)  # None indicates all selected (unfiltered)

        popup.close()

    def test_count_label_filtered_indicator(self):
        # Add two downloads
        e1 = DownloadEntry(id="1", url="http://example.com/1", filename="1.zip", status="downloading", download_type="http")
        e2 = DownloadEntry(id="2", url="http://example.com/2", filename="2.zip", status="completed", download_type="http")
        self.win._model.load_entries([e1, e2])
        self.win._update_count_label()
        self.assertEqual(self.win._count_label.text(), "2 Downloads, 1 Active")

        # Apply filter
        self.win._model.set_status_filter({"downloading"})
        self.win._update_count_label()
        self.assertIn("Filtered", self.win._count_label.text())
        self.assertEqual(self.win._count_label.text(), "1 of 2 Downloads, 1 Active (Filtered)")

        # Clear filter
        self.win._model.clear_filters()
        self.win._update_count_label()
        self.assertNotIn("Filtered", self.win._count_label.text())
        self.assertEqual(self.win._count_label.text(), "2 Downloads, 1 Active")


if __name__ == "__main__":
    unittest.main()



