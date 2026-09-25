"""Unit tests for dialogs in My-IDM."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tempfile
import unittest
import unittest.mock
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QGuiApplication

from my_idm.config import GeneralConfig
from my_idm.dialogs import AddDownloadDialog, DeleteConfirmDialog, RenameDialog

app = QApplication.instance() or QApplication([])


class TestAddDownloadDialogClipboard(unittest.TestCase):

    def setUp(self):
        clipboard = QGuiApplication.clipboard()
        self._orig_clipboard = clipboard.text() if clipboard else ""

    def tearDown(self):
        clipboard = QGuiApplication.clipboard()
        if clipboard:
            clipboard.setText(self._orig_clipboard)

    def test_prefill_http_url_from_clipboard(self):
        """HTTP URL in clipboard is automatically prefilled."""
        clipboard = QGuiApplication.clipboard()
        clipboard.setText("https://downloads.example.com/test-file.tar.gz")

        dlg = AddDownloadDialog()
        try:
            self.assertEqual(
                dlg._url_edit.text(),
                "https://downloads.example.com/test-file.tar.gz"
            )
            # Text should be selected for quick replace
            self.assertTrue(dlg._url_edit.hasSelectedText())
        finally:
            dlg.close()

    def test_prefill_magnet_link_from_clipboard(self):
        """Magnet link in clipboard is automatically prefilled."""
        clipboard = QGuiApplication.clipboard()
        magnet = "magnet:?xt=urn:btih:da39a3ee5e6b4b0d3255bfef95601890afd80709&dn=sample_download_dialog_test"
        clipboard.setText(magnet)

        dlg = AddDownloadDialog()
        try:
            self.assertEqual(dlg._url_edit.text(), magnet)
            self.assertTrue(dlg._url_edit.hasSelectedText())
        finally:
            dlg.close()

    def test_do_not_prefill_non_url_text(self):
        """Non-URL text in clipboard is ignored."""
        clipboard = QGuiApplication.clipboard()
        clipboard.setText("Just some random copied text from another app")

        dlg = AddDownloadDialog()
        try:
            self.assertEqual(dlg._url_edit.text(), "")
        finally:
            dlg.close()

    def test_do_not_prefill_multiline_text(self):
        """Multi-line text starting with http is not mistakenly prefilled."""
        clipboard = QGuiApplication.clipboard()
        clipboard.setText("https://example.com\nsome notes\nmore notes")

        dlg = AddDownloadDialog()
        try:
            self.assertEqual(dlg._url_edit.text(), "")
        finally:
            dlg.close()

    def test_explicit_initial_url_overrides_clipboard(self):
        """Passing initial_url overrides clipboard content."""
        clipboard = QGuiApplication.clipboard()
        clipboard.setText("https://clipboard.example.com/clip.zip")

        dlg = AddDownloadDialog(initial_url="https://override.example.com/explicit.zip")
        try:
            self.assertEqual(
                dlg._url_edit.text(),
                "https://override.example.com/explicit.zip"
            )
        finally:
            dlg.close()

    def test_empty_clipboard(self):
        """Empty clipboard leaves the input empty."""
        clipboard = QGuiApplication.clipboard()
        clipboard.clear()

        dlg = AddDownloadDialog()
        try:
            self.assertEqual(dlg._url_edit.text(), "")
        finally:
            dlg.close()

    def test_add_download_picks_default_folder(self):
        """Add download dialog picks default_save_path even when remember_last is default."""
        with tempfile.TemporaryDirectory() as custom_dir:
            cfg = GeneralConfig(default_save_path=custom_dir)
            cfg.save()

            dlg = AddDownloadDialog()
            try:
                self.assertEqual(dlg.save_path, custom_dir)
                self.assertEqual(dlg._save_edit.currentText(), custom_dir)
            finally:
                dlg.close()


class TestAddDownloadDialogTorButton(unittest.TestCase):

    def test_tor_button_initial_state_and_toggle(self):
        """AddDownloadDialog contains Tor button reflecting state and toggling successfully."""
        dlg = AddDownloadDialog()
        try:
            self.assertIsNotNone(dlg._tor_btn)
            self.assertEqual(dlg._tor_btn.text(), "🧅 Tor: OFF")
            self.assertIn("#21262d", dlg._tor_btn.styleSheet())
            self.assertFalse(dlg.is_tor_enabled())

            # Toggle Tor ON
            dlg._on_toggle_tor()
            self.assertEqual(dlg._tor_btn.text(), "🧅 Tor: ON")
            self.assertIn("#50fa7b", dlg._tor_btn.styleSheet())
            self.assertTrue(dlg.is_tor_enabled())

            # Toggle Tor OFF
            dlg._on_toggle_tor()
            self.assertEqual(dlg._tor_btn.text(), "🧅 Tor: OFF")
            self.assertFalse(dlg.is_tor_enabled())
        finally:
            dlg.close()


class TestDialogsScrollable(unittest.TestCase):

    def test_settings_dialog_tabs_are_scrollable(self):
        """All tabs in SettingsDialog must be wrapped in QScrollArea with widgetResizable."""
        from PySide6.QtWidgets import QScrollArea
        from my_idm.settings_dialog import SettingsDialog

        dlg = SettingsDialog()
        try:
            self.assertEqual(dlg._tabs.count(), 7)
            for i in range(dlg._tabs.count()):
                widget = dlg._tabs.widget(i)
                self.assertIsInstance(widget, QScrollArea)
                self.assertTrue(widget.widgetResizable())
        finally:
            dlg.close()

    def test_security_dialog_tabs_are_scrollable(self):
        """All tabs in SecuritySettingsDialog must be wrapped in QScrollArea with widgetResizable."""
        from PySide6.QtWidgets import QScrollArea
        from my_idm.security import SecurityConfig
        from my_idm.security_dialog import SecuritySettingsDialog

        dlg = SecuritySettingsDialog(SecurityConfig.load())
        try:
            from PySide6.QtWidgets import QTabWidget
            tab_widget = dlg.findChild(QTabWidget)
            self.assertIsNotNone(tab_widget)
            self.assertEqual(tab_widget.count(), 2)
            for i in range(tab_widget.count()):
                widget = tab_widget.widget(i)
                self.assertIsInstance(widget, QScrollArea)
                self.assertTrue(widget.widgetResizable())
        finally:
            dlg.close()


class TestAddDownloadDialogMultiline(unittest.TestCase):
    """Test multiline URL support in AddDownloadDialog."""

    def test_multiline_urls_property(self):
        """AddDownloadDialog.urls parses multiple lines and filters blanks."""
        dlg = AddDownloadDialog()
        try:
            dlg._url_edit.setPlainText(
                "https://example.com/file1.zip\n\n"
                "https://example.com/file2.zip\n"
                "magnet:?xt=urn:btih:da39a3ee5e6b4b0d3255bfef95601890afd80709\n"
            )
            dlg._accept()
            self.assertEqual(len(dlg.urls), 3)
            self.assertEqual(dlg.urls[0], "https://example.com/file1.zip")
            self.assertEqual(dlg.urls[1], "https://example.com/file2.zip")
            self.assertEqual(dlg.urls[2], "magnet:?xt=urn:btih:da39a3ee5e6b4b0d3255bfef95601890afd80709")
            self.assertEqual(dlg.url, "https://example.com/file1.zip")
        finally:
            dlg.close()

    def test_multiline_prefill_from_clipboard(self):
        """Clipboard with multiple valid URLs prefills all lines."""
        multiline_urls = (
            "https://mirror1.example.com/iso.img\n"
            "https://mirror2.example.com/iso.img\n"
            "https://mirror3.example.com/iso.img"
        )
        mock_cb = unittest.mock.MagicMock()
        mock_cb.text.return_value = multiline_urls
        with unittest.mock.patch("my_idm.dialogs.QGuiApplication.clipboard", return_value=mock_cb):
            dlg = AddDownloadDialog()
            try:
                self.assertEqual(dlg._url_edit.toPlainText().strip(), multiline_urls)
                self.assertEqual(len(dlg.urls or [dlg.url]), 3)
            finally:
                dlg.close()


class TestRenameDialog(unittest.TestCase):
    """Test RenameDialog length, styling, and behavior."""

    def test_rename_dialog_dimensions_and_initial_name(self):
        """RenameDialog is long (minimum width >= 550) and pre-fills current name."""
        dlg = RenameDialog("debian-12.0.0-amd64-netinst.iso")
        try:
            self.assertGreaterEqual(dlg.minimumWidth(), 550)
            self.assertEqual(dlg._name_edit.text(), "debian-12.0.0-amd64-netinst.iso")
            self.assertEqual(dlg._name_edit.selectedText(), "debian-12.0.0-amd64-netinst")
            self.assertEqual(dlg.new_name, "debian-12.0.0-amd64-netinst.iso")
        finally:
            dlg.close()

    def test_rename_dialog_accept_and_modify(self):
        """Modifying text in name edit updates new_name upon accept."""
        dlg = RenameDialog("ubuntu.iso")
        try:
            dlg._name_edit.setText("ubuntu-24.04.iso")
            dlg._accept()
            self.assertEqual(dlg.new_name, "ubuntu-24.04.iso")
        finally:
            dlg.close()


class TestDeleteConfirmDialog(unittest.TestCase):
    """Tests for DeleteConfirmDialog text and delete_files flag."""

    def test_delete_confirm_dialog_defaults_and_trash_label(self):
        dlg = DeleteConfirmDialog(count=1)
        try:
            self.assertIn("Trash", dlg._files_cb.text())
            self.assertTrue(dlg.delete_files)
            dlg._accept()
            self.assertTrue(dlg.delete_files)
        finally:
            dlg.close()

    def test_delete_confirm_dialog_with_files_checked(self):
        dlg = DeleteConfirmDialog(count=3)
        try:
            dlg._files_cb.setChecked(True)
            dlg._accept()
            self.assertTrue(dlg.delete_files)
        finally:
            dlg.close()


if __name__ == "__main__":
    unittest.main()

