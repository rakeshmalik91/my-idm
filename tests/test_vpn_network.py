"""Unit tests for VPN adapter binding, proxy configuration, and kill switch."""

import socket
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
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


class _FakeSnic:
    """Stand-in for a `psutil.net_if_addrs()` address tuple."""

    def __init__(self, family, address):
        self.family = family
        self.address = address
        self.netmask = None
        self.broadcast = None
        self.ptp = None


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

    def test_posix_vpn_interface_names_are_detected(self):
        """A connected VPN on macOS or Linux must be tagged as one.

        macOS names every system-VPN interface ``utunN`` whatever the provider, and BSD/Linux name
        theirs ``pppN`` / ``ipsecN``. Missing those meant a fully connected tunnel was not tagged
        as a VPN, which also demoted it in the interface list - that list is sorted VPNs-first.
        """
        for name in ("utun0", "utun3", "ppp0", "ipsec0", "ipsec_tunnel"):
            with self.subTest(interface=name):
                self.assertTrue(
                    is_vpn_adapter_name(name),
                    f"{name!r} is a VPN interface and must be recognised on a POSIX host",
                )

    def test_common_posix_physical_interfaces_are_not_flagged(self):
        """The additions must not turn ordinary adapters into VPNs.

        Substring matching is a blunt instrument, so the negative cases matter as much as the
        positive ones - ``lo0`` and ``en0`` are the interfaces a false positive would hit first.
        """
        for name in ("lo0", "en0", "eth0", "enp3s0", "wlan0", "ap0", "bridge0"):
            with self.subTest(interface=name):
                self.assertFalse(is_vpn_adapter_name(name), f"{name!r} is not a VPN")

    def test_get_available_interfaces(self):
        """Enumeration must be exercised, not just called on a host with no adapters.

        `get_available_interfaces()` silently returns an empty list when the host
        has no non-loopback IPv4 adapter, which would make a bare `for` loop over
        the result assert nothing at all. psutil is therefore faked first so the
        loop is guaranteed to run over known adapters, and the real host scan is
        still checked afterwards.
        """
        addrs = {
            "NordLynx": [_FakeSnic(socket.AF_INET, "10.5.0.2")],
            "Ethernet": [
                _FakeSnic(socket.AF_INET, "192.168.1.50"),
                _FakeSnic(socket.AF_INET6, "fe80::1234"),
            ],
            "Loopback Only": [_FakeSnic(socket.AF_INET, "127.0.0.1")],
        }
        stats = {
            "NordLynx": SimpleNamespace(isup=True),
            "Ethernet": SimpleNamespace(isup=False),
            "Loopback Only": SimpleNamespace(isup=True),
        }
        with patch("psutil.net_if_addrs", return_value=addrs), \
             patch("psutil.net_if_stats", return_value=stats):
            interfaces = get_available_interfaces()

        self.assertIsInstance(interfaces, list)
        self.assertEqual(
            len(interfaces), 2,
            f"only adapters with a non-loopback IPv4 belong in the list, got {interfaces}",
        )
        by_name = {iface.name: iface for iface in interfaces}
        self.assertNotIn("Loopback Only", by_name, "127.0.0.0/8 adapters must be filtered out")
        self.assertIn("NordLynx", by_name, f"VPN adapter missing from {[i.name for i in interfaces]}")
        self.assertIn("Ethernet", by_name, f"adapter missing from {[i.name for i in interfaces]}")

        self.assertEqual(by_name["NordLynx"].ip, "10.5.0.2", "NordLynx: wrong IPv4 selected")
        self.assertTrue(by_name["NordLynx"].is_up, "NordLynx: isup=True must map to is_up=True")
        self.assertTrue(by_name["NordLynx"].is_vpn, "NordLynx: a VPN adapter name must be flagged")

        self.assertEqual(
            by_name["Ethernet"].ip, "192.168.1.50",
            "Ethernet: the first AF_INET address wins, AF_INET6 must be skipped",
        )
        self.assertFalse(by_name["Ethernet"].is_up, "Ethernet: isup=False must map to is_up=False")
        self.assertFalse(by_name["Ethernet"].is_vpn, "Ethernet: a plain adapter is not a VPN")

        for iface in interfaces:
            self.assertIsInstance(iface, NetworkInterfaceInfo, f"{iface.name}: wrong type")
            self.assertTrue(len(iface.name) > 0, f"{iface.name}: name must not be empty")
            self.assertIsInstance(iface.is_up, bool, f"{iface.name}: is_up must be a bool")

        # The same invariants must hold against the machine actually running the
        # suite; this loop is a smoke check, so an empty host is legitimate here.
        for iface in get_available_interfaces():
            self.assertIsInstance(iface, NetworkInterfaceInfo, f"{iface.name}: wrong type on host")
            self.assertTrue(len(iface.name) > 0, f"{iface.name}: name must not be empty on host")
            self.assertIsInstance(iface.is_up, bool, f"{iface.name}: is_up must be a bool on host")

    def test_is_interface_active(self):
        # Default route is always active
        self.assertTrue(is_interface_active("", ""))
        # Non-existent interface should return False
        self.assertFalse(is_interface_active("NonExistentVPN12345", "10.254.254.254"))


