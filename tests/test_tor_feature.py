"""Unit tests for Tor proxy activation, traffic routing, and settings."""

import os
import sys
import socket
import unittest
from unittest.mock import patch, MagicMock
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from my_idm.config import TorConfig, is_tor_reachable
from my_idm.database import Database, DownloadEntry
from my_idm.download_model import DownloadTableModel, Col
from my_idm.manager import DownloadManager
from my_idm.settings_dialog import SettingsDialog
from my_idm.styles import Colors
from my_idm.http_engine import HTTPEngine
from my_idm.torrent_engine import TorrentEngine

app = QApplication.instance() or QApplication(sys.argv)


class TestTorConfig(unittest.TestCase):
    """Test TorConfig serialization, deserialization, and URL generation."""

    def setUp(self):
        self.settings = QSettings("MyIDMTest", "TorConfigTest")
        self.settings.clear()

    def tearDown(self):
        self.settings.clear()

    def test_default_values(self):
        cfg = TorConfig()
        self.assertFalse(cfg.enabled)
        self.assertFalse(cfg.auto_start_at_startup)
        self.assertTrue(cfg.route_http)
        self.assertTrue(cfg.route_torrent)
        self.assertEqual(cfg.proxy_host, "127.0.0.1")
        self.assertEqual(cfg.proxy_port, 9050)
        self.assertEqual(cfg.socks5_url, "socks5://127.0.0.1:9050")

    def test_save_and_load(self):
        cfg = TorConfig(
            enabled=True,
            auto_start_at_startup=True,
            route_http=False,
            route_torrent=True,
            proxy_host="192.168.1.50",
            proxy_port=9150,
            tor_executable_path="C:\\Tor\\tor.exe",
        )
        cfg.save(self.settings)

        loaded = TorConfig.load(self.settings)
        self.assertTrue(loaded.enabled)
        self.assertTrue(loaded.auto_start_at_startup)
        self.assertFalse(loaded.route_http)
        self.assertTrue(loaded.route_torrent)
        self.assertEqual(loaded.proxy_host, "192.168.1.50")
        self.assertEqual(loaded.proxy_port, 9150)
        self.assertEqual(loaded.tor_executable_path, "C:\\Tor\\tor.exe")
        self.assertEqual(loaded.socks5_url, "socks5://192.168.1.50:9150")

    def test_dict_conversion(self):
        cfg = TorConfig(
            enabled=True,
            auto_start_at_startup=False,
            route_http=True,
            route_torrent=False,
            proxy_host="localhost",
            proxy_port=9050,
        )
        d = cfg.to_dict()
        self.assertTrue(d["enabled"])
        self.assertFalse(d["auto_start_at_startup"])
        self.assertTrue(d["route_http"])
        self.assertFalse(d["route_torrent"])

        rebuilt = TorConfig.from_dict(d)
        self.assertEqual(rebuilt.socks5_url, "socks5://localhost:9050")


class TestTorReachability(unittest.TestCase):
    """Test is_tor_reachable network check."""

    @patch("socket.create_connection")
    def test_reachable_success(self, mock_conn):
        mock_sock = MagicMock()
        mock_conn.return_value = mock_sock
        result = is_tor_reachable("127.0.0.1", 9050, timeout=1.0)
        self.assertTrue(result)
        mock_conn.assert_called_once_with(("127.0.0.1", 9050), timeout=1.0)
        mock_sock.close.assert_called_once()

    @patch("socket.create_connection", side_effect=OSError("Connection refused"))
    def test_reachable_failure(self, mock_conn):
        result = is_tor_reachable("127.0.0.1", 9050, timeout=1.0)
        self.assertFalse(result)


