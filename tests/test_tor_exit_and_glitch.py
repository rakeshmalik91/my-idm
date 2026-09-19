"""Unit tests for Tor exit termination, startup auto-start gating, and download progress bar glitch fix."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

# Ensure QApplication exists for QSettings and Qt model tests
_app = QApplication.instance() or QApplication([])

from my_idm.config import TorConfig
from my_idm.database import Database, DownloadEntry
from my_idm.download_model import DownloadTableModel
from my_idm.http_engine import HTTPEngine
from my_idm.manager import DownloadManager
from my_idm.tor_service import TorServiceManager


class TestTorStartupGating(unittest.TestCase):
    """Test that Tor is not started or enabled on startup unless auto_start_at_startup is True."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".ini", delete=False)
        self.tmp.close()
        self.settings = QSettings(self.tmp.name, QSettings.Format.IniFormat)
        self.settings.clear()

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def test_tor_disabled_on_startup_when_autostart_false(self):
        # User had enabled Tor in prior session, but auto_start_at_startup is False
        self.settings.beginGroup("Tor")
        self.settings.setValue("enabled", True)
        self.settings.setValue("auto_start_at_startup", False)
        self.settings.endGroup()

        cfg = TorConfig.load(self.settings)
        self.assertFalse(cfg.enabled, "Tor should NOT be enabled on startup when auto_start is False")
        self.assertFalse(cfg.auto_start_at_startup)

    def test_tor_enabled_on_startup_when_autostart_true(self):
        self.settings.beginGroup("Tor")
        self.settings.setValue("enabled", True)
        self.settings.setValue("auto_start_at_startup", True)
        self.settings.endGroup()

        cfg = TorConfig.load(self.settings)
        self.assertTrue(cfg.enabled)
        self.assertTrue(cfg.auto_start_at_startup)


class TestTorExitTermination(unittest.TestCase):
    """Test that exiting with Tor on stops the service and resets state."""

    def setUp(self):
        self.db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_file.close()
        self.db = Database(self.db_file.name)
        self.db.open()
        self.manager = DownloadManager(self.db)

    def tearDown(self):
        self.manager.stop()
        self.db.close()
        try:
            os.unlink(self.db_file.name)
        except OSError:
            pass

    def test_manager_stop_stops_tor_service_and_resets_enabled_if_not_autostart(self):
        self.manager._tor_config.enabled = True
        self.manager._tor_config.auto_start_at_startup = False

        with patch.object(self.manager._tor_service, "stop") as mock_tor_stop:
            with patch.object(self.manager._tor_config, "save") as mock_save:
                self.manager.stop()
                mock_tor_stop.assert_called_once()
                self.assertFalse(self.manager._tor_config.enabled)
                mock_save.assert_called_once()

    def test_tor_service_stop_cleans_up_pid_file(self):
        temp_dir = Path(tempfile.mkdtemp())
        pid_file = temp_dir / "tor.pid"
        pid_file.write_text("12345", encoding="utf-8")

        cfg = TorConfig(enabled=True)
        service = TorServiceManager(cfg, data_dir=temp_dir)

        with patch("subprocess.run") as mock_run, patch("os.kill") as mock_kill:
            service.stop()
            self.assertFalse(pid_file.exists(), "tor.pid should be removed on stop")


class TestProgressGlitchFixes(unittest.TestCase):
    """Test progress bar throttling and anti-jitter behavior."""

    def setUp(self):
        self.db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_file.close()
        self.db = Database(self.db_file.name)
        self.db.open()
        self.http_engine = HTTPEngine(self.db)
        self.model = DownloadTableModel()

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.db_file.name)
        except OSError:
            pass

    def test_http_progress_throttling(self):
        emitted: list[tuple] = []
        self.http_engine.set_callbacks(
            progress_cb=lambda did, dl, tot, sp, eta: emitted.append((did, dl, tot)),
            status_cb=lambda did, st, err: None,
        )

        # Emit 50 rapid calls for the same download within milliseconds
        for i in range(50):
            self.http_engine._emit_progress("dl-test", i * 1000, 1_000_000, 500.0, 10.0)

        # Due to 100ms throttle, should only emit 1 time
        self.assertEqual(len(emitted), 1)

        # But completing the download (downloaded == total) must always emit immediately
        self.http_engine._emit_progress("dl-test", 1_000_000, 1_000_000, 0.0, 0.0)
        self.assertEqual(len(emitted), 2)
        self.assertEqual(emitted[-1][1], 1_000_000)

    def test_anti_jitter_guard_prevents_minor_backwards_progress_during_download(self):
        entry = DownloadEntry(
            id="dl-jitter",
            url="https://example.com/file.iso",
            filename="file.iso",
            total_size=100_000_000,
            downloaded_size=50_000_000,
            status="downloading",
        )
        self.model.load_entries([entry])

        # An out-of-order signal reporting slightly fewer bytes (e.g. 49.9MB)
        self.model.update_progress("dl-jitter", 49_900_000, 100_000_000, 1000.0, 10.0)
        # Verify it did not jump backward
        self.assertEqual(self.model.entries[0].downloaded_size, 50_000_000)

        # Normal forward progress moves ahead
        self.model.update_progress("dl-jitter", 52_000_000, 100_000_000, 1000.0, 10.0)
        self.assertEqual(self.model.entries[0].downloaded_size, 52_000_000)


if __name__ == "__main__":
    unittest.main()