class TestHTTPEngineVPN(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.db.open()
        self.engine = HTTPEngine(self.db)

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
    """Interface binding / proxy / kill switch, verified against a fake session.

    A real `lt.session` binds ``0.0.0.0:6881`` on the machine running the suite and
    ``TorrentEngine.start()`` mkdirs the user's real ``~/.my-idm/fastresume``.
    The whole libtorrent surface is therefore replaced, and the assertions read
    the settings dict the engine hands to ``session.apply_settings`` — which is
    precisely the contract under test.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.fastresume_dir = Path(self.tmp_dir.name) / "fastresume"
        self.fastresume_dir.mkdir(parents=True, exist_ok=True)

        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.db.open()

        lt_patcher = patch("my_idm.torrent_engine.lt")
        self.addCleanup(lt_patcher.stop)
        self.mock_lt = lt_patcher.start()

        has_patcher = patch("my_idm.torrent_engine._HAS_LIBTORRENT", True)
        self.addCleanup(has_patcher.stop)
        has_patcher.start()

        fr_patcher = patch("my_idm.torrent_engine.FASTRESUME_DIR", self.fastresume_dir)
        self.addCleanup(fr_patcher.stop)
        fr_patcher.start()

        self.engine = TorrentEngine(self.db)
        self.addCleanup(self.engine.stop)
        # Registered after engine.stop, so LIFO order clears the handles first and
        # stop() does not spend two seconds draining alerts for a fake handle.
        self.addCleanup(self._discard_handles)

        # `get_settings()` must hand back a real dict, because the engine writes
        # into it and then passes the same object to `apply_settings`.
        self.mock_session = self.mock_lt.session.return_value
        self.mock_session.get_settings.return_value = {}
        self.engine.start()

    def _discard_handles(self):
        self.engine._handles.clear()

    def _applied_settings(self):
        """The settings dict the engine most recently handed to the fake session."""
        self.assertIsNotNone(
            self.mock_session.apply_settings.call_args,
            "engine never called session.apply_settings (settings were rejected)",
        )
        return self.mock_session.apply_settings.call_args[0][0]

    def test_apply_network_config_to_libtorrent(self):
        self.assertTrue(
            self.engine.available,
            "engine must report libtorrent available while _HAS_LIBTORRENT is patched on",
        )
        self.mock_session.get_settings.assert_called()

        # With nothing bound, the session must fall back to the wildcard bind and
        # no forced proxy.
        initial = self._applied_settings()
        self.assertEqual(
            initial["listen_interfaces"], "0.0.0.0:6881,[::]:6881",
            "unbound configuration must listen on the wildcard interfaces",
        )
        self.assertEqual(initial["outgoing_interfaces"], "", "unbound configuration must not pin outgoing traffic")
        self.assertFalse(initial["force_proxy"], "no proxy must be forced when none is configured")

        self.mock_session.apply_settings.reset_mock()

        config = NetworkConfig(
            interface_name="VPN Adapter",
            interface_ip="192.168.1.50",
            proxy_enabled=True,
            proxy_type="socks5",
            proxy_host="proxy.test.com",
            proxy_port=1080,
        )
        self.engine.apply_network_config(config)

        sett = self._applied_settings()
        self.assertIn("192.168.1.50", sett["listen_interfaces"], "the bound IPv4 must be used for listening")
        self.assertEqual(
            sett["listen_interfaces"], "192.168.1.50:6881",
            "listening must be pinned to the bound IP on the torrent port",
        )
        self.assertEqual(sett["outgoing_interfaces"], "192.168.1.50", "outgoing traffic must be pinned too")
        self.assertEqual(sett["proxy_hostname"], "proxy.test.com", "proxy host not applied")
        self.assertEqual(sett["proxy_port"], 1080, "proxy port not applied")
        self.assertEqual(
            sett["proxy_type"], self.mock_lt.proxy_type_t.socks5,
            "an unauthenticated socks5 proxy must use the plain socks5 proxy type",
        )
        self.assertTrue(sett["force_proxy"], "a configured proxy must be forced")
        self.assertTrue(sett["proxy_peer_connections"], "peer connections must go through the proxy")
        self.assertTrue(sett["proxy_tracker_connections"], "tracker connections must go through the proxy")

    def test_killswitch_blocks_torrent_when_down(self):
        self.assertTrue(self.engine.available, "kill switch test needs a libtorrent-capable engine")

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
        self.db.add_download(entry)

        with patch("my_idm.torrent_engine.is_interface_active", return_value=False) as mock_active:
            res = self.engine.add_torrent(entry)

        self.assertFalse(res, "Torrent must be blocked when killswitch is active and VPN is down")
        self.assertTrue(
            any("Kill switch active" in err for _, _, err in statuses),
            f"a kill-switch block must be reported through the status callback; got {statuses}",
        )
        mock_active.assert_called_once_with("DisconnectedVPN", "10.99.99.99")
        self.mock_session.add_torrent.assert_not_called()
        self.assertNotIn(entry.id, self.engine._handles, "a blocked torrent must not be tracked as a handle")
        self.assertEqual(
            self.db.get_download(entry.id).status, "error",
            "a blocked torrent must be marked as errored in the database",
        )

    def test_killswitch_allows_torrent_when_interface_is_up(self):
        """The kill switch must gate on liveness, not block unconditionally."""
        config = NetworkConfig(
            interface_name="LiveVPN",
            interface_ip="10.99.99.99",
            kill_switch=True,
        )
        self.engine.apply_network_config(config)

        entry = DownloadEntry(
            id="t-2",
            url="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=test",
            save_path="C:/Downloads",
            download_type="torrent",
        )
        self.db.add_download(entry)

        info_hash = "0123456789abcdef0123456789abcdef01234567"
        params = MagicMock()
        del params.info_hashes
        params.info_hash = info_hash
        params.name = None  # keeps `original_name` out of the persisted metadata
        self.mock_lt.parse_magnet_uri.return_value = params

        handle = MagicMock()
        del handle.info_hashes
        handle.is_valid.return_value = True
        handle.info_hash.return_value = info_hash
        handle.status.return_value.has_metadata = False
        self.mock_session.add_torrent.return_value = handle

        with patch("my_idm.torrent_engine.is_interface_active", return_value=True) as mock_active:
            res = self.engine.add_torrent(entry)

        self.assertTrue(res, "an up VPN must not block the torrent")
        mock_active.assert_called_once_with("LiveVPN", "10.99.99.99")
        self.mock_session.add_torrent.assert_called_once_with(params)
        self.assertIn(entry.id, self.engine._handles, "an accepted torrent must be tracked as a handle")
        self.assertEqual(
            self.db.get_download(entry.id).torrent_info_hash, info_hash,
            "the info hash read back from the handle must be persisted",
        )
        self.assertNotEqual(
            self.db.get_download(entry.id).status, "error",
            "an accepted torrent must not be marked errored",
        )


class TestManagerVPNIntegration(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.db.open()
        self.manager = DownloadManager(self.db)
        self.addCleanup(self.manager.stop)

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

    def test_manager_bandwidth_limits_and_allocation(self):
        """Manager sets global limits and per-download allocation correctly."""
        limits_received = []
        self.manager.bandwidth_limits_changed.connect(lambda dl, ul: limits_received.append((dl, ul)))

        self.manager.set_bandwidth_limits(102400, 51200)
        self.assertEqual(len(limits_received), 1)
        self.assertEqual(limits_received[0], (102400, 51200))
        self.assertEqual(self.manager.network_config.download_limit, 102400)
        self.assertEqual(self.manager.network_config.upload_limit, 51200)
        self.assertEqual(self.manager._http._download_limit, 102400)

        entry = DownloadEntry(
            id="bw-test-http",
            url="https://example.com/file.dat",
            filename="file.dat",
            status="paused",
        )
        self.db.add_download(entry)

        # Set allocation
        self.manager.set_download_bandwidth_allocation("bw-test-http", "medium")
        self.assertEqual(self.manager.get_download_bandwidth_allocation("bw-test-http"), "medium")

        # Verify effective rate limit for medium (50%) of 102400 is 51200
        eff = self.manager._http._get_effective_download_limit(self.db.get_download("bw-test-http"))
        self.assertEqual(eff, 51200)


class TestNetworkSettingsDialog(unittest.TestCase):

    def setUp(self):
        self.dialogs = []

    def tearDown(self):
        # A dialog that is not closed on a failing assertion stays alive for the
        # rest of the session holding its widgets and its connections.
        while self.dialogs:
            dlg = self.dialogs.pop()
            try:
                dlg.close()
                dlg.deleteLater()
            except Exception:
                pass

    def _make_dialog(self, cfg):
        dlg = NetworkSettingsDialog(cfg)
        self.dialogs.append(dlg)
        return dlg

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
        dlg = self._make_dialog(cfg)
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


if __name__ == "__main__":
    unittest.main()