class TestEnginesTorRouting(unittest.TestCase):
    """Test HTTPEngine and TorrentEngine configuration when Tor is active."""

    def test_http_engine_tor_config(self):
        db = Database(":memory:")
        engine = HTTPEngine(db)
        tor_cfg = TorConfig(enabled=True, route_http=True, proxy_port=9050)
        engine.set_tor_config_sync(tor_cfg)
        self.assertEqual(engine.tor_config.proxy_port, 9050)
        self.assertTrue(engine.tor_config.enabled)

    def test_torrent_engine_apply_tor(self):
        db = Database(":memory:")
        engine = TorrentEngine(db)
        tor_cfg = TorConfig(enabled=True, route_torrent=True, proxy_host="127.0.0.1", proxy_port=9050)
        # Applying Tor config should configure libtorrent settings without errors
        engine.apply_tor_config(tor_cfg)
        self.assertEqual(engine.tor_config.socks5_url, "socks5://127.0.0.1:9050")

        # Deactivating Tor should restore default settings
        tor_cfg_off = TorConfig(enabled=False)
        engine.apply_tor_config(tor_cfg_off)
        self.assertFalse(engine.tor_config.enabled)


class TestDownloadManagerTor(unittest.TestCase):
    """Test DownloadManager Tor controls and signals."""

    def setUp(self):
        QSettings("MyIDM", "My-IDM").clear()
        self.db = Database(":memory:")
        self.manager = DownloadManager(self.db)

    def tearDown(self):
        self.manager.stop()
        QSettings("MyIDM", "My-IDM").clear()

    def test_manager_toggle_tor(self):
        signal_received = []
        self.manager.tor_config_changed.connect(lambda cfg: signal_received.append(cfg))

        with patch.object(self.manager.tor_service, "start", return_value=(True, "Connected")):
            success, msg = self.manager.toggle_tor(True)
            self.assertTrue(success)
            self.assertTrue(self.manager.tor_config.enabled)
            self.assertEqual(len(signal_received), 1)
            self.assertTrue(signal_received[0].enabled)

        with patch.object(self.manager.tor_service, "stop"):
            success, msg = self.manager.toggle_tor(False)
            self.assertTrue(success)
            self.assertFalse(self.manager.tor_config.enabled)
            self.assertEqual(len(signal_received), 2)
            self.assertFalse(signal_received[1].enabled)

    def test_toggle_tor_unreachable_warning(self):
        with patch.object(self.manager.tor_service, "start", return_value=(False, "tor proxy not reachable")):
            success, msg = self.manager.toggle_tor(True)
            self.assertFalse(success)
            self.assertIn("not reachable", msg)
            self.assertFalse(self.manager.tor_config.enabled)


class TestSettingsDialogTorTab(unittest.TestCase):
    """Test SettingsDialog Tor tab interactions and persistence."""

    def setUp(self):
        QSettings("MyIDM", "My-IDM").clear()

    def tearDown(self):
        QSettings("MyIDM", "My-IDM").clear()

    def test_tor_settings_tab_population_and_save(self):
        tor_cfg = TorConfig(
            enabled=True,
            auto_start_at_startup=True,
            route_http=True,
            route_torrent=False,
            proxy_host="127.0.0.1",
            proxy_port=9150,
        )

        dlg = SettingsDialog(tor_config=tor_cfg, initial_tab=2)
        # Verify initial values loaded into UI widgets
        self.assertTrue(dlg._tor_enable_cb.isChecked())
        self.assertTrue(dlg._tor_autostart_cb.isChecked())
        self.assertTrue(dlg._tor_route_http_cb.isChecked())
        self.assertFalse(dlg._tor_route_torrent_cb.isChecked())
        self.assertEqual(dlg._tor_port_spin.value(), 9150)

        # Modify values
        dlg._tor_route_torrent_cb.setChecked(True)
        dlg._tor_port_spin.setValue(9050)

        # Save
        dlg._on_save()

        saved_tor_cfg = dlg.tor_config
        self.assertTrue(saved_tor_cfg.enabled)
        self.assertTrue(saved_tor_cfg.route_http)
        self.assertTrue(saved_tor_cfg.route_torrent)
        self.assertEqual(saved_tor_cfg.proxy_port, 9050)


