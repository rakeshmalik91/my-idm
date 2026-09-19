"""Consolidated unit tests for Tor: service discovery, lifecycle, routing, UI indicators, progress bar, and seamless pause/resume."""

import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from unittest.mock import MagicMock, patch
from PySide6.QtCore import Qt, QSettings
from PySide6.QtWidgets import QApplication

from my_idm.config import TorConfig
from my_idm.database import Database, DownloadEntry
from my_idm.download_model import DownloadTableModel, Col
from my_idm.manager import DownloadManager
from my_idm.main_window import MainWindow
from my_idm.tor_service import TorServiceManager, find_tor_executable

app = QApplication.instance() or QApplication([])


class TestTorDiscovery(unittest.TestCase):
    """Test find_tor_executable resolution."""

    def test_custom_valid_path(self):
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as f:
            temp_exe = f.name
        try:
            found = find_tor_executable(temp_exe)
            self.assertEqual(found, str(Path(temp_exe).resolve()))
        finally:
            if os.path.exists(temp_exe):
                os.unlink(temp_exe)

    def test_nonexistent_custom_path_fallback(self):
        with patch("shutil.which", return_value=None):
            with patch("pathlib.Path.is_file", return_value=False):
                found = find_tor_executable("C:\\Nonexistent\\tor.exe")
                self.assertIsNone(found)

    @patch("shutil.which")
    @patch("os.path.isfile", return_value=True)
    def test_found_in_path(self, mock_isfile, mock_which):
        mock_which.return_value = "C:\\Tools\\tor.exe"
        found = find_tor_executable("")
        self.assertEqual(found, str(Path("C:\\Tools\\tor.exe").resolve()))


