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
from my_idm.http_engine import HTTPEngine
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

    @patch("my_idm.tor_service.is_tor_reachable", return_value=True)
    @patch("subprocess.run")
    def test_external_tor_not_terminated_on_stop(self, mock_run, mock_reachable):
        """When Tor was already running before start(), stop() must NOT kill external process."""
        success, _ = self.service.start()
        self.assertTrue(success)
        self.assertFalse(self.service.is_spawned)

        self.service.stop()
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_stale_pid_file_ignored_when_not_spawned(self, mock_run):
        """If a stale tor.pid exists, stop() ignores it if My-IDM did not spawn Tor."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        pid_file = self.data_dir / "tor.pid"
        pid_file.write_text("99999", encoding="utf-8")

        self.assertFalse(self.service.is_spawned)
        self.service.stop()
        mock_run.assert_not_called()

    @patch("my_idm.tor_service.find_tor_executable", return_value="C:\\Tools\\tor.exe")
    @patch("my_idm.tor_service.is_tor_reachable", side_effect=[False, False])
    @patch("subprocess.Popen")
    def test_port_conflict_detection(self, mock_popen, mock_reachable, mock_find):
        """When tor fails to start due to port already in use, a clear diagnostic is returned."""
        proc = MagicMock()
        proc.poll.return_value = 1
        proc.communicate.return_value = (b"", b"[warn] Could not bind to 127.0.0.1:9050: Address already in use. Is Tor already running?")
        mock_popen.return_value = proc

        success, msg = self.service.start()
        self.assertFalse(success)
        self.assertIn("already in use by another application or Tor instance", msg)
        self.assertIn("Port 9050", msg)

    @patch("my_idm.tor_service.find_tor_executable", return_value="C:\\Tools\\tor.exe")
    @patch("my_idm.tor_service.is_tor_reachable", side_effect=[False, False])
    @patch("subprocess.Popen")
    def test_data_dir_locked_conflict_detection(self, mock_popen, mock_reachable, mock_find):
        """When tor fails to start because data dir is locked, a clear message is returned."""
        proc = MagicMock()
        proc.poll.return_value = 1
        proc.communicate.return_value = (b"", b"[err] It appears something else is already using this data directory. If not, delete ...")
        mock_popen.return_value = proc

        success, msg = self.service.start()
        self.assertFalse(success)
        self.assertIn("already using the data directory", msg)

    @patch("my_idm.tor_service.is_tor_reachable")
    @patch("my_idm.tor_service.find_tor_executable", return_value=None)
    def test_binary_not_found_with_tor_browser_running(self, mock_find, mock_reachable):
        """When binary not found on port 9050, but Tor Browser is running on port 9150, advise user."""
        def reachable_check(h, p, timeout=0.5):
            return p == 9150
        mock_reachable.side_effect = reachable_check

        success, msg = self.service.start()
        self.assertFalse(success)
        self.assertIn("Tor Browser was detected actively running on port 9150", msg)


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


class TestPerDownloadTorRouting(unittest.TestCase):
    """Per-download Tor routing: flag, availability gating, badge and HTTP session."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(":memory:")
        self.db.open()
        self.mgr = DownloadManager(self.db)
        self.model = DownloadTableModel()
        self.model.set_tor_availability_provider(self.mgr.tor_available)
        self.entry = DownloadEntry(
            id="t1",
            filename="file.zip",
            url="https://example.com/file.zip",
            download_type="http",
            save_path=".",
            file_path="./file.zip",
            total_size=1000,
            downloaded_size=0,
            status="downloading",
        )
        self.db.add_download(self.entry)
        self.model.load_entries(self.db.get_all_downloads())

    def tearDown(self):
        self.mgr.stop()
        self.db.close()
        self.tmp.cleanup()

    def _enable_tor(self):
        """Patch Tor as reachable so enabling a per-download route is permitted."""
        return patch.object(self.mgr, "tor_available", return_value=True)

    # -- flag storage --------------------------------------------------------

    def test_flag_defaults_false(self):
        self.assertFalse(self.mgr.is_download_tor_routed("t1"))

    def test_flag_round_trips_through_metadata(self):
        with self._enable_tor():
            ok, _ = self.mgr.set_download_tor_route("t1", True)
        self.assertTrue(ok)
        self.assertTrue(self.mgr.is_download_tor_routed("t1"))
        self.assertTrue(self.db.get_download("t1").metadata.get("route_through_tor"))
        with self._enable_tor():
            self.mgr.set_download_tor_route("t1", False)
        self.assertFalse(self.mgr.is_download_tor_routed("t1"))

    def test_enabling_requires_live_tor(self):
        with patch.object(self.mgr, "tor_available", return_value=False):
            ok, msg = self.mgr.set_download_tor_route("t1", True)
        self.assertFalse(ok)
        self.assertIn("not running", msg.lower())
        self.assertFalse(self.mgr.is_download_tor_routed("t1"))

    def test_disabling_is_allowed_even_without_tor(self):
        with patch.object(self.mgr, "tor_available", return_value=True):
            self.mgr.set_download_tor_route("t1", True)
        with patch.object(self.mgr, "tor_available", return_value=False):
            ok, _ = self.mgr.set_download_tor_route("t1", False)
        self.assertTrue(ok)
        self.assertFalse(self.mgr.is_download_tor_routed("t1"))

    def test_unknown_download_rejected(self):
        ok, msg = self.mgr.set_download_tor_route("nope", True)
        self.assertFalse(ok)
        self.assertIn("not found", msg.lower())

    def test_tor_available_is_cached_and_non_blocking(self):
        """tor_available() must not touch the network.

        Probing inline blocked for the socket timeout (1 s on Windows), which
        froze the GUI on every context-menu open and every row repaint.
        """
        import time

        with patch.object(self.mgr.tor_service, "is_running", return_value=True):
            start = time.perf_counter()
            for _ in range(500):
                self.mgr.tor_available()
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.2, "tor_available() must be a cached read")

    def test_refresh_tor_availability_probes_in_background(self):
        """A probe runs off-thread and updates the cache when it lands."""
        import time

        with patch("my_idm.manager.is_tor_reachable", return_value=True):
            self.assertTrue(self.mgr.refresh_tor_availability())
            deadline = time.time() + 5
            while time.time() < deadline and not self.mgr.tor_available():
                app.processEvents()
                time.sleep(0.01)
        self.assertTrue(self.mgr.tor_available())

    def test_refresh_publishes_changes_only_once(self):
        import time

        seen = []
        self.mgr.tor_availability_changed.connect(lambda live: seen.append(live))
        with patch("my_idm.manager.is_tor_reachable", return_value=True):
            self.mgr.refresh_tor_availability()
            deadline = time.time() + 5
            while time.time() < deadline and not self.mgr.tor_available():
                app.processEvents()
                time.sleep(0.01)
        # A repeated probe with the same result must not re-emit.
        with patch("my_idm.manager.is_tor_reachable", return_value=True):
            self.mgr.refresh_tor_availability()
            for _ in range(40):
                app.processEvents()
                time.sleep(0.01)
        self.assertEqual(seen, [True], f"expected exactly one change, got {seen}")

    def test_probe_failure_leaves_cache_false(self):
        import time

        self.assertFalse(self.mgr.tor_available())
        with patch("my_idm.manager.is_tor_reachable", side_effect=OSError("boom")):
            self.mgr.refresh_tor_availability()
            for _ in range(40):
                app.processEvents()
                time.sleep(0.01)
        self.assertFalse(self.mgr.tor_available())

    # -- badge ---------------------------------------------------------------

    def test_flag_alone_does_not_light_the_badge(self):
        """A flag with Tor down must not claim an active Tor route."""
        self.model.set_tor_availability_provider(lambda: False)
        with self._enable_tor():
            self.mgr.set_download_tor_route("t1", True)
        entry = self.db.get_download("t1")
        self.assertFalse(self.model.is_tor_active_for(entry))

    def test_badge_lights_when_flagged_and_tor_running(self):
        self.model.set_tor_availability_provider(lambda: True)
        with self._enable_tor():
            self.mgr.set_download_tor_route("t1", True)
        entry = self.db.get_download("t1")
        self.assertTrue(self.model.is_tor_routed_by_choice(entry))
        # Enabling restarts an active download, so pin the status explicitly.
        entry.status = "downloading"
        self.assertTrue(self.model.is_tor_active_for(entry))

    def test_badge_off_when_not_transferring(self):
        self.model.set_tor_availability_provider(lambda: True)
        with self._enable_tor():
            self.mgr.set_download_tor_route("t1", True)
        entry = self.db.get_download("t1")
        entry.status = "paused"
        self.assertFalse(self.model.is_tor_active_for(entry))

    def test_badge_off_when_tor_stops(self):
        self.model.set_tor_availability_provider(lambda: True)
        with self._enable_tor():
            self.mgr.set_download_tor_route("t1", True)
        self.model.set_tor_availability_provider(lambda: False)
        entry = self.db.get_download("t1")
        self.assertFalse(self.model.is_tor_active_for(entry))

    def test_global_tor_still_works_without_the_flag(self):
        self.model.set_tor_availability_provider(lambda: True)
        from my_idm.config import TorConfig as _TC
        cfg = _TC(enabled=True, route_http=True, route_torrent=True)
        self.model.set_tor_config(cfg)
        entry = self.db.get_download("t1")
        self.assertFalse(self.model.is_tor_routed_by_choice(entry))
        self.assertTrue(self.model.is_tor_active_for(entry))

    def test_availability_provider_failure_is_contained(self):
        def boom():
            raise RuntimeError("probe failed")
        self.model.set_tor_availability_provider(boom)
        self.assertFalse(self.model.tor_available())

    def test_no_provider_means_unavailable(self):
        self.model.set_tor_availability_provider(None)
        self.assertFalse(self.model.tor_available())

    # -- HTTP engine ---------------------------------------------------------

    def test_http_engine_session_selection(self):
        """A flagged download uses the Tor session; others use the default."""
        from my_idm.http_engine import HTTPEngine
        from my_idm.config import TorConfig

        engine = HTTPEngine(self.db)
        engine._session = object()
        engine._tor_session = "TOR-SESSION"
        engine._tor_config = TorConfig(enabled=True, route_http=True)

        plain = self.db.get_download("t1")
        self.assertFalse(engine._is_tor_routed(plain))

        with patch.object(self.mgr, "tor_available", return_value=True):
            self.mgr.set_download_tor_route("t1", True)
        flagged = self.db.get_download("t1")
        self.assertTrue(engine._is_tor_routed(flagged))

    def test_tor_flagged_entry_skips_general_proxy(self):
        """The general HTTP proxy must not also apply to a Tor-routed download."""
        from my_idm.config import TorConfig
        from my_idm.network import NetworkConfig

        engine = HTTPEngine(self.db)
        engine._tor_config = TorConfig(enabled=True, route_http=True)
        engine._network_config = NetworkConfig(
            proxy_enabled=True, proxy_host="p", proxy_port=1
        )

        with self._enable_tor():
            self.mgr.set_download_tor_route("t1", True)
        entry = self.db.get_download("t1")

        kwargs = engine._request_kwargs({}, entry=entry, url=entry.url)
        self.assertNotIn("proxy", kwargs)

    def test_plain_entry_still_uses_general_proxy(self):
        from my_idm.config import TorConfig
        from my_idm.network import NetworkConfig

        engine = HTTPEngine(self.db)
        engine._tor_config = TorConfig(enabled=False)
        engine._network_config = NetworkConfig(
            proxy_enabled=True, proxy_host="p", proxy_port=1
        )

        entry = self.db.get_download("t1")
        kwargs = engine._request_kwargs({}, entry=entry, url=entry.url)
        self.assertIn("proxy", kwargs)
        self.assertEqual(kwargs["proxy"], "http://p:1")

    def test_engine_flag_setter_is_idempotent(self):
        engine = self.mgr._http
        engine.set_download_tor_route("t1", True)
        first = self.db.get_download("t1").metadata_json
        engine.set_download_tor_route("t1", True)
        self.assertEqual(self.db.get_download("t1").metadata_json, first)

    # -- torrent engine ------------------------------------------------------

    def test_torrent_route_flag_recorded(self):
        torrent = DownloadEntry(
            id="tt1", filename="a.torrent", url="magnet:?xt=urn:btih:abc",
            download_type="torrent", save_path=".", file_path="./a.torrent",
            status="completed",
        )
        self.db.add_download(torrent)
        self.mgr._torrent.set_torrent_tor_route("tt1", True)
        self.assertTrue(self.mgr._torrent.is_torrent_tor_routed("tt1"))
        self.assertTrue(self.db.get_download("tt1").metadata.get("route_through_tor"))


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
        status_http = self.model.data(self.model.index(row_http, Col.STATUS), Qt.ItemDataRole.DisplayRole)

        self.assertIn("🧅", name_http)
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