class TestMainWindowTor(unittest.TestCase):
    """Test MainWindow Tor toolbar action, status bar badge, and menu."""

    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.manager = DownloadManager(self.db)
        from my_idm.main_window import MainWindow
        self.win = MainWindow(self.manager)

    def tearDown(self):
        self.win.close()
        self.manager.stop()
        self.db.close()

    def test_tor_action_and_badge_updates(self):
        self.assertFalse(self.win._act_tor.isChecked())
        self.assertIn("OFF", self.win._act_tor.text())
        self.assertIn("OFF", self.win._tor_status_btn.text())

        # Enable Tor on manager
        tor_cfg = TorConfig(enabled=True, route_http=True, route_torrent=True)
        self.manager.set_tor_config(tor_cfg)

        self.assertTrue(self.win._act_tor.isChecked())
        self.assertIn("ON", self.win._act_tor.text())
        self.assertIn("HTTP", self.win._tor_status_btn.text())
        self.assertIn("Torrent", self.win._tor_status_btn.text())

        # Disable Tor
        tor_cfg_off = TorConfig(enabled=False)
        self.manager.set_tor_config(tor_cfg_off)

        self.assertFalse(self.win._act_tor.isChecked())
        self.assertIn("OFF", self.win._act_tor.text())
        self.assertIn("OFF", self.win._tor_status_btn.text())


