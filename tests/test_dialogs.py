"""Unit tests for dialogs in My-IDM."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tempfile
import unittest
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QGuiApplication

from my_idm.config import GeneralConfig
from my_idm.dialogs import AddDownloadDialog

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
                self.assertEqual(dlg._save_edit.text(), custom_dir)
            finally:
                dlg.close()



if __name__ == "__main__":
    unittest.main()