class TestTorServiceLifecycle(unittest.TestCase):
    """Test TorServiceManager process lifecycle and error handling."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp_dir.name) / "tor_data"
        self.cfg = TorConfig(proxy_host="127.0.0.1", proxy_port=9050)
        self.service = TorServiceManager(self.cfg, data_dir=self.data_dir)

    def tearDown(self):
        self.service.stop()
        self.tmp_dir.cleanup()

    @patch("my_idm.tor_service.is_tor_reachable", return_value=True)
    def test_start_already_running(self, mock_reachable):
        success, msg = self.service.start()
        self.assertTrue(success)
        self.assertIn("existing Tor service", msg)

    @patch("my_idm.tor_service.is_tor_reachable", return_value=False)
    @patch("my_idm.tor_service.find_tor_executable", return_value=None)
    def test_start_binary_not_found(self, mock_find, mock_reachable):
        success, msg = self.service.start()
        self.assertFalse(success)
        self.assertIn("could not be found", msg)

    @patch("my_idm.tor_service.find_tor_executable", return_value="C:\\Tools\\tor.exe")
    @patch("my_idm.tor_service.is_tor_reachable", side_effect=[False, True])
    @patch("subprocess.Popen")
    def test_start_success(self, mock_popen, mock_reachable, mock_find):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 1234
        mock_popen.return_value = proc

        success, msg = self.service.start()
        self.assertTrue(success)
        self.assertIn("started and connected", msg)
        self.assertTrue(self.service.is_spawned)


class TestTorStartupAndExitGating(unittest.TestCase):
    """Test that Tor is only started on startup if configured, and cleanly terminated on exit."""

    def test_tor_disabled_on_startup_when_autostart_false(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".ini", delete=False)
        tmp.close()
        try:
            settings = QSettings(tmp.name, QSettings.Format.IniFormat)
            settings.beginGroup("Tor")
            settings.setValue("enabled", True)
            settings.setValue("auto_start_at_startup", False)
            settings.endGroup()

            cfg = TorConfig.load(settings)
            self.assertFalse(cfg.enabled, "Tor should NOT be enabled on startup when auto_start is False")
            self.assertFalse(cfg.auto_start_at_startup)
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def test_tor_enabled_on_startup_when_autostart_true(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".ini", delete=False)
        tmp.close()
        try:
            settings = QSettings(tmp.name, QSettings.Format.IniFormat)
            settings.beginGroup("Tor")
            settings.setValue("enabled", True)
            settings.setValue("auto_start_at_startup", True)
            settings.endGroup()

            cfg = TorConfig.load(settings)
            self.assertTrue(cfg.enabled)
            self.assertTrue(cfg.auto_start_at_startup)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


class TestTorUIAndIndicator(unittest.TestCase):
    """Test the visual onion indicator on table view and instant refresh."""

    def setUp(self):
        self.model = DownloadTableModel()
        self.entry_http = DownloadEntry(
            id="e_http",
            filename="file.zip",
            url="https://example.com/file.zip",
            download_type="http",
            status="downloading",
        )
        self.entry_torrent = DownloadEntry(
            id="e_torrent",
            filename="movie.torrent",
            url="magnet:?xt=urn:btih:abc",
            download_type="torrent",
            status="downloading",
        )
        self.entry_paused = DownloadEntry(
            id="e_paused",
            filename="paused.zip",
            url="https://example.com/paused.zip",
            download_type="http",
            status="paused",
        )
        self.model.load_entries([self.entry_http, self.entry_torrent, self.entry_paused])

    def test_tor_inactive_by_default(self):
        """No indicator when Tor is disabled."""
        self.model.set_tor_config(TorConfig(enabled=False))
        self.assertFalse(self.model.is_tor_active_for(self.entry_http))
        row = self.model._id_to_row["e_http"]
        name_val = self.model.data(self.model.index(row, Col.NAME), Qt.ItemDataRole.DisplayRole)
        self.assertNotIn("🧅", name_val)

    def test_tor_active_for_http_and_torrent_when_enabled(self):
        """Active HTTP and torrent downloads display onion indicator when routed."""
        cfg = TorConfig(enabled=True, route_http=True, route_torrent=True)
        self.model.set_tor_config(cfg)

        self.assertTrue(self.model.is_tor_active_for(self.entry_http))
        self.assertTrue(self.model.is_tor_active_for(self.entry_torrent))
        self.assertFalse(self.model.is_tor_active_for(self.entry_paused))

        row_http = self.model._id_to_row["e_http"]
        name_http = self.model.data(self.model.index(row_http, Col.NAME), Qt.ItemDataRole.DisplayRole)
        type_http = self.model.data(self.model.index(row_http, Col.TYPE), Qt.ItemDataRole.DisplayRole)
        status_http = self.model.data(self.model.index(row_http, Col.STATUS), Qt.ItemDataRole.DisplayRole)

        self.assertIn("🧅", name_http)
        self.assertIn("🧅", type_http)
        self.assertIn("Tor 🧅", status_http)

    def test_tor_selective_routing(self):
        """When routing only HTTP, torrents do not show indicator."""
        cfg = TorConfig(enabled=True, route_http=True, route_torrent=False)
        self.model.set_tor_config(cfg)
        self.assertTrue(self.model.is_tor_active_for(self.entry_http))
        self.assertFalse(self.model.is_tor_active_for(self.entry_torrent))

    def test_switching_tor_off_refreshes_listing(self):
        """Switching Tor OFF immediately removes indicators."""
        self.model.set_tor_config(TorConfig(enabled=True, route_http=True))
        row = self.model._id_to_row["e_http"]
        self.assertIn("🧅", self.model.data(self.model.index(row, Col.NAME), Qt.ItemDataRole.DisplayRole))

        self.model.set_tor_config(TorConfig(enabled=False))
        self.assertNotIn("🧅", self.model.data(self.model.index(row, Col.NAME), Qt.ItemDataRole.DisplayRole))


class TestTorToggleAndProgress(unittest.TestCase):
    """Test progress bar on buttons during toggle, green styling, and seamless pause/resume."""

    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.manager = DownloadManager(self.db)
        self.win = MainWindow(self.manager)
        self.win.show()

    def tearDown(self):
        self.win.close()
        self.manager.stop()
        self.db.close()

    def test_pause_and_resume_downloads_around_tor_toggle(self):
        """Active downloads are paused before Tor start/stop and resumed after."""
        e1 = DownloadEntry(id="d1", url="http://example.com/1.zip", filename="1.zip", save_path="/tmp", status="downloading")
        e2 = DownloadEntry(id="d2", url="magnet:?xt=urn:btih:abc", filename="2.torrent", save_path="/tmp", status="fetching_metadata")
        e3 = DownloadEntry(id="d3", url="http://example.com/3.zip", filename="3.zip", save_path="/tmp", status="paused")
        self.db.add_download(e1)
        self.db.add_download(e2)
        self.db.add_download(e3)

        paused_order = []
        resumed_order = []

        with patch.object(self.manager, "pause_download", side_effect=lambda did: paused_order.append(did)), \
             patch.object(self.manager, "resume_download", side_effect=lambda did: resumed_order.append(did)), \
             patch.object(self.manager._tor_service, "start", return_value=(True, "Connected")):

            success, msg = self.manager.toggle_tor(True)
            self.assertTrue(success)

            self.assertIn("d1", paused_order)
            self.assertIn("d2", paused_order)
            self.assertNotIn("d3", paused_order)

            self.assertIn("d1", resumed_order)
            self.assertIn("d2", resumed_order)
            self.assertNotIn("d3", resumed_order)

    def test_resume_downloads_when_tor_start_fails(self):
        """Active downloads are resumed under direct routing if Tor start fails."""
        e1 = DownloadEntry(id="d1", url="http://example.com/1.zip", filename="1.zip", save_path="/tmp", status="downloading")
        self.db.add_download(e1)

        paused_order = []
        resumed_order = []

        with patch.object(self.manager, "pause_download", side_effect=lambda did: paused_order.append(did)), \
             patch.object(self.manager, "resume_download", side_effect=lambda did: resumed_order.append(did)), \
             patch.object(self.manager._tor_service, "start", return_value=(False, "Failed to connect")):

            success, msg = self.manager.toggle_tor(True)
            self.assertFalse(success)
            self.assertIn("d1", paused_order)
            self.assertIn("d1", resumed_order)

    def test_tor_progressbar_and_green_style(self):
        """Toolbar and footer buttons show progress bar during toggle and turn green when ON."""
        self.assertFalse(self.win._tor_toolbar_progress.isVisible())
        self.assertFalse(self.win._tor_footer_progress.isVisible())

        # Connecting state
        self.win._on_tor_status_changed("connecting", "Starting Tor...")
        self.assertTrue(self.win._tor_toolbar_progress.isVisible())
        self.assertTrue(self.win._tor_footer_progress.isVisible())
        self.assertIn("Connecting", self.win._tor_status_btn.text())
        self.assertIn("Connecting", self.win._act_tor.text())

        # Connected state
        self.manager.tor_config.enabled = True
        self.win._on_tor_status_changed("connected", "Tor connected")
        self.assertFalse(self.win._tor_toolbar_progress.isVisible())
        self.assertFalse(self.win._tor_footer_progress.isVisible())
        self.assertIn("#50fa7b", self.win._tor_status_btn.styleSheet())
        self.assertIn("#50fa7b", self.win._tor_toolbar_btn.styleSheet())

        # Disconnecting state
        self.win._on_tor_status_changed("disconnecting", "Stopping Tor...")
        self.assertTrue(self.win._tor_toolbar_progress.isVisible())
        self.assertTrue(self.win._tor_footer_progress.isVisible())
        self.assertIn("Disconnecting", self.win._tor_status_btn.text())
        self.assertIn("Disconnecting", self.win._act_tor.text())

        # Disconnected state
        self.manager.tor_config.enabled = False
        self.win._on_tor_status_changed("disconnected", "Tor deactivated")
        self.assertFalse(self.win._tor_toolbar_progress.isVisible())
        self.assertFalse(self.win._tor_footer_progress.isVisible())
        self.assertNotIn("#50fa7b", self.win._tor_status_btn.styleSheet())
        self.assertNotIn("#50fa7b", self.win._tor_toolbar_btn.styleSheet())


if __name__ == "__main__":
    unittest.main()