class TestTorSettingsDialogDetection(unittest.TestCase):
    """Test the smart alternative port detection in SettingsDialog."""

    def setUp(self):
        from my_idm.settings_dialog import SettingsDialog
        self.dialog = SettingsDialog()

    def tearDown(self):
        self.dialog.close()

    @patch("my_idm.settings_dialog.is_tor_reachable")
    def test_detects_tor_browser_on_port_9150(self, mock_reachable):
        """When checking port 9050 and it's unreachable, detect Tor Browser on 9150."""
        self.dialog._tor_port_spin.setValue(9050)
        mock_reachable.side_effect = lambda h, p: p == 9150

        self.dialog._on_test_tor()
        self.assertIn("Tor Browser is active on port 9150", self.dialog._tor_test_status_lbl.text())

    @patch("my_idm.settings_dialog.is_tor_reachable")
    def test_detects_tor_service_on_port_9050(self, mock_reachable):
        """When checking port 9150 and it's unreachable, detect Tor Service on 9050."""
        self.dialog._tor_port_spin.setValue(9150)
        mock_reachable.side_effect = lambda h, p: p == 9050

        self.dialog._on_test_tor()
        self.assertIn("Tor Service is active on port 9050", self.dialog._tor_test_status_lbl.text())


if __name__ == "__main__":
    unittest.main()
