"""Unit tests for TorServiceManager, Tor discovery, process lifecycle, and alerting."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from my_idm.config import TorConfig
from my_idm.database import Database
from my_idm.manager import DownloadManager
from my_idm.tor_service import TorServiceManager, find_tor_executable

app = QApplication.instance() or QApplication(sys.argv)


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


class TestTorServiceManager(unittest.TestCase):
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
        self.assertFalse(self.service.is_spawned)

    @patch("my_idm.tor_service.is_tor_reachable", return_value=False)
    @patch("my_idm.tor_service.find_tor_executable", return_value=None)
    def test_start_executable_not_found(self, mock_find, mock_reachable):
        success, msg = self.service.start()
        self.assertFalse(success)
        self.assertIn("could not be found", msg)
        self.assertFalse(self.service.is_spawned)

    @patch("my_idm.tor_service.find_tor_executable", return_value="C:\\Tor\\tor.exe")
    @patch("subprocess.Popen")
    def test_start_process_exits_with_error(self, mock_popen, mock_find):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1
        mock_proc.communicate.return_value = ("", "Port 9050 already in use")
        mock_popen.return_value = mock_proc

        with patch("my_idm.tor_service.is_tor_reachable", return_value=False):
            success, msg = self.service.start(timeout=2.0)
            self.assertFalse(success)
            self.assertIn("terminated unexpectedly", msg)
            self.assertIn("Port 9050 already in use", msg)
            self.assertFalse(self.service.is_spawned)

    @patch("my_idm.tor_service.find_tor_executable", return_value="C:\\Tor\\tor.exe")
    @patch("subprocess.Popen")
    def test_start_success_connected(self, mock_popen, mock_find):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 9999
        mock_popen.return_value = mock_proc

        # First call False (not ready), second call True (ready)
        with patch("my_idm.tor_service.is_tor_reachable", side_effect=[False, True]):
            success, msg = self.service.start(timeout=5.0)
            self.assertTrue(success)
            self.assertIn("started and connected", msg)
            self.assertTrue(self.service.is_spawned)

        # Calling stop terminates the process
        self.service.stop()
        mock_proc.terminate.assert_called_once()
        self.assertFalse(self.service.is_spawned)


class TestDownloadManagerTorService(unittest.TestCase):
    """Test DownloadManager integration with TorServiceManager and error handling."""

    def setUp(self):
        QSettings("MyIDM", "My-IDM").clear()
        self.db = Database(":memory:")
        self.db.open()
        self.manager = DownloadManager(self.db)

    def tearDown(self):
        self.manager.stop()
        self.db.close()
        QSettings("MyIDM", "My-IDM").clear()

    def test_toggle_tor_start_success(self):
        with patch.object(self.manager.tor_service, "start", return_value=(True, "Connected")):
            success, msg = self.manager.toggle_tor(True)
            self.assertTrue(success)
            self.assertTrue(self.manager.tor_config.enabled)
            self.assertEqual(msg, "Connected")

    def test_toggle_tor_start_failure_reverts_state(self):
        with patch.object(self.manager.tor_service, "start", return_value=(False, "tor.exe not found")):
            success, msg = self.manager.toggle_tor(True)
            self.assertFalse(success)
            self.assertFalse(self.manager.tor_config.enabled)
            self.assertIn("not found", msg)

    def test_toggle_tor_stop_calls_service_stop(self):
        with patch.object(self.manager.tor_service, "stop") as mock_stop:
            success, msg = self.manager.toggle_tor(False)
            self.assertTrue(success)
            self.assertFalse(self.manager.tor_config.enabled)
            mock_stop.assert_called_once()


class TestMainWindowTorAlert(unittest.TestCase):
    """Test MainWindow Tor toolbar action and error alert on failed start."""

    def setUp(self):
        QSettings("MyIDM", "My-IDM").clear()
        self.db = Database(":memory:")
        self.db.open()
        self.manager = DownloadManager(self.db)
        from my_idm.main_window import MainWindow
        self.win = MainWindow(self.manager)

    def tearDown(self):
        self.win.close()
        self.manager.stop()
        self.db.close()
        QSettings("MyIDM", "My-IDM").clear()

    @patch("PySide6.QtWidgets.QMessageBox.critical")
    def test_toggle_tor_failure_shows_critical_alert_and_unchecks(self, mock_msg):
        with patch.object(self.manager, "toggle_tor", return_value=(False, "Executable failed to start")):
            # Simulate user clicking toolbar button
            self.win._on_toggle_tor(True)

            # Button should be unchecked
            self.assertFalse(self.win._act_tor.isChecked())
            # Critical alert dialog was presented
            mock_msg.assert_called_once()
            args, _ = mock_msg.call_args
            self.assertIn("Tor Connection Error", args[1])
            self.assertIn("Executable failed to start", args[2])


if __name__ == "__main__":
    unittest.main()