class TestTorDownloadListingIndicator(unittest.TestCase):
    """Test the onion indicator ('🧅') in download table view rows for active Tor transfers."""

    def setUp(self):
        self.model = DownloadTableModel()
        self.http_entry = DownloadEntry(
            id="test-http-1",
            url="https://example.com/file.zip",
            filename="file.zip",
            save_path="C:/Downloads",
            download_type="http",
            status="downloading",
            downloaded_size=1024 * 1024,
            total_size=10 * 1024 * 1024,
            speed=250000,
        )
        self.torrent_entry = DownloadEntry(
            id="test-torrent-1",
            url="magnet:?xt=urn:btih:abcdef123456",
            filename="ubuntu.iso",
            save_path="C:/Downloads",
            download_type="torrent",
            status="downloading",
            downloaded_size=500 * 1024 * 1024,
            total_size=1000 * 1024 * 1024,
            speed=500000,
        )
        self.paused_entry = DownloadEntry(
            id="test-http-paused",
            url="https://example.com/paused.mp4",
            filename="paused.mp4",
            save_path="C:/Downloads",
            download_type="http",
            status="paused",
            downloaded_size=500,
            total_size=1000,
        )
        self.completed_entry = DownloadEntry(
            id="test-torrent-completed",
            url="magnet:?xt=urn:btih:999999",
            filename="completed.iso",
            save_path="C:/Downloads",
            download_type="torrent",
            status="completed",
            downloaded_size=1000,
            total_size=1000,
        )
        self.model.load_entries([
            self.http_entry,
            self.torrent_entry,
            self.paused_entry,
            self.completed_entry,
        ])

    def test_tor_inactive_by_default(self):
        """By default Tor is OFF; no indicator should appear on any download."""
        self.assertFalse(self.model.is_tor_active_for(self.http_entry))
        self.assertFalse(self.model.is_tor_active_for(self.torrent_entry))
        self.assertFalse(self.model.is_tor_active_for(self.paused_entry))

        http_row = self.model._id_to_row[self.http_entry.id]
        name_val = self.model.data(self.model.index(http_row, Col.NAME))
        self.assertEqual(name_val, "file.zip")
        self.assertNotIn("🧅", name_val)

        type_val = self.model.data(self.model.index(http_row, Col.TYPE))
        self.assertEqual(type_val, "HTTP")
        self.assertNotIn("🧅", type_val)

        status_val = self.model.data(self.model.index(http_row, Col.STATUS))
        self.assertEqual(status_val, "Downloading")
        self.assertNotIn("🧅", status_val)

    def test_tor_active_for_http_and_torrent_when_enabled(self):
        """When Tor is enabled and routing HTTP & Torrent, active downloads show '🧅'."""
        cfg = TorConfig(enabled=True, route_http=True, route_torrent=True)
        self.model.set_tor_config(cfg)

        self.assertTrue(self.model.is_tor_active_for(self.http_entry))
        self.assertTrue(self.model.is_tor_active_for(self.torrent_entry))
        self.assertFalse(self.model.is_tor_active_for(self.paused_entry))
        self.assertFalse(self.model.is_tor_active_for(self.completed_entry))

        # Check HTTP row display
        http_row = self.model._id_to_row[self.http_entry.id]
        self.assertEqual(
            self.model.data(self.model.index(http_row, Col.NAME)),
            "🧅 file.zip",
        )
        self.assertEqual(
            self.model.data(self.model.index(http_row, Col.TYPE)),
            "🧅 HTTP",
        )
        self.assertEqual(
            self.model.data(self.model.index(http_row, Col.STATUS)),
            "Downloading (Tor 🧅)",
        )

        # Check Torrent row display
        tor_row = self.model._id_to_row[self.torrent_entry.id]
        self.assertEqual(
            self.model.data(self.model.index(tor_row, Col.NAME)),
            "🧅 ubuntu.iso",
        )
        self.assertEqual(
            self.model.data(self.model.index(tor_row, Col.TYPE)),
            "🧅 TORRENT",
        )
        self.assertEqual(
            self.model.data(self.model.index(tor_row, Col.STATUS)),
            "Downloading (Tor 🧅)",
        )

        # Check Paused row - should NOT have onion indicator
        paused_row = self.model._id_to_row[self.paused_entry.id]
        self.assertEqual(
            self.model.data(self.model.index(paused_row, Col.NAME)),
            "paused.mp4",
        )
        self.assertEqual(
            self.model.data(self.model.index(paused_row, Col.STATUS)),
            "Paused",
        )

    def test_tor_selective_routing(self):
        """When routing only HTTP, torrents do not show Tor icon."""
        cfg = TorConfig(enabled=True, route_http=True, route_torrent=False)
        self.model.set_tor_config(cfg)

        self.assertTrue(self.model.is_tor_active_for(self.http_entry))
        self.assertFalse(self.model.is_tor_active_for(self.torrent_entry))

        http_row = self.model._id_to_row[self.http_entry.id]
        tor_row = self.model._id_to_row[self.torrent_entry.id]
        self.assertIn("🧅", self.model.data(self.model.index(http_row, Col.NAME)))
        self.assertNotIn("🧅", self.model.data(self.model.index(tor_row, Col.NAME)))

    def test_switching_tor_off_refreshes_listing(self):
        """Switching Tor OFF immediately removes the indicator from active transfers."""
        # First turn ON
        cfg_on = TorConfig(enabled=True, route_http=True, route_torrent=True)
        self.model.set_tor_config(cfg_on)
        http_row = self.model._id_to_row[self.http_entry.id]
        self.assertIn("🧅", self.model.data(self.model.index(http_row, Col.NAME)))

        # Now turn OFF
        cfg_off = TorConfig(enabled=False, route_http=True, route_torrent=True)
        self.model.set_tor_config(cfg_off)
        self.assertFalse(self.model.is_tor_active_for(self.http_entry))
        self.assertEqual(
            self.model.data(self.model.index(http_row, Col.NAME)),
            "file.zip",
        )
        self.assertEqual(
            self.model.data(self.model.index(http_row, Col.STATUS)),
            "Downloading",
        )
        self.assertEqual(
            self.model.data(self.model.index(http_row, Col.TYPE)),
            "HTTP",
        )

    def test_pausing_download_removes_indicator(self):
        """When a download is paused during Tor session, the indicator is removed."""
        cfg_on = TorConfig(enabled=True, route_http=True, route_torrent=True)
        self.model.set_tor_config(cfg_on)

        http_row = self.model._id_to_row[self.http_entry.id]
        self.assertIn("🧅", self.model.data(self.model.index(http_row, Col.NAME)))

        # Simulate pause
        self.model.update_status(self.http_entry.id, "paused")
        self.assertEqual(
            self.model.data(self.model.index(http_row, Col.NAME)),
            "file.zip",
        )
        self.assertEqual(
            self.model.data(self.model.index(http_row, Col.STATUS)),
            "Paused",
        )

        # Resume again
        self.model.update_status(self.http_entry.id, "downloading")
        self.assertEqual(
            self.model.data(self.model.index(http_row, Col.NAME)),
            "🧅 file.zip",
        )

    def test_tooltips_contain_tor_information(self):
        """Active Tor downloads show Tor SOCKS5 routing information in tooltips."""
        cfg_on = TorConfig(enabled=True, route_http=True, proxy_host="127.0.0.1", proxy_port=9050)
        self.model.set_tor_config(cfg_on)

        from PySide6.QtCore import Qt
        http_row = self.model._id_to_row[self.http_entry.id]
        name_tip = self.model.data(self.model.index(http_row, Col.NAME), Qt.ItemDataRole.ToolTipRole)
        status_tip = self.model.data(self.model.index(http_row, Col.STATUS), Qt.ItemDataRole.ToolTipRole)
        type_tip = self.model.data(self.model.index(http_row, Col.TYPE), Qt.ItemDataRole.ToolTipRole)

        self.assertIn("🧅 Active Tor Route", name_tip)
        self.assertIn("9050", name_tip)
        self.assertIn("Active Tor Transfer", status_tip)
        self.assertIn("Tor", type_tip)

    def test_main_window_toggle_tor_refreshes_table(self):
        """MainWindow toggling Tor refreshes the table model and shows Tor indicator."""
        from my_idm.main_window import MainWindow
        db = Database(":memory:")
        db.open()
        manager = DownloadManager(db)
        win = MainWindow(manager)
        try:
            # Add entry to manager DB and win._model
            entry = db.add_download(
                DownloadEntry(
                    url="https://example.com/test_tor.zip",
                    save_path="C:/Downloads",
                    download_type="http",
                    status="downloading",
                    filename="test_tor.zip",
                )
            )
            win._model.load_entries([entry])

            # Initial: Tor OFF
            row = win._model._id_to_row[entry.id]
            self.assertEqual(win._model.data(win._model.index(row, Col.NAME)), "test_tor.zip")

            # Enable Tor config on manager
            tor_cfg = TorConfig(enabled=True, route_http=True, route_torrent=True)
            manager.set_tor_config(tor_cfg)

            # Check that model updated
            self.assertEqual(win._model.data(win._model.index(row, Col.NAME)), "🧅 test_tor.zip")
            self.assertEqual(win._model.data(win._model.index(row, Col.STATUS)), "Downloading (Tor 🧅)")

            # Disable Tor
            tor_cfg_off = TorConfig(enabled=False)
            manager.set_tor_config(tor_cfg_off)

            self.assertEqual(win._model.data(win._model.index(row, Col.NAME)), "test_tor.zip")
            self.assertEqual(win._model.data(win._model.index(row, Col.STATUS)), "Downloading")
        finally:
            win.close()
            manager.stop()
            db.close()

    def test_sorting_preserves_alphabetical_order_with_tor(self):
        """Sorting by name orders by actual filename rather than the Tor emoji prefix."""
        e1 = DownloadEntry(id="1", filename="apple.zip", status="downloading", download_type="http")
        e2 = DownloadEntry(id="2", filename="banana.zip", status="paused", download_type="http")
        e3 = DownloadEntry(id="3", filename="cherry.zip", status="downloading", download_type="http")

        model = DownloadTableModel()
        model.load_entries([e3, e1, e2])
        model.set_tor_config(TorConfig(enabled=True, route_http=True))

        from PySide6.QtCore import Qt
        model.sort(Col.NAME, Qt.SortOrder.AscendingOrder)
        names = [model.data(model.index(r, Col.NAME)) for r in range(model.rowCount())]
        self.assertEqual(names, ["🧅 apple.zip", "banana.zip", "🧅 cherry.zip"])


if __name__ == "__main__":
    unittest.main()

