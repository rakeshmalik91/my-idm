"""Unit tests for dialogs in My-IDM."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tempfile
import unittest
import unittest.mock
from unittest.mock import MagicMock, patch
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog
from PySide6.QtGui import QGuiApplication

from my_idm.config import ExternalToolsConfig, GeneralConfig
from my_idm.dialogs import AddDownloadDialog, DeleteConfirmDialog, RefreshAddressDialog, RenameDialog
from my_idm.settings_dialog import SettingsDialog, TAB_EXTERNAL_TOOLS

app = QApplication.instance() or QApplication([])

# Every config group that production persists. The autouse guard below snapshots
# and restores all of them so a test can never leave a poisoned value behind --
# e.g. a `default_save_path` pointing into an already-deleted TemporaryDirectory.
_CONFIG_GROUPS = (
    "General",
    "Torrent",
    "Tor",
    "ExternalTools",
    "BrowserIntegration",
    "Network",
    "Security",
    "Scheduler",
)


class ConfigIsolationMixin:
    """Snapshot/restore the whole QSettings tree around every test.

    ``AddDownloadDialog`` reads ``GeneralConfig`` at construction time, so
    ``test_add_download_picks_default_folder`` used to persist a
    ``default_save_path`` naming a temp directory that the ``with`` block then
    deleted -- poisoning every later test in the process.
    """

    def setUp(self):
        super().setUp()
        settings = QSettings("MyIDM", "My-IDM")
        snapshot = {
            group: {key: settings.value(f"{group}/{key}", None) for key in settings.childKeys()}
            for group in _CONFIG_GROUPS
        }
        self.addCleanup(self._restore_settings, settings, snapshot)

    @staticmethod
    def _restore_settings(settings, snapshot):
        settings.clear()
        for group, values in snapshot.items():
            for key, value in values.items():
                settings.setValue(f"{group}/{key}", value)
        settings.sync()


class FakeClipboard:
    """In-memory stand-in for the system clipboard.

    ``QClipboard.setText`` is a silent no-op when another process holds the
    clipboard lock, so a real clipboard makes these assertions a coin flip. The
    conftest fixture already snapshots/clears/restores the real clipboard; this
    swaps the object entirely so no test depends on OS state.
    """

    def __init__(self):
        self._text = ""
        self._image = None
        self._mime = None
        self.set_calls = []

    # -- QClipboard API surface used by production code --------------------
    def text(self, mode=None):
        return self._text

    def setText(self, text, mode=None):
        self._text = text
        self.set_calls.append(text)

    def clear(self, mode=None):
        self._text = ""
        self._image = None
        self._mime = None

    def image(self, mode=None):
        return self._image

    def setImage(self, image, mode=None):
        self._image = image

    def mimeData(self, mode=None):
        return self._mime

    def setMimeData(self, data, mode=None):
        self._mime = data


class TestAddDownloadDialogClipboard(ConfigIsolationMixin, unittest.TestCase):
    """Prefill-from-clipboard behaviour, driven through a fake clipboard."""

    def setUp(self):
        super().setUp()
        self.clipboard = FakeClipboard()
        patcher = patch.object(
            QGuiApplication, "clipboard", staticmethod(lambda: self.clipboard)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(QApplication.processEvents)

    def test_prefill_http_url_from_clipboard(self):
        """HTTP URL in clipboard is automatically prefilled."""
        url = "https://downloads.example.com/test-file.tar.gz"
        self.clipboard.setText(url)

        dlg = AddDownloadDialog()
        try:
            self.assertEqual(dlg._url_edit.text(), url)
            # Text should be selected for quick replace
            self.assertTrue(dlg._url_edit.hasSelectedText())
        finally:
            dlg.close()

    def test_prefill_magnet_link_from_clipboard(self):
        """Magnet link in clipboard is automatically prefilled."""
        magnet = "magnet:?xt=urn:btih:da39a3ee5e6b4b0d3255bfef95601890afd80709&dn=sample_download_dialog_test"
        self.clipboard.setText(magnet)

        dlg = AddDownloadDialog()
        try:
            self.assertEqual(dlg._url_edit.text(), magnet)
            self.assertTrue(dlg._url_edit.hasSelectedText())
        finally:
            dlg.close()

    def test_do_not_prefill_non_url_text(self):
        """Non-URL text in clipboard is ignored."""
        self.clipboard.setText("Just some random copied text from another app")

        dlg = AddDownloadDialog()
        try:
            self.assertEqual(dlg._url_edit.text(), "")
        finally:
            dlg.close()

    def test_do_not_prefill_multiline_text(self):
        """Multi-line text starting with http is not mistakenly prefilled."""
        self.clipboard.setText("https://example.com\nsome notes\nmore notes")

        dlg = AddDownloadDialog()
        try:
            self.assertEqual(dlg._url_edit.text(), "")
        finally:
            dlg.close()

    def test_explicit_initial_url_overrides_clipboard(self):
        """Passing initial_url overrides clipboard content."""
        self.clipboard.setText("https://clipboard.example.com/clip.zip")

        dlg = AddDownloadDialog(initial_url="https://override.example.com/explicit.zip")
        try:
            self.assertEqual(
                dlg._url_edit.text(), "https://override.example.com/explicit.zip"
            )
        finally:
            dlg.close()

    def test_empty_clipboard(self):
        """Empty clipboard leaves the input empty."""
        self.clipboard.clear()

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
                # The dialog must not quietly "remember" the folder either.
                self.assertEqual(GeneralConfig.load().default_save_path, custom_dir)
            finally:
                dlg.close()

        # The autouse guard is what undoes the save; run it now and prove the
        # deleted temp path is not left behind for the rest of the session.
        self.doCleanups()
        self.assertNotEqual(
            GeneralConfig.load().default_save_path, custom_dir,
            "the snapshot/restore guard must not leave a deleted temp path behind",
        )



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


class TestAddDownloadDialogYouTubeBanner(unittest.TestCase):
    """Paste-detection banner and its hand-off to the YouTube dialog."""

    YT_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def _dialog(self):
        mgr = MagicMock()
        mgr.external_tools_config = ExternalToolsConfig(ytdlp_auto_detect_urls=True)
        mgr.general_config.get_effective_save_path.return_value = "."
        return AddDownloadDialog(manager=mgr)

    def test_banner_appears_for_youtube_url(self):
        dlg = self._dialog()
        dlg._url_edit.setPlainText(self.YT_URL)
        dlg._update_youtube_banner()
        self.assertFalse(dlg._yt_banner.isHidden())
        self.assertEqual(dlg.youtube_url, self.YT_URL)
        dlg.close()

    def test_banner_survives_cancelled_youtube_dialog(self):
        """Regression: dismissing the YouTube dialog hid the hand-off banner."""
        dlg = self._dialog()
        dlg._url_edit.setPlainText(self.YT_URL)
        dlg._update_youtube_banner()

        with patch("my_idm.youtube_dialog.YouTubeDialog") as mock_cls:
            mock_cls.return_value.exec.return_value = QDialog.DialogCode.Rejected
            dlg._on_open_youtube_dialog()

        self.assertFalse(dlg._yt_banner.isHidden(), "banner must remain after cancel")
        self.assertEqual(dlg.youtube_selection, {})
        dlg.close()

    def test_youtube_dialog_can_be_reopened_after_cancel(self):
        dlg = self._dialog()
        dlg._url_edit.setPlainText(self.YT_URL)
        dlg._update_youtube_banner()

        for _ in range(2):
            with patch("my_idm.youtube_dialog.YouTubeDialog") as mock_cls:
                mock_cls.return_value.exec.return_value = QDialog.DialogCode.Rejected
                dlg._on_open_youtube_dialog()
                self.assertEqual(mock_cls.call_count, 1)
                self.assertEqual(
                    mock_cls.call_args.kwargs.get("initial_url"), self.YT_URL
                )
        self.assertFalse(dlg._yt_banner.isHidden())
        dlg.close()

    def test_banner_still_tracks_input_after_cancel(self):
        dlg = self._dialog()
        dlg._url_edit.setPlainText(self.YT_URL)
        with patch("my_idm.youtube_dialog.YouTubeDialog") as mock_cls:
            mock_cls.return_value.exec.return_value = QDialog.DialogCode.Rejected
            dlg._on_open_youtube_dialog()

        dlg._url_edit.setPlainText("https://example.com/file.zip")
        self.assertTrue(dlg._yt_banner.isHidden())
        dlg._url_edit.setPlainText(self.YT_URL)
        self.assertFalse(dlg._yt_banner.isHidden())
        dlg.close()

    def test_accepting_hands_selection_back(self):
        dlg = self._dialog()
        dlg._url_edit.setPlainText(self.YT_URL)
        dlg._update_youtube_banner()

        sentinel = {"videos": ["a"], "format_selector": "best", "save_path": "C:/x"}
        with patch("my_idm.youtube_dialog.YouTubeDialog") as mock_cls:
            mock_cls.return_value.exec.return_value = QDialog.DialogCode.Accepted
            mock_cls.return_value.selection.return_value = sentinel
            dlg._on_open_youtube_dialog()

        self.assertEqual(dlg.youtube_selection, sentinel)
        dlg.close()


class TestAddDownloadDialogAnimePaheBanner(unittest.TestCase):
    """Paste-detection banner and its hand-off to the AnimePahe section of Preferences.

    Mirrors TestAddDownloadDialogYouTubeBanner: a pasted animepahe.* URL is
    detected and offered a one-click route into Preferences with the URL
    prefilled, and dismissing Preferences leaves this dialog open with the
    banner still showing.
    """

    AP_URL = "https://animepahe.si/anime/ef667bb4-3a9b-449e-1a22-26156a642e47"

    def _dialog(self):
        mgr = MagicMock()
        mgr.external_tools_config = ExternalToolsConfig(ytdlp_auto_detect_urls=True)
        mgr.general_config.get_effective_save_path.return_value = "."
        return AddDownloadDialog(manager=mgr)

    def test_banner_appears_for_animepahe_url(self):
        dlg = self._dialog()
        dlg._url_edit.setPlainText(self.AP_URL)
        dlg._update_animepahe_banner()
        self.assertFalse(dlg._ap_banner.isHidden())
        self.assertEqual(dlg._detected_animepahe_url(), self.AP_URL)
        self.assertFalse(dlg.animepahe_handoff)
        dlg.close()

    def test_banner_disappears_for_non_animepahe_url(self):
        dlg = self._dialog()
        dlg._url_edit.setPlainText("https://example.com/file.zip")
        dlg._update_animepahe_banner()
        self.assertTrue(dlg._ap_banner.isHidden())
        self.assertEqual(dlg._detected_animepahe_url(), "")
        dlg.close()

    def test_hand_off_prefills_url_and_starts_scraper(self):
        """Clicking the banner opens Preferences on the AnimePahe tab with the URL set."""
        dlg = self._dialog()
        dlg._url_edit.setPlainText(self.AP_URL)
        dlg._update_animepahe_banner()

        with patch("my_idm.settings_dialog.SettingsDialog") as mock_cls:
            mock_dlg = mock_cls.return_value
            mock_dlg.exec.return_value = SettingsDialog.DialogCode.Accepted
            mock_dlg.animepahe_download_started = True
            dlg._on_open_animepahe_dialog()

        # Preferences was opened on the External Tools tab with the URL prefilled.
        self.assertEqual(mock_cls.call_count, 1)
        self.assertEqual(mock_cls.call_args.kwargs.get("initial_tab"), TAB_EXTERNAL_TOOLS)
        self.assertEqual(mock_dlg._animepahe_url_edit.setText.call_args.args[0], self.AP_URL)
        # Scraper started → Add Download dialog closes via the hand-off flag.
        self.assertTrue(dlg.animepahe_handoff)
        dlg.close()

    def test_banner_survives_cancelled_preferences(self):
        """Regression: closing Preferences without downloading hides nothing."""
        dlg = self._dialog()
        dlg._url_edit.setPlainText(self.AP_URL)
        dlg._update_animepahe_banner()

        with patch("my_idm.settings_dialog.SettingsDialog") as mock_cls:
            mock_dlg = mock_cls.return_value
            mock_dlg.exec.return_value = SettingsDialog.DialogCode.Rejected
            mock_dlg.animepahe_download_started = False
            dlg._on_open_animepahe_dialog()

        self.assertFalse(dlg._ap_banner.isHidden(), "banner must remain after cancel")
        self.assertFalse(dlg.animepahe_handoff)
        dlg.close()

    def test_banner_still_tracks_input_after_cancel(self):
        dlg = self._dialog()
        dlg._url_edit.setPlainText(self.AP_URL)
        with patch("my_idm.settings_dialog.SettingsDialog") as mock_cls:
            mock_cls.return_value.exec.return_value = SettingsDialog.DialogCode.Rejected
            mock_cls.return_value.animepahe_download_started = False
            dlg._on_open_animepahe_dialog()

        dlg._url_edit.setPlainText("https://example.com/file.zip")
        self.assertTrue(dlg._ap_banner.isHidden())
        dlg._url_edit.setPlainText(self.AP_URL)
        self.assertFalse(dlg._ap_banner.isHidden())
        dlg.close()


class TestDialogsScrollable(unittest.TestCase):

    def test_settings_dialog_tabs_are_scrollable(self):
        """All tabs in SettingsDialog must be wrapped in QScrollArea with widgetResizable."""
        from PySide6.QtWidgets import QScrollArea
        from my_idm.settings_dialog import SettingsDialog

        dlg = SettingsDialog()
        try:
            # Named rather than counted: a new tab should be a deliberate line change, and
            # the scroll-area guarantee below must cover it automatically either way.
            self.assertEqual(
                [dlg._tabs.tabText(i) for i in range(dlg._tabs.count())],
                [
                    "📁 Downloads & Retries",
                    "🖥️ Application & Tray",
                    "📋 Clipboard Capture",
                    "👁️ Views & Columns",
                    "🧲 BitTorrent",
                    "🌐 Browser Integration",
                    "🛡️ VPN & Proxy",
                    "🧅 Tor",
                    "🛡️ Antivirus & Security",
                    "🌐 AnimePahe Scraper",
                    "▶️ YouTube (yt-dlp)",
                ],
            )
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


class TestAddDownloadDialogMultiline(ConfigIsolationMixin, unittest.TestCase):
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
            self.assertEqual(dlg._name_edit.selectedText(), "debian-12.0.0-amd64-netinst.iso")
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


class TestRefreshAddressDialog(unittest.TestCase):
    """Test RefreshAddressDialog initialization, validation, and properties."""

    def test_refresh_dialog_initialization(self):
        old_url = "https://cdn.example.com/expired-token-123/video.mp4"
        dlg = RefreshAddressDialog(current_url=old_url, filename="video.mp4")
        try:
            self.assertEqual(dlg._current_url_edit.text(), old_url)
            self.assertEqual(dlg.current_url, old_url)
            self.assertTrue(dlg.resume_immediately)
            self.assertGreaterEqual(dlg.minimumWidth(), 560)
        finally:
            dlg.close()

    def test_refresh_dialog_validation_empty_url(self):
        dlg = RefreshAddressDialog(current_url="https://example.com/file.zip")
        try:
            dlg._url_edit.setText("   ")
            with patch("my_idm.dialogs.QMessageBox.warning") as mock_warn:
                dlg._accept()
                mock_warn.assert_called_once()
            self.assertEqual(dlg.result(), 0)
        finally:
            dlg.close()

    def test_refresh_dialog_validation_invalid_scheme(self):
        dlg = RefreshAddressDialog(current_url="https://example.com/file.zip")
        try:
            dlg._url_edit.setText("ftp://example.com/file.zip")
            with patch("my_idm.dialogs.QMessageBox.warning") as mock_warn:
                dlg._accept()
                mock_warn.assert_called_once()
            self.assertEqual(dlg.result(), 0)
        finally:
            dlg.close()

    def test_refresh_dialog_accept_valid_url(self):
        old_url = "https://example.com/expired.zip"
        new_url = "https://example.com/fresh.zip"
        dlg = RefreshAddressDialog(current_url=old_url)
        try:
            dlg._url_edit.setText(new_url)
            dlg._resume_cb.setChecked(False)
            dlg._accept()
            self.assertEqual(dlg.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(dlg.new_url, new_url)
            self.assertFalse(dlg.resume_immediately)
        finally:
            dlg.close()


if __name__ == "__main__":
    unittest.main()


