"""Unit tests for VPN adapter binding, proxy configuration, and kill switch."""

import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from my_idm.database import Database, DownloadEntry
from my_idm.http_engine import HTTPEngine
from my_idm.manager import DownloadManager
from my_idm.network import (
    NetworkConfig,
    NetworkInterfaceInfo,
    get_available_interfaces,
    is_interface_active,
    is_vpn_adapter_name,
)
from my_idm.network_dialog import NetworkSettingsDialog
from my_idm.torrent_engine import TorrentEngine

app = QApplication.instance() or QApplication([])


class TestNetworkConfig(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.test_settings = QSettings(
            str(Path(self.tmp_dir.name) / "test.ini"), QSettings.Format.IniFormat
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_default_config(self):
        config = NetworkConfig()
        self.assertEqual(config.interface_name, "")
        self.assertEqual(config.interface_ip, "")
        self.assertFalse(config.kill_switch)
        self.assertFalse(config.proxy_enabled)
        self.assertFalse(config.is_interface_bound)
        self.assertEqual(config.proxy_url, "")

    def test_proxy_url_generation(self):
        config = NetworkConfig(
            proxy_enabled=True,
            proxy_type="socks5",
            proxy_host="127.0.0.1",
            proxy_port=1080,
            proxy_username="user",
            proxy_password="secret",
        )
        self.assertEqual(config.proxy_url, "socks5://user:secret@127.0.0.1:1080")

        # Without password
        config2 = NetworkConfig(
            proxy_enabled=True,
            proxy_type="http",
            proxy_host="proxy.com",
            proxy_port=8080,
            proxy_username="user",
        )
        self.assertEqual(config2.proxy_url, "http://user@proxy.com:8080")

        # Without auth
        config3 = NetworkConfig(
            proxy_enabled=True,
            proxy_type="http",
            proxy_host="proxy.com",
            proxy_port=3128,
        )
        self.assertEqual(config3.proxy_url, "http://proxy.com:3128")

    def test_save_and_load_persistence(self):
        config = NetworkConfig(
            interface_name="NordLynx",
            interface_ip="10.5.0.2",
            kill_switch=True,
            proxy_enabled=True,
            proxy_type="socks5",
            proxy_host="vpn.nord.com",
            proxy_port=1080,
            proxy_username="vpnuser",
            proxy_password="vpnpass",
        )
        config.save(self.test_settings)

        loaded = NetworkConfig.load(self.test_settings)
        self.assertEqual(loaded.interface_name, "NordLynx")
        self.assertEqual(loaded.interface_ip, "10.5.0.2")
        self.assertTrue(loaded.kill_switch)
        self.assertTrue(loaded.proxy_enabled)
        self.assertEqual(loaded.proxy_type, "socks5")
        self.assertEqual(loaded.proxy_host, "vpn.nord.com")
        self.assertEqual(loaded.proxy_port, 1080)
        self.assertEqual(loaded.proxy_username, "vpnuser")
        self.assertEqual(loaded.proxy_password, "vpnpass")

    def test_vpn_adapter_name_detection(self):
        self.assertTrue(is_vpn_adapter_name("NordLynx"))
        self.assertTrue(is_vpn_adapter_name("wg0"))
        self.assertTrue(is_vpn_adapter_name("WireGuard Tunnel"))
        self.assertTrue(is_vpn_adapter_name("OpenVPN TAP-Windows Adapter V9"))
        self.assertTrue(is_vpn_adapter_name("Tailscale"))
        self.assertTrue(is_vpn_adapter_name("Proton VPN TUN"))
        self.assertFalse(is_vpn_adapter_name("Wi-Fi"))
        self.assertFalse(is_vpn_adapter_name("Ethernet"))
        self.assertFalse(is_vpn_adapter_name("Local Area Connection"))

    def test_get_available_interfaces(self):
        interfaces = get_available_interfaces()
        self.assertIsInstance(interfaces, list)
        for iface in interfaces:
            self.assertIsInstance(iface, NetworkInterfaceInfo)
            self.assertTrue(len(iface.name) > 0)
            self.assertIsInstance(iface.is_up, bool)

    def test_is_interface_active(self):
        # Default route is always active
        self.assertTrue(is_interface_active("", ""))
        # Non-existent interface should return False
        self.assertFalse(is_interface_active("NonExistentVPN12345", "10.254.254.254"))


class TestHTTPEngineVPN(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.engine = HTTPEngine(self.db)

    def tearDown(self):
        self.db.close()

    def test_request_kwargs_with_proxy(self):
        config = NetworkConfig(
            proxy_enabled=True,
            proxy_type="http",
            proxy_host="127.0.0.1",
            proxy_port=8080,
        )
        self.engine.set_network_config_sync(config)
        kwargs = self.engine._request_kwargs({"Custom": "Header"})
        self.assertEqual(kwargs["proxy"], "http://127.0.0.1:8080")
        self.assertEqual(kwargs["headers"]["Custom"], "Header")

    def test_request_kwargs_without_proxy(self):
        config = NetworkConfig(proxy_enabled=False)
        self.engine.set_network_config_sync(config)
        kwargs = self.engine._request_kwargs({"Custom": "Header"})
        self.assertNotIn("proxy", kwargs)


class TestTorrentEngineVPN(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.engine = TorrentEngine(self.db)
        self.engine.start()

    def tearDown(self):
        self.engine.stop()
        self.db.close()

    def test_apply_network_config_to_libtorrent(self):
        if not self.engine.available:
            self.skipTest("libtorrent not available")

        config = NetworkConfig(
            interface_name="VPN Adapter",
            interface_ip="192.168.1.50",
            proxy_enabled=True,
            proxy_type="socks5",
            proxy_host="proxy.test.com",
            proxy_port=1080,
        )
        self.engine.apply_network_config(config)

        # Check session settings
        sett = self.engine._session.get_settings()
        self.assertIn("192.168.1.50", sett["listen_interfaces"])
        self.assertEqual(sett["outgoing_interfaces"], "192.168.1.50")
        self.assertEqual(sett["proxy_hostname"], "proxy.test.com")
        self.assertEqual(sett["proxy_port"], 1080)
        self.assertTrue(sett["force_proxy"])

    def test_killswitch_blocks_torrent_when_down(self):
        if not self.engine.available:
            self.skipTest("libtorrent not available")

        config = NetworkConfig(
            interface_name="DisconnectedVPN",
            interface_ip="10.99.99.99",
            kill_switch=True,
        )
        self.engine.apply_network_config(config)

        statuses = []
        self.engine.set_callbacks(
            lambda *args: None,
            lambda did, status, err: statuses.append((did, status, err)),
        )

        entry = DownloadEntry(
            id="t-1",
            url="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=test",
            save_path="C:/Downloads",
            download_type="torrent",
        )
        res = self.engine.add_torrent(entry)
        self.assertFalse(res, "Torrent must be blocked when killswitch is active and VPN is down")
        self.assertTrue(any("Kill switch active" in err for _, _, err in statuses))


class TestManagerVPNIntegration(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.manager = DownloadManager(self.db)

    def tearDown(self):
        self.manager.stop()
        self.db.close()

    def test_manager_network_config_signal(self):
        received = []
        self.manager.network_config_changed.connect(lambda c: received.append(c))

        new_cfg = NetworkConfig(
            interface_name="WireGuard",
            interface_ip="10.8.0.5",
            kill_switch=True,
        )
        self.manager.set_network_config(new_cfg)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].interface_name, "WireGuard")
        self.assertEqual(self.manager.network_config.interface_name, "WireGuard")


class TestNetworkSettingsDialog(unittest.TestCase):

    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_dialog_loads_and_updates_config(self):
        cfg = NetworkConfig(
            interface_name="",
            interface_ip="",
            kill_switch=False,
            proxy_enabled=True,
            proxy_type="socks5",
            proxy_host="127.0.0.1",
            proxy_port=1080,
        )
        dlg = NetworkSettingsDialog(cfg)
        self.assertEqual(dlg._proxy_host_edit.text(), "127.0.0.1")
        self.assertEqual(dlg._proxy_port_spin.value(), 1080)
        self.assertTrue(dlg._proxy_enable_cb.isChecked())

        # Modify values
        dlg._proxy_host_edit.setText("192.168.1.100")
        dlg._proxy_port_spin.setValue(9050)
        dlg._on_save()

        updated = dlg.config
        self.assertEqual(updated.proxy_host, "192.168.1.100")
        self.assertEqual(updated.proxy_port, 9050)
        dlg.close()


if __name__ == "__main__":
    unittest.main()
