"""Unit tests for MainWindow, toolbar layout, column resizing, and UI interactions."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QToolBar,
    QToolButton,
)

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
        from my_idm.notifications import unregister_notification_handler
        unregister_notification_handler()
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
        """Open File and Open Folder buttons should not be on the toolbar."""
        toolbar = self.win.findChild(QToolBar)
        self.assertIsNotNone(toolbar)

        actions = toolbar.actions()
        self.assertNotIn(self.win._act_open_file, actions)
        self.assertNotIn(self.win._act_open_folder, actions)

    def test_details_and_console_footer_buttons_and_toolbar_removal(self):
        """Details panel button is removed from toolbar; footer has Details and Console toggle buttons."""
        self.win.show()
        toolbar = self.win.findChild(QToolBar)
        self.assertIsNotNone(toolbar)

        # Details button is removed from top toolbar
        btn = getattr(self.win, "_details_toolbar_btn", None)
        self.assertIsNone(btn, "_details_toolbar_btn should no longer exist on MainWindow")

        # Footer buttons exist and are visible
        details_btn = getattr(self.win, "_details_status_btn", None)
        console_btn = getattr(self.win, "_console_status_btn", None)
        self.assertIsNotNone(details_btn)
        self.assertIsNotNone(console_btn)
        self.assertTrue(details_btn.isVisible())
        self.assertTrue(console_btn.isVisible())

        # Initially details panel is visible in Details mode
        self.assertTrue(self.win._details_panel.isVisible())
        self.assertEqual(self.win._details_panel.current_mode(), "details")
        self.assertIn("ON", details_btn.text())
        self.assertIn("OFF", console_btn.text())

        # Click Console button on footer -> switches to Console mode
        console_btn.click()
        self.assertTrue(self.win._details_panel.isVisible())
        self.assertEqual(self.win._details_panel.current_mode(), "console")
        self.assertIn("OFF", details_btn.text())
        self.assertIn("ON", console_btn.text())

        # Click Console button again while active -> hides bottom panel
        console_btn.click()
        self.assertFalse(self.win._details_panel.isVisible())
        self.assertIn("OFF", details_btn.text())
        self.assertIn("OFF", console_btn.text())

        # Click Details button on footer -> opens panel in Details mode
        details_btn.click()
        self.assertTrue(self.win._details_panel.isVisible())
        self.assertEqual(self.win._details_panel.current_mode(), "details")
        self.assertIn("ON", details_btn.text())
        self.assertIn("OFF", console_btn.text())

    def test_icon_only_buttons_on_toolbar(self):
        """Resume, Pause, Stop, Delete, Move, and Recheck must be icon-only on toolbar, while Preferences shows text."""
        toolbar = self.win.findChild(QToolBar)
        self.assertIsNotNone(toolbar)

        for act in (
            self.win._act_resume,
            self.win._act_pause,
            self.win._act_stop,
            self.win._act_start_seeding,
            self.win._act_pause_all,
            self.win._act_stop_all_seeding,
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
        """Toolbar must not contain any logo or text label widgets."""
        toolbars = self.win.findChildren(QToolBar)
        self.assertTrue(len(toolbars) > 0)
        main_tb = toolbars[0]
        labels = main_tb.findChildren(QLabel)
        self.assertEqual(len(labels), 0, f"Toolbar should not have any labels: {[l.text() for l in labels]}")

    def test_footer_active_and_total_count(self):
        """Footer displays 'X Downloads, Y Active'."""
        self.assertIn("Downloads", self.win._count_label.text())
        self.assertIn("Active", self.win._count_label.text())

    def test_animepahe_footer_badge_lifecycle(self):
        """Footer badge for AnimePahe is hidden by default and becomes visible when scraper runs."""
        self.win.show()
        # Initially hidden when scraper is not running
        self.assertTrue(self.win._animepahe_status_btn.isHidden())

        # Scraper starts running
        self.manager.animepahe_status_changed.emit(True)
        self.assertFalse(self.win._animepahe_status_btn.isHidden())
        self.assertEqual(self.win._animepahe_status_btn.text(), "🎬 AnimePahe: Active")

        # Scraper stops running
        self.manager.animepahe_status_changed.emit(False)
        self.assertTrue(self.win._animepahe_status_btn.isHidden())

    def test_animepahe_footer_console_log_button_and_panel_toggle(self):
        """Clicking on console log button or menu from footer opens bottom panel and selects console tab."""
        self.win.show()
        # Console button is always visible on the footer
        self.assertFalse(self.win._animepahe_console_btn.isHidden())
        self.assertTrue(self.win._animepahe_console_btn.isVisible())

        # Start scraper -> status badge becomes active, console button remains visible
        self.manager.animepahe_status_changed.emit(True)
        self.assertTrue(self.win._animepahe_status_btn.isVisible())
        self.assertTrue(self.win._animepahe_console_btn.isVisible())

        # Hide details panel first to test that clicking console log button opens it
        self.win._act_toggle_details.setChecked(False)
        self.assertFalse(self.win._details_panel.isVisible())
        self.assertIn("OFF", self.win._animepahe_console_btn.text())

        # Click footer console log button -> opens panel in Console mode
        self.win._animepahe_console_btn.click()
        self.assertTrue(self.win._details_panel.isVisible())
        self.assertTrue(self.win._act_toggle_details.isChecked())
        self.assertTrue(self.win._details_panel.is_animepahe_console_active())
        self.assertIn("ON", self.win._animepahe_console_btn.text())

        # Clicking again while console tab is active toggles panel closed
        self.win._animepahe_console_btn.click()
        self.assertFalse(self.win._details_panel.isVisible())
        self.assertFalse(self.win._act_toggle_details.isChecked())
        self.assertIn("OFF", self.win._animepahe_console_btn.text())

        # Opening via menu action
        self.win._on_view_animepahe_console_log()
        self.assertTrue(self.win._details_panel.isVisible())
        self.assertTrue(self.win._details_panel.is_animepahe_console_active())
        self.assertIn("ON", self.win._animepahe_console_btn.text())

        # Scraper stops -> status badge hides, but console button remains visible always
        self.manager.animepahe_status_changed.emit(False)
        self.assertTrue(self.win._animepahe_status_btn.isHidden())
        self.assertTrue(self.win._animepahe_console_btn.isVisible())


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

        mock_cb = unittest.mock.MagicMock()
        with unittest.mock.patch("my_idm.main_window.QGuiApplication.clipboard", return_value=mock_cb):
            self.win._table.selectRow(0)
            self.win._on_copy_url()
            mock_cb.setText.assert_called_with(entry.url)
            self.assertIn("Copied Magnet link", self.win._status_label.text())

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

    def test_context_menu_selects_unselected_row(self):
        """Right-clicking an unselected row in table selects it for the context menu."""
        e1 = DownloadEntry(id="d_cm_1", url="https://example.com/1", filename="1.zip", save_path="D:/Downloads", added_at="2026-09-25T10:00:00Z")
        e2 = DownloadEntry(id="d_cm_2", url="https://example.com/2", filename="2.zip", save_path="D:/Downloads", added_at="2026-09-25T09:00:00Z")
        self.db.add_download(e1)
        self.db.add_download(e2)
        self.win._load_history()

        # Select row 0 initially
        self.win._table.selectRow(0)
        self.assertEqual(self.win._selected_ids(), ["d_cm_1"])

        # Context menu at row 1's rect
        rect = self.win._table.visualRect(self.win._model.index(1, 0))
        pos = rect.center()
        with patch("my_idm.main_window.QMenu") as mock_menu_cls:
            mock_menu = MagicMock()
            mock_menu_cls.return_value = mock_menu
            mock_menu.exec.return_value = None
            self.win._show_context_menu(pos)

        self.assertEqual(self.win._selected_ids(), ["d_cm_2"])

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
            pt = QPointF(5.0, 5.0)
            press_event = QMouseEvent(
                QEvent.Type.MouseButtonPress,
                pt,
                pt,
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

    def test_row_selection_survives_status_change_with_segregated_view(self):
        """Regression: any status change deselected the row under the cursor.

        Segregated view rebuilds the model on every status change and a model
        reset drops the view's selection.
        """
        self.win._model.set_segregated_view(True, mode="status")
        QApplication.processEvents()

        entry = next(
            (e for e in self.manager.db.get_all_downloads()
             if e.status == "completed" and e.download_type != "torrent"),
            None,
        )
        if entry is None:
            self.skipTest("no suitable entry")

        row = self.win._model.row_for_id(entry.id)
        self.assertIsNotNone(row)
        self.win._table.selectRow(row)
        QApplication.processEvents()
        self.assertIn(entry.id, self.win._selected_ids())

        self.win._model.update_status(entry.id, "downloading", "")
        QApplication.processEvents()
        fresh = self.manager.get_entry(entry.id)
        self.win._model.refresh_entry(entry.id, fresh)
        self.win._restore_selection(self.win._selected_ids() or [entry.id])
        QApplication.processEvents()

        self.assertIn(entry.id, self.win._selected_ids())

    def test_restore_selection_ignores_ids_no_longer_visible(self):
        self.win._model.load_entries([])
        # Must not raise when nothing is selected or nothing resolves.
        self.win._restore_selection([])
        self.win._restore_selection(["missing-id"])
        self.assertEqual(self.win._selected_ids(), [])

    def test_toolbar_search_box_is_present_and_filters(self):
        """The toolbar search box filters rows by name, URL, or domain."""
        self.win._model.load_entries([
            DownloadEntry(id="a", url="https://alpha.com/one.zip", filename="one.zip",
                          save_path=".", file_path="./one.zip", status="completed"),
            DownloadEntry(id="b", url="https://beta.com/two.zip", filename="two.zip",
                          save_path=".", file_path="./two.zip", status="completed"),
        ])
        self.assertEqual(self.win._model.rowCount(), 2)

        self.win._search_edit.setText("one")
        self.assertEqual(self.win._model.rowCount(), 1)
        self.assertEqual(self.win._model.search_query(), "one")
        self.assertTrue(self.win._model.is_searching())

        self.win._search_edit.setText("beta")
        self.assertEqual(self.win._model.rowCount(), 1)

        self.win._search_edit.setText("nothing-matches")
        self.assertEqual(self.win._model.rowCount(), 0)

        self.win._search_edit.clear()
        self.assertEqual(self.win._model.rowCount(), 2)
        self.assertFalse(self.win._model.is_searching())

    def test_toolbar_search_is_case_insensitive(self):
        self.win._model.load_entries([
            DownloadEntry(id="a", url="https://x.com/Movie.mkv", filename="Movie.mkv",
                          save_path=".", file_path="./Movie.mkv", status="completed"),
        ])
        self.win._search_edit.setText("movie")
        self.assertEqual(self.win._model.rowCount(), 1)
        self.win._search_edit.clear()

    def test_search_does_not_block_header_filter_clear(self):
        """Clearing the header filters must not wipe what the user is typing."""
        self.win._model.load_entries([
            DownloadEntry(id="a", url="https://x.com/a.zip", filename="a.zip",
                          save_path=".", file_path="./a.zip", status="completed"),
        ])
        self.win._search_edit.setText("a")
        self.win._model.set_status_filter({"completed"})
        self.win._model.clear_filters()
        self.assertEqual(self.win._model.search_query(), "a")
        self.win._search_edit.clear()

    def test_preferences_is_the_last_toolbar_control(self):
        actions = self.win._toolbar.actions()
        self.assertTrue(actions, "toolbar has no actions")
        self.assertIs(actions[-1], self.win._act_preferences)

    def test_toolbar_search_precedes_preferences(self):
        """The search box sits in the gap before the Settings button."""
        names = []
        for action in self.win._toolbar.actions():
            widget = self.win._toolbar.widgetForAction(action)
            if widget is not None:
                names.append(widget.objectName())
        self.assertIn("toolbar_search", names)
        self.assertIn("toolbar_gap", names)
        self.assertLess(names.index("toolbar_gap"), names.index("toolbar_search"))

    def test_toolbar_gap_expands(self):
        self.assertEqual(
            self.win._toolbar_gap.sizePolicy().horizontalPolicy(),
            QSizePolicy.Policy.Expanding,
        )

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

        # Size is filterable too
        size_rect = header._get_filter_btn_rect(Col.SIZE)
        self.assertFalse(size_rect.isEmpty())
        self.assertEqual(size_rect.width(), 16)
        self.assertEqual(size_rect.height(), 16)

        # A genuinely non-filterable column has an empty rect
        self.assertTrue(header._get_filter_btn_rect(Col.SPEED).isEmpty())
        self.assertTrue(header._get_filter_btn_rect(Col.PROGRESS).isEmpty())

    def test_header_filter_button_click_opens_popup(self):
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtCore import QPointF, QEvent
        header = self.win._header_view

        # Get the filter button rect for Col.STATUS
        btn_rect = header._get_filter_btn_rect(Col.STATUS)
        self.assertFalse(btn_rect.isEmpty())

        # Simulate left-click directly on the filter button
        click_pt = btn_rect.center()
        pt = QPointF(click_pt.x(), click_pt.y())
        press_event = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            pt,
            pt,
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

    def test_toolbar_stop_all_seeding_button(self):
        """Toolbar contains Stop All Seeding action and triggers manager.stop_all_seeding."""
        self.assertIn(self.win._act_stop_all_seeding, self.win._toolbar.actions())
        with unittest.mock.patch.object(self.manager, "stop_all_seeding", return_value=3) as mock_stop:
            self.win._act_stop_all_seeding.trigger()
            mock_stop.assert_called_once()
            self.assertIn("Stopped 3 seeding torrents", self.win._status_label.text())

    def test_toolbar_pause_all_downloads_button(self):
        """Toolbar contains Pause All action and triggers manager.pause_all_downloads."""
        self.assertIn(self.win._act_pause_all, self.win._toolbar.actions())
        with unittest.mock.patch.object(self.manager, "pause_all_downloads", return_value=2) as mock_pause_all:
            self.win._act_pause_all.trigger()
            mock_pause_all.assert_called_once()
            self.assertIn("Paused 2 downloads", self.win._status_label.text())

    def test_toolbar_start_seeding_button(self):
        """Toolbar contains Start Seeding action and triggers manager.start_seeding on selected downloads."""
        self.assertIn(self.win._act_start_seeding, self.win._toolbar.actions())
        with unittest.mock.patch.object(self.win, "_selected_ids", return_value=["dl-seed-1"]):
            with unittest.mock.patch.object(self.manager, "start_seeding") as mock_seed:
                self.win._act_start_seeding.trigger()
                mock_seed.assert_called_once_with("dl-seed-1")

    def test_stale_ui_state_adds_new_columns_at_end(self):
        """A state saved when the table had fewer columns must not scramble order."""
        from my_idm.main_window import _DEFAULT_TAIL_COLUMNS

        header = self.win._table.horizontalHeader()
        # The user had dragged Size to position 1 on a 14-column build.
        header.moveSection(header.visualIndex(Col.SIZE), 1)
        legacy = {
            "column_widths": {str(c): 120 for c in range(14)},
            "header_state": bytes(header.saveState().toHex()).decode(),
            "column_count": 14,
            "sort_column": Col.ADDED,
            "sort_order": 0,
        }

        with patch.object(self.manager, "get_ui_state", return_value=legacy):
            self.win._restore_ui_state_from_db()

        # New columns land at the very end...
        self.assertEqual(header.visualIndex(Col.SEEDING_STARTED_AT), Col.COUNT - 1)
        self.assertEqual(header.count(), Col.COUNT)
        # ...the tail is fully pinned...
        for i, col in enumerate(_DEFAULT_TAIL_COLUMNS):
            self.assertEqual(header.visualIndex(col), Col.COUNT - len(_DEFAULT_TAIL_COLUMNS) + i)
        # ...and the user's own ordering of the older columns survives.
        self.assertEqual(Col.HEADERS[header.logicalIndex(1)], "Size")

    def test_current_ui_state_respects_user_order(self):
        """Once the stored state matches the column count, order is left alone."""
        header = self.win._table.horizontalHeader()
        header.moveSection(header.visualIndex(Col.SOURCE), 0)
        current = {
            "column_widths": {str(c): 120 for c in range(Col.COUNT)},
            "header_state": bytes(header.saveState().toHex()).decode(),
            "column_count": Col.COUNT,
            "sort_column": Col.ADDED,
            "sort_order": 0,
        }
        with patch.object(self.manager, "get_ui_state", return_value=current):
            self.win._restore_ui_state_from_db()
        self.assertEqual(Col.HEADERS[header.logicalIndex(0)], "Source")

    def test_saved_ui_state_records_column_count(self):
        """column_count must be written so a later upgrade can detect a stale state."""
        self.win._save_ui_state_to_db()
        state = self.manager.get_ui_state()
        self.assertIsNotNone(state)
        self.assertEqual(state.get("column_count"), Col.COUNT)

    def test_default_column_order_places_source_domain_and_file_name_at_end(self):
        """The tail columns default to the end of the table, in the documented order."""
        header = self.win._table.horizontalHeader()
        from my_idm.main_window import _DEFAULT_TAIL_COLUMNS

        expected = {col: Col.COUNT - len(_DEFAULT_TAIL_COLUMNS) + i
                    for i, col in enumerate(_DEFAULT_TAIL_COLUMNS)}
        for col, visual in expected.items():
            self.assertEqual(
                header.visualIndex(col), visual,
                f"{Col.HEADERS[col]} should sit at visual {visual}",
            )

    def test_new_columns_default_to_the_very_end(self):
        """The appended columns sit at the end of the table by default."""
        from my_idm.main_window import _DEFAULT_TAIL_COLUMNS

        header = self.win._table.horizontalHeader()
        span = len(_DEFAULT_TAIL_COLUMNS)
        for i, col in enumerate(_DEFAULT_TAIL_COLUMNS):
            self.assertEqual(header.visualIndex(col), Col.COUNT - span + i, Col.HEADERS[col])
        self.assertEqual(header.visualIndex(Col.SEEDING_STARTED_AT), Col.COUNT - 1)

    def test_new_column_indices_do_not_shift_existing_columns(self):
        """Persisted column indices must keep pointing at the same columns."""
        self.assertEqual(Col.LAST_SEEDED, 14)
        self.assertEqual(Col.SOURCE, 15)
        self.assertEqual(Col.SEEDING_STARTED_AT, 16)
        for name, value in (
            ("QUEUE", 0), ("NAME", 1), ("SOURCE_DOMAIN", 2), ("SIZE", 3),
            ("PROGRESS", 4), ("STATUS", 5), ("SPEED", 6), ("ETA", 7),
            ("SEEDS_PEERS", 8), ("ADDED", 9), ("LAST_TRIED", 10),
            ("COMPLETED", 11), ("SAVE_PATH", 12), ("FILE_NAME", 13),
        ):
            self.assertEqual(getattr(Col, name), value, name)
        self.assertEqual(Col.COUNT, 17)
        self.assertEqual(Col.HEADERS[Col.LAST_SEEDED], "Last Seeded")
        self.assertEqual(Col.HEADERS[Col.SOURCE], "Source")
        self.assertEqual(Col.HEADERS[Col.SEEDING_STARTED_AT], "Seeding Started At")

    def test_reset_view_restores_new_column_order(self):
        header = self.win._table.horizontalHeader()
        header.moveSection(header.visualIndex(Col.SOURCE), 0)
        self.win._on_reset_view()
        self.assertEqual(header.visualIndex(Col.SEEDING_STARTED_AT), Col.COUNT - 1)
        self.assertEqual(header.visualIndex(Col.SOURCE), Col.COUNT - 2)

    def test_ui_state_restore_preserves_user_column_order(self):
        """Restoring UI state does not forcefully push File / Folder Name or Source Domain to the front."""
        header = self.win._table.horizontalHeader()
        # Move Col.FILE_NAME to the very last position
        header.moveSection(header.visualIndex(Col.FILE_NAME), Col.COUNT - 1)
        self.assertEqual(header.visualIndex(Col.FILE_NAME), Col.COUNT - 1)

        # Save UI state and restore it
        self.win._save_ui_state_to_db()
        self.win._restore_ui_state_from_db()

        # Must still be at the end, not forced back to index 2
        self.assertEqual(header.visualIndex(Col.FILE_NAME), Col.COUNT - 1)

    def test_export_selected_as_csv(self):
        """Exporting selected downloads creates a valid CSV file with Name and URL/Magnet columns."""
        e1 = DownloadEntry(
            id="csv-1",
            url="https://example.com/file1.zip",
            filename="file1.zip",
            status="completed",
        )
        e2 = DownloadEntry(
            id="csv-2",
            url="magnet:?xt=urn:btih:csvmagnet123",
            filename="my_torrent.mkv",
            status="seeding",
        )
        self.db.add_download(e1)
        self.db.add_download(e2)
        self.win._load_history()

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tf:
            csv_path = tf.name

        try:
            with unittest.mock.patch.object(self.win, "_selected_ids", return_value=["csv-1", "csv-2"]):
                with unittest.mock.patch("my_idm.main_window.QFileDialog.getSaveFileName", return_value=(csv_path, "CSV Files (*.csv)")):
                    self.win._act_export_csv.trigger()

            with open(csv_path, "r", encoding="utf-8") as f:
                content = f.read()

            lines = [line.strip() for line in content.splitlines() if line.strip()]
            self.assertEqual(lines[0], "Name,URL/Magnet")
            self.assertIn("file1.zip,https://example.com/file1.zip", lines)
            self.assertIn("my_torrent.mkv,magnet:?xt=urn:btih:csvmagnet123", lines)
        finally:
            if Path(csv_path).exists():
                Path(csv_path).unlink()

    def test_segregated_view_toggle_and_collapsible_sections(self):
        """Segregated view partitions items into Active, Seeding, Inactive sections and persists collapse state."""
        e_active = DownloadEntry(
            id="sec-active-1",
            url="https://example.com/active.zip",
            filename="active.zip",
            status="downloading",
        )
        e_seed = DownloadEntry(
            id="sec-seed-1",
            url="magnet:?xt=urn:btih:seed123",
            filename="seed.iso",
            status="seeding",
        )
        e_inact = DownloadEntry(
            id="sec-inact-1",
            url="https://example.com/done.zip",
            filename="done.zip",
            status="completed",
        )
        self.db.add_download(e_active)
        self.db.add_download(e_seed)
        self.db.add_download(e_inact)
        self.win._load_history()

        # Menu structure verification
        self.assertEqual(self.win._act_segregated_view.text(), "On")
        menu_actions = self.win._menu_segregated_view.actions()
        self.assertIn(self.win._act_segregated_view, menu_actions)
        self.assertIn(self.win._act_seg_by_status, menu_actions)
        self.assertIn(self.win._act_seg_by_date, menu_actions)

        # Turn on segregated view
        self.win._act_segregated_view.setChecked(True)
        self.assertTrue(self.win._model.is_segregated_view())
        self.assertTrue(self.db.get_ui_state("segregated_view_enabled"))

        # In segregated view, we have 3 headers + 3 items = 6 rows
        self.assertEqual(self.win._model.rowCount(), 6)
        hdr_indices = self.win._model.get_section_header_row_indices()
        self.assertEqual(len(hdr_indices), 3)

        # First header should be Active
        self.assertTrue(self.win._model.is_section_header_row(0))
        entry_h0 = self.win._model._entries[0]
        self.assertEqual(entry_h0.section_id, "active")

        # Collapse the Active section by clicking on header row 0
        self.win._on_table_clicked(self.win._model.index(0, 0))
        self.assertTrue(self.win._model._collapsed_sections)
        self.assertIn("active", self.win._model._collapsed_sections)
        self.assertTrue(self.db.get_ui_state("segregated_active_collapsed"))
        # With active collapsed, row count drops by 1
        self.assertEqual(self.win._model.rowCount(), 5)

        # Un-collapse Active section
        self.win._on_table_clicked(self.win._model.index(0, 0))
        self.assertNotIn("active", self.win._model._collapsed_sections)
        self.assertFalse(self.db.get_ui_state("segregated_active_collapsed"))
        self.assertEqual(self.win._model.rowCount(), 6)

        # Turn off segregated view -> back to flat list of 3 items
        self.win._act_segregated_view.setChecked(False)
        self.assertFalse(self.win._model.is_segregated_view())
        self.assertEqual(self.win._model.rowCount(), 3)

    def test_segregated_view_date_mode_and_persistence(self):
        """Date-based segregated view groups by Today, Yesterday, Last 7 Days, Last 30 Days, Older and persists settings."""
        from datetime import datetime, timedelta

        now = datetime.now().astimezone()
        e_today = DownloadEntry(
            id="d-today-1",
            url="https://example.com/1",
            filename="today.zip",
            status="completed",
            added_at=now.isoformat(),
        )
        e_yest = DownloadEntry(
            id="d-yest-1",
            url="https://example.com/2",
            filename="yest.zip",
            status="completed",
            added_at=(now - timedelta(days=1)).isoformat(),
        )
        e_older = DownloadEntry(
            id="d-older-1",
            url="https://example.com/3",
            filename="older.zip",
            status="completed",
            added_at=(now - timedelta(days=60)).isoformat(),
        )
        self.db.add_download(e_today)
        self.db.add_download(e_yest)
        self.db.add_download(e_older)
        self.win._load_history()

        # Switch to Date segregation via menu action
        self.win._act_seg_by_date.trigger()
        self.assertTrue(self.win._model.is_segregated_view())
        self.assertEqual(self.win._model.segregated_mode(), "date")
        self.assertEqual(self.db.get_ui_state("segregated_view_mode"), "date")
        self.assertTrue(self.db.get_ui_state("segregated_view_enabled"))

        # 5 headers + 3 items = 8 rows
        self.assertEqual(self.win._model.rowCount(), 8)

        # Collapse "Last 7 Days" and "Older" sections
        self.win._model.set_section_collapsed("date_last_7_days", True)
        self.db.set_ui_state("segregated_date_last_7_days_collapsed", True)
        self.win._model.set_section_collapsed("date_older", True)
        self.db.set_ui_state("segregated_date_older_collapsed", True)
        self.assertEqual(self.win._model.rowCount(), 7)

        # Create a new MainWindow with same db to verify next launch persistence
        win2 = MainWindow(self.win._manager)
        try:
            self.assertTrue(win2._segregated_view_enabled)
            self.assertEqual(win2._segregated_view_mode, "date")
            self.assertTrue(win2._model.is_segregated_view())
            self.assertEqual(win2._model.segregated_mode(), "date")
            self.assertTrue(win2._model.is_section_collapsed("date_last_7_days"))
            self.assertTrue(win2._model.is_section_collapsed("date_older"))
        finally:
            win2.close()

    def test_tools_menu_animepahe_actions(self):
        """Tools menu contains actions to launch AnimePahe GUI and External Tools settings."""
        self.assertIsNotNone(self.win._act_launch_animepahe_gui)
        self.assertIsNotNone(self.win._act_external_tools_settings)

        # Triggering when not configured prompts user
        with patch.object(self.win._manager.external_tools_config, "get_effective_repo_path", return_value=""):
            with patch("my_idm.main_window.QMessageBox.question") as mock_q:
                self.win._act_launch_animepahe_gui.trigger()
                mock_q.assert_called_once()

        # Triggering when configured launches GUI
        with patch.object(self.win._manager.external_tools_config, "get_effective_repo_path", return_value=tempfile.gettempdir()):
            with patch("my_idm.main_window.launch_animepahe_gui", return_value=(True, "Success")):
                self.win._act_launch_animepahe_gui.trigger()
                self.assertEqual(self.win._status_label.text(), "Launched AnimePahe Downloader GUI")

    def test_window_geometry_persistence_does_not_shift_on_relaunch(self):
        """Saving and restoring UI state across multiple launches preserves window position without shifting upwards."""
        self.win.resize(800, 600)
        self.win.move(300, 200)
        self.win.show()
        QApplication.processEvents()

        initial_pos = self.win.pos()
        initial_y = initial_pos.y()
        initial_x = initial_pos.x()
        self.win._save_ui_state_to_db()

        for launch_idx in range(3):
            next_win = MainWindow(self.manager)
            next_win.show()
            QApplication.processEvents()

            current_pos = next_win.pos()
            self.assertEqual(
                current_pos.y(),
                initial_y,
                f"Window shifted vertically on launch {launch_idx + 1}: {current_pos.y()} vs {initial_y}",
            )
            self.assertEqual(current_pos.x(), initial_x)
            next_win._save_ui_state_to_db()
            next_win.close()

    def test_legacy_window_geometry_restore_does_not_shift(self):
        """Restoring legacy UI state dictionary (x, y, width, height) positions window accurately without shift."""
        legacy_state = {"x": 350, "y": 250, "width": 820, "height": 610}
        self.manager.save_ui_state(legacy_state)

        next_win = MainWindow(self.manager)
        next_win.show()
        QApplication.processEvents()

        self.assertEqual(next_win.pos().x(), 350)
        self.assertEqual(next_win.pos().y(), 250)
        next_win.close()

    def test_about_dialog_contains_copyright(self):
        """About dialog displays the application information and copyright notice."""
        from unittest.mock import patch
        from PySide6.QtWidgets import QMessageBox

        captured_dialogs = []
        original_exec = QMessageBox.exec

        def _intercept_exec(dialog_self):
            captured_dialogs.append(dialog_self)
            return QMessageBox.StandardButton.Ok

        with patch.object(QMessageBox, "exec", _intercept_exec):
            self.win._on_about()
            self.assertEqual(len(captured_dialogs), 1)
            dlg = captured_dialogs[0]
            self.assertIn("© Rakesh Malik, 2026", dlg.informativeText())
            self.assertIn("My-IDM", dlg.text())

    def test_about_lists_advertised_features(self):
        """About text should mention the feature set the dialog advertises."""
        from unittest.mock import patch
        from PySide6.QtWidgets import QMessageBox

        captured = []

        def _intercept_exec(dialog_self):
            captured.append(dialog_self)
            return QMessageBox.StandardButton.Ok

        with patch.object(QMessageBox, "exec", _intercept_exec):
            self.win._on_about()

        info = captured[0].informativeText()
        for feature in (
            "multi-segment",
            "bittorrent",
            "tor",
            "vpn kill switch",
            "youtube",
            "animepahe",
            "malware",
        ):
            self.assertIn(
                feature, info.lower(), f"missing feature: {feature}"
            )

    def test_about_credits_ai_coauthors(self):
        """The About dialog credits the AI tools that co-authored the project."""
        from unittest.mock import patch
        from PySide6.QtWidgets import QMessageBox

        captured_dialogs = []

        def _intercept_exec(dialog_self):
            captured_dialogs.append(dialog_self)
            return QMessageBox.StandardButton.Ok

        with patch.object(QMessageBox, "exec", _intercept_exec):
            self.win._on_about()

        info = captured_dialogs[0].informativeText()
        self.assertIn("Co-authored with", info)
        for credit in (
            "Gemini 3.8 Flash",
            "Claude 4.6 Opus",
            "Nvidia Nemotron 3 Ultra",
            "Space Bunny Alpha",
            "Poolside Laguna S 2.1",
            "Antigravity IDE",
            "Kilo Code plugin for Antigravity IDE",
        ):
            self.assertIn(credit, info, f"missing credit: {credit}")

    def test_system_tray_setup_and_actions(self):
        """System tray icon and context menu actions are properly initialized."""
        if self.win._tray_icon is not None:
            self.assertEqual(self.win._tray_icon.toolTip(), "My-IDM — Download Manager")
            menu = self.win._tray_icon.contextMenu()
            self.assertIsNotNone(menu)
            action_texts = [a.text() for a in menu.actions() if not a.isSeparator()]
            self.assertTrue(any("Show" in t or "Hide" in t for t in action_texts))
            self.assertTrue(any("Pause All" in t for t in action_texts))
            self.assertTrue(any("Resume All" in t for t in action_texts))
            self.assertTrue(any("Preferences" in t for t in action_texts))
            self.assertTrue(any("Add Download" in t for t in action_texts))
            self.assertTrue(any("Exit" in t for t in action_texts))

    def test_system_tray_has_add_download_action(self):
        """Tray context menu exposes an Add Download entry."""
        if self.win._tray_icon is None:
            self.skipTest("system tray unavailable")
        menu = self.win._tray_icon.contextMenu()
        self.assertIsNotNone(menu)
        texts = [a.text() for a in menu.actions() if not a.isSeparator()]
        self.assertTrue(any("Add Download" in t for t in texts), texts)

    def test_tray_add_download_opens_dialog(self):
        """Triggering the tray action opens the Add Download dialog."""
        if self.win._tray_icon is None:
            self.skipTest("system tray unavailable")
        with patch.object(self.win, "_on_add") as mock_add:
            self.win._on_tray_add_download()
        mock_add.assert_called_once()

    def test_tray_add_download_restores_hidden_window(self):
        """The modal dialog's parent must be visible, so the window is restored first."""
        self.win.hide()
        self.assertFalse(self.win.isVisible())
        with patch.object(self.win, "_on_add"):
            self.win._on_tray_add_download()
        self.assertTrue(self.win.isVisible())

    def test_tray_menu_groups_restart_and_exit(self):
        """Restart and Exit share one group with no separator between them."""
        if self.win._tray_icon is None:
            self.skipTest("system tray unavailable")
        menu = self.win._tray_icon.contextMenu()
        actions = menu.actions()
        restart = next(a for a in actions if "Restart" in a.text())
        exit_ = next(a for a in actions if "Exit" in a.text())
        gap = actions[actions.index(restart) + 1 : actions.index(exit_)]
        self.assertFalse(
            any(a.isSeparator() for a in gap),
            "no separator expected between Restart and Exit",
        )

    def test_tray_has_about_action(self):
        if self.win._tray_icon is None:
            self.skipTest("system tray unavailable")
        menu = self.win._tray_icon.contextMenu()
        texts = [a.text() for a in menu.actions() if not a.isSeparator()]
        self.assertTrue(any("About" in t for t in texts), texts)

    def test_tray_about_restores_hidden_window(self):
        """The About dialog is parented to the window, so it must be restored."""
        from unittest.mock import patch
        from PySide6.QtWidgets import QMessageBox

        self.win.hide()
        self.assertFalse(self.win.isVisible())
        with patch.object(QMessageBox, "exec", lambda d: QMessageBox.StandardButton.Ok):
            self.win._on_about()
        self.assertTrue(self.win.isVisible())

    def test_tray_add_download_matches_tray_indentation(self):
        """Every tray entry must use the same style or the labels misalign.

        Qt reserves the icon column for the whole menu, so mixing an icon-based
        entry with glyph-in-text entries leaves the labels at different indents.
        """
        if self.win._tray_icon is None:
            self.skipTest("system tray unavailable")
        menu = self.win._tray_icon.contextMenu()
        for action in menu.actions():
            if action.isSeparator():
                continue
            has_icon = not action.icon().isNull()
            has_glyph = any(ord(ch) > 0x2000 for ch in action.text())
            self.assertNotEqual(
                has_icon, has_glyph,
                f"'{action.text()}' mixes icon and glyph styling",
            )

    def test_system_tray_toggle_show_window(self):
        """_toggle_show_window toggles between visible and hidden."""
        self.win.show()
        self.assertTrue(self.win.isVisible())
        self.win._toggle_show_window()
        self.assertFalse(self.win.isVisible())
        self.win._toggle_show_window()
        self.assertTrue(self.win.isVisible())

    def test_minimize_to_tray(self):
        """When minimize_to_tray is enabled, minimizing window hides it."""
        from PySide6.QtGui import QWindowStateChangeEvent
        self.win._manager._general_config.enable_system_tray = True
        self.win._manager._general_config.minimize_to_tray = True
        self.win.show()
        self.win.setWindowState(Qt.WindowState.WindowMinimized)
        ev = QWindowStateChangeEvent(Qt.WindowState.WindowNoState)
        self.win.changeEvent(ev)
        QApplication.processEvents()
        self.assertTrue(self.win.isHidden())

    def test_close_to_tray_and_exit_app(self):
        """Closing window when close_to_tray is enabled hides window; _exit_app completely closes."""
        from PySide6.QtGui import QCloseEvent
        self.win._manager._general_config.enable_system_tray = True
        self.win._manager._general_config.close_to_tray = True
        self.win._force_exit = False
        self.win.show()

        # Regular closeEvent should be ignored and window hidden
        close_ev = QCloseEvent()
        self.win.closeEvent(close_ev)
        self.assertFalse(close_ev.isAccepted())
        self.assertTrue(self.win.isHidden())
        self.assertTrue(self.win._close_to_tray_notified)

        # _exit_app should set _force_exit and execute closeEvent even while hidden
        self.win._exit_app()
        self.assertTrue(self.win._force_exit)
        self.assertTrue(getattr(self.win, "_is_closing", False))

    def test_tray_resume_all_downloads(self):
        """_on_resume_all_downloads triggers manager.resume_all_downloads and updates status label."""
        with unittest.mock.patch.object(self.manager, "resume_all_downloads", return_value=3) as mock_resume:
            self.win._on_resume_all_downloads()
            mock_resume.assert_called_once()
            self.assertIn("Resumed 3 downloads", self.win._status_label.text())

    def test_tray_notification_click_restores_and_focuses(self):
        """Clicking on tray notification restores and focuses main window."""
        self.win.hide()
        self.assertTrue(self.win.isHidden())
        with unittest.mock.patch("my_idm.single_instance.activate_window") as mock_activate:
            self.win._on_tray_message_clicked()
            self.assertFalse(self.win.isHidden())
            mock_activate.assert_called_once_with(self.win)

    def test_notification_handler_delegation_to_tray(self):
        """my_idm.notifications.show_notification routes through registered MainWindow tray icon."""
        from my_idm.notifications import show_notification
        received = []
        self.win._sig_show_tray_notification.connect(lambda t, m, d: received.append((t, m, d)))
        res = show_notification("Test Title", "Test Message", duration=4)
        self.assertTrue(res)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0], ("Test Title", "Test Message", 4000))

    def test_status_completed_triggers_notification(self):
        """When download status becomes completed, notify_download_complete is called if enabled."""
        entry = DownloadEntry(id="done-1", url="http://example.com/done.mp4", filename="done.mp4", status="downloading")
        self.db.add_download(entry)
        self.win._manager._general_config.notify_on_completion = True
        with unittest.mock.patch("my_idm.notifications.notify_download_complete") as mock_notify:
            self.win._on_status_changed("done-1", "completed", "")
            mock_notify.assert_called_once_with("done.mp4")

            # Duplicate call should not re-notify
            mock_notify.reset_mock()
            self.win._on_status_changed("done-1", "completed", "")
            mock_notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()



