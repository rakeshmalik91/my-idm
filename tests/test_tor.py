"""Consolidated unit tests for Tor: service discovery, lifecycle, routing, UI indicators, progress bar, and seamless pause/resume."""

import sys
import tempfile
import threading
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from unittest.mock import MagicMock, patch
from PySide6.QtCore import Qt, QCoreApplication, QEventLoop, QSettings
from PySide6.QtWidgets import QApplication

from my_idm.config import TorConfig
from my_idm.database import Database, DownloadEntry
from my_idm.download_model import DownloadTableModel, Col
from my_idm.http_engine import HTTPEngine
from my_idm.manager import DownloadManager
from my_idm.main_window import MainWindow
from my_idm.tor_service import TorServiceManager, find_tor_executable

app = QApplication.instance() or QApplication([])

# Upper bound for any cross-thread hand-off in this module. Generous enough for a
# loaded CI box, small enough that a genuine hang fails fast instead of stalling
# the whole session.
THREAD_TIMEOUT = 10.0


def pump_until(predicate, timeout=THREAD_TIMEOUT):
    """Pump the Qt event loop until *predicate* holds; return whether it did.

    ``QCoreApplication.processEvents(flags, maxtime)`` blocks until an event
    arrives or the slice expires, so this waits on the event queue instead of
    spinning at 100 Hz and starving the very thread it is waiting for.
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            return False
        QCoreApplication.processEvents(QEventLoop.AllEvents, min(remaining_ms, 100))
    return True


class TestTorDiscovery(unittest.TestCase):
    """Test find_tor_executable resolution."""

    def setUp(self):
        # A TemporaryDirectory keeps the fixture inside one tree that the
        # framework removes, instead of leaving a named temp file behind if the
        # test raises between creation and cleanup.
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)

    def test_custom_valid_path(self):
        temp_exe = Path(self.tmp_dir.name) / "tor.exe"
        temp_exe.write_text("#!/bin/sh\n", encoding="utf-8")
        self.assertTrue(temp_exe.is_file(), "fixture executable was not created")
        found = find_tor_executable(str(temp_exe))
        self.assertEqual(found, str(temp_exe.resolve()))

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

        # `TorServiceManager.stop()` terminates a PID it recorded itself, using
        # `subprocess.run(["taskkill", "/F", "/T", "/PID", pid])` on Windows and
        # `os.kill(pid, 15)` elsewhere. These tests fake the spawned process, so
        # those PIDs are fictional and must never be resolved against the host's
        # real process table. Patch both for the whole test — including tearDown,
        # which calls stop() — rather than relying on every test to remember.
        run_patcher = patch("subprocess.run")
        self.addCleanup(run_patcher.stop)
        self.mock_run = run_patcher.start()

        kill_patcher = patch("os.kill")
        self.addCleanup(kill_patcher.stop)
        self.mock_kill = kill_patcher.start()

    def tearDown(self):
        try:
            self.service.stop()
        finally:
            self.tmp_dir.cleanup()

    @patch("my_idm.tor_service.is_tor_reachable", return_value=True)
    def test_start_already_running(self, mock_reachable):
        success, msg = self.service.start()
        self.assertTrue(success, "a reachable Tor on the configured port is a successful start")
        self.assertIn("existing Tor service", msg)

    @patch("my_idm.tor_service.is_tor_reachable", return_value=False)
    @patch("my_idm.tor_service.find_tor_executable", return_value=None)
    def test_start_binary_not_found(self, mock_find, mock_reachable):
        success, msg = self.service.start()
        self.assertFalse(success, "a missing tor.exe must not report success")
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
        self.assertTrue(success, "spawned Tor must report success once the SOCKS port answers")
        self.assertIn("started and connected", msg)
        self.assertTrue(self.service.is_spawned, "is_spawned must be True for a live process we launched")
        self.assertEqual(mock_popen.call_count, 1, "Tor must be spawned exactly once")
        argv = mock_popen.call_args[0][0]
        self.assertEqual(argv[0], "C:\\Tools\\tor.exe", "the discovered executable must be argv[0]")
        self.assertIn("--SocksPort", argv)
        self.assertIn("9050", argv)

        # tearDown calls stop() anyway; doing it here turns the faked PID into an
        # assertion about the termination contract instead of a silent risk.
        self.service.stop()
        self.assertTrue(
            self.mock_run.called or self.mock_kill.called,
            "stop() must terminate the PID it spawned",
        )
        if self.mock_run.called:
            argv = self.mock_run.call_args[0][0]
            self.assertEqual(argv[0], "taskkill")
            self.assertIn("/F", argv)
            self.assertIn("/T", argv)
            self.assertEqual(argv[argv.index("/PID") + 1], "1234")
        else:
            self.assertEqual(self.mock_kill.call_args[0][0], 1234)
        proc.terminate.assert_called_once()
        self.assertFalse(self.service.is_spawned, "is_spawned must be False once stopped")
        self.assertFalse(
            (self.data_dir / "tor.pid").exists(),
            "stop() must remove the pid file it wrote",
        )

    @patch("my_idm.tor_service.is_tor_reachable", return_value=True)
    @patch("subprocess.run")
    def test_external_tor_not_terminated_on_stop(self, mock_run, mock_reachable):
        """When Tor was already running before start(), stop() must NOT kill external process."""
        success, _ = self.service.start()
        self.assertTrue(success, "connecting to an already-running Tor is a success")
        self.assertFalse(self.service.is_spawned, "an adopted external service is not 'spawned'")

        self.service.stop()
        mock_run.assert_not_called()
        self.assertFalse(
            self.mock_run.called,
            "no PID may be terminated when Tor was never spawned by us",
        )

    @patch("subprocess.run")
    def test_stale_pid_file_ignored_when_not_spawned(self, mock_run):
        """If a stale tor.pid exists, stop() ignores it if My-IDM did not spawn Tor."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        pid_file = self.data_dir / "tor.pid"
        pid_file.write_text("99999", encoding="utf-8")

        self.assertFalse(self.service.is_spawned)
        self.service.stop()
        mock_run.assert_not_called()
        self.assertFalse(
            self.mock_run.called,
            "a stale pid file must not authorise killing a process we did not spawn",
        )

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
        self.assertFalse(success, "a Tor that dies on a bound port must not report success")
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
        self.assertFalse(success, "a Tor that dies on a locked data dir must not report success")
        self.assertIn("already using the data directory", msg)

    @patch("my_idm.tor_service.is_tor_reachable")
    @patch("my_idm.tor_service.find_tor_executable", return_value=None)
    def test_binary_not_found_with_tor_browser_running(self, mock_find, mock_reachable):
        """When binary not found on port 9050, but Tor Browser is running on port 9150, advise user."""
        def reachable_check(h, p, timeout=0.5):
            return p == 9150
        mock_reachable.side_effect = reachable_check

        success, msg = self.service.start()
        self.assertFalse(success, "no executable means no Tor to connect to")
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
            self.assertTrue(cfg.enabled, "auto_start=True must keep the saved enabled flag")
            self.assertTrue(cfg.auto_start_at_startup)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


class TestPerDownloadTorRouting(unittest.TestCase):
    """Per-download Tor routing: flag, availability gating, badge and HTTP session."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.db.open()
        self.mgr = DownloadManager(self.db)
        self.addCleanup(self.mgr.stop)
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
        froze the GUI on every context-menu open and every row repaint. The
        contract is that it is a *pure* read of the cached flag, so assert that
        directly instead of timing a loop that never did any I/O anyway.
        """
        with patch("my_idm.manager.is_tor_reachable") as mock_probe:
            self.mgr._tor_available_cache = True
            self.assertTrue(self.mgr.tor_available(), "a cached True flag must be returned verbatim")
            self.mgr._tor_available_cache = False
            self.assertFalse(self.mgr.tor_available(), "a cached False flag must be returned verbatim")
        mock_probe.assert_not_called()

    def test_refresh_tor_availability_probes_in_background(self):
        """A probe runs off-thread and updates the cache when it lands."""
        probed = threading.Event()
        calls = []

        def fake_probe(host, port, timeout=0.4):
            calls.append((host, port, timeout))
            probed.set()
            return True

        with patch("my_idm.manager.is_tor_reachable", side_effect=fake_probe):
            self.assertTrue(
                self.mgr.refresh_tor_availability(),
                "refresh_tor_availability must report that it scheduled a probe",
            )
            self.assertTrue(probed.wait(timeout=THREAD_TIMEOUT), "tor probe thread never ran")
            # The probe thread hands the result back over a queued signal, so the
            # cache can only be updated once the GUI thread drains the event queue.
            self.assertTrue(
                pump_until(lambda: len(calls) == 1 and not self.mgr._tor_probe_in_flight),
                f"probe result never reached the GUI thread; calls={calls}",
            )

        self.assertTrue(self.mgr.tor_available(), "a successful probe must publish True")
        self.assertEqual(len(calls), 1, f"exactly one probe expected, got {calls}")
        self.assertEqual(
            calls[0][0], self.mgr.tor_config.proxy_host,
            "the probe must target the configured Tor host",
        )
        self.assertEqual(
            calls[0][1], self.mgr.tor_config.proxy_port,
            "the probe must target the configured Tor port",
        )

    def test_refresh_publishes_changes_only_once(self):
        seen = []
        calls = []
        self.mgr.tor_availability_changed.connect(lambda live: seen.append(live))

        def fake_probe(host, port, timeout=0.4):
            calls.append((host, port, timeout))
            return True

        # `_tor_probe_in_flight` is cleared by the slot that applies the result, so
        # a False value means the probe has already been applied on this thread.
        def landed(count):
            return len(calls) == count and not self.mgr._tor_probe_in_flight

        with patch("my_idm.manager.is_tor_reachable", side_effect=fake_probe):
            self.assertTrue(self.mgr.refresh_tor_availability(), "first refresh must schedule a probe")
            self.assertTrue(
                pump_until(lambda: landed(1)),
                f"first probe never landed; calls={calls}",
            )
            # A repeated probe with the same result must not re-emit.
            self.assertTrue(self.mgr.refresh_tor_availability(), "second refresh must schedule a probe")
            self.assertTrue(
                pump_until(lambda: landed(2)),
                f"second probe never landed; calls={calls}",
            )

        self.assertTrue(self.mgr.tor_available(), "the cache must hold the last probe result")
        self.assertEqual(len(calls), 2, f"exactly two probes expected, got {calls}")
        self.assertEqual(seen, [True], f"expected exactly one change, got {seen}")

    def test_probe_failure_leaves_cache_false(self):
        calls = []

        def failing_probe(host, port, timeout=0.4):
            calls.append((host, port, timeout))
            raise OSError("boom")

        self.assertFalse(self.mgr.tor_available(), "the cache must start empty")
        with patch("my_idm.manager.is_tor_reachable", side_effect=failing_probe):
            self.assertTrue(
                self.mgr.refresh_tor_availability(),
                "a raising probe must still be reported as scheduled",
            )
            self.assertTrue(
                pump_until(lambda: len(calls) == 1 and not self.mgr._tor_probe_in_flight),
                f"failing probe never reported back; calls={calls}",
            )
        self.assertFalse(self.mgr.tor_available(), "a probe that raised must leave the cache False")
        self.assertEqual(len(calls), 1, f"a failed probe must not be retried silently; calls={calls}")

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
        self.addCleanup(self.db.close)
        self.db.open()
        self.manager = DownloadManager(self.db)
        self.addCleanup(self.manager.stop)

        # `MainWindow.__init__` fires an immediate Tor probe and arms a 4 s
        # repeating one. Keep the probe off the host's real sockets for the whole
        # test (including teardown, which closes the window) by patching before
        # the window exists.
        probe = patch("my_idm.manager.is_tor_reachable", return_value=True)
        self.addCleanup(probe.stop)
        self.mock_probe = probe.start()

        self.win = MainWindow(self.manager)
        self.addCleanup(self._close_window, self.win)
        self.win.show()

    def tearDown(self):
        # The 4 s availability timer is parented to the window, so it survives
        # `close()` when the close event is intercepted for close-to-tray.
        timer = getattr(self.win, "_tor_availability_timer", None)
        if timer is not None:
            timer.stop()

    @staticmethod
    def _close_window(win):
        """Actually close the window.

        `MainWindow.closeEvent` ignores the event and hides the window whenever
        `close_to_tray` / `enable_system_tray` are on, which is the default, so
        `_force_exit` has to be set first or the widget (and its child timer)
        stays alive for the rest of the session.
        """
        timer = getattr(win, "_tor_availability_timer", None)
        if timer is not None:
            timer.stop()
        win._force_exit = True
        win.close()
        win.deleteLater()
        QApplication.processEvents()

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
        statuses = []
        self.manager.tor_status_changed.connect(lambda state, msg: statuses.append((state, msg)))

        with patch.object(self.manager, "pause_download", side_effect=lambda did: paused_order.append(did)), \
             patch.object(self.manager, "resume_download", side_effect=lambda did: resumed_order.append(did)), \
             patch.object(self.manager._tor_service, "start", return_value=(True, "Connected")):

            success, msg = self.manager.toggle_tor(True)
            self.assertTrue(success, "a successful Tor start must make toggle_tor succeed")
            self.assertEqual(msg, "Connected", "toggle_tor must surface the service's own message")

            self.assertIn("d1", paused_order)
            self.assertIn("d2", paused_order)
            self.assertNotIn("d3", paused_order)

            self.assertIn("d1", resumed_order)
            self.assertIn("d2", resumed_order)
            self.assertNotIn("d3", resumed_order)

        self.assertTrue(self.manager.tor_config.enabled, "Tor config must be enabled after a successful toggle")
        self.assertIn(
            ("connected", "Connected"), statuses,
            f"the 'connected' status must be published to the UI; got {statuses}",
        )

    def test_resume_downloads_when_tor_start_fails(self):
        """Active downloads are resumed under direct routing if Tor start fails."""
        e1 = DownloadEntry(id="d1", url="http://example.com/1.zip", filename="1.zip", save_path="/tmp", status="downloading")
        self.db.add_download(e1)

        paused_order = []
        resumed_order = []
        statuses = []
        self.manager.tor_status_changed.connect(lambda state, msg: statuses.append((state, msg)))

        with patch.object(self.manager, "pause_download", side_effect=lambda did: paused_order.append(did)), \
             patch.object(self.manager, "resume_download", side_effect=lambda did: resumed_order.append(did)), \
             patch.object(self.manager._tor_service, "start", return_value=(False, "Failed to connect")):

            success, msg = self.manager.toggle_tor(True)
            self.assertFalse(success, "a failed Tor start must not report success")
            self.assertEqual(msg, "Failed to connect", "the failure reason must reach the caller")
            self.assertIn("d1", paused_order)
            self.assertIn("d1", resumed_order)

        self.assertFalse(
            self.manager.tor_config.enabled,
            "a failed Tor start must leave routing disabled so downloads use direct connections",
        )
        self.assertIn(
            ("error", "Failed to connect"), statuses,
            f"the failure must be published to the UI; got {statuses}",
        )

    def test_tor_progressbar_and_green_style(self):
        """Toolbar and footer buttons show progress bar during toggle and turn green when ON."""
        self.assertFalse(self.win._tor_toolbar_progress.isVisible(), "no spinner before any Tor status")
        self.assertFalse(self.win._tor_footer_progress.isVisible(), "no spinner before any Tor status")

        # Connecting state
        self.win._on_tor_status_changed("connecting", "Starting Tor...")
        self.assertTrue(self.win._tor_toolbar_progress.isVisible(), "toolbar spinner must show while connecting")
        self.assertTrue(self.win._tor_footer_progress.isVisible(), "footer spinner must show while connecting")
        self.assertIn("Connecting", self.win._tor_status_btn.text())
        self.assertIn("Connecting", self.win._act_tor.text())

        # Connected state
        self.manager.tor_config.enabled = True
        self.win._on_tor_status_changed("connected", "Tor connected")
        self.assertFalse(self.win._tor_toolbar_progress.isVisible(), "toolbar spinner must stop when connected")
        self.assertFalse(self.win._tor_footer_progress.isVisible(), "footer spinner must stop when connected")
        self.assertIn("#50fa7b", self.win._tor_status_btn.styleSheet())
        self.assertIn("#50fa7b", self.win._tor_toolbar_btn.styleSheet())

        # Disconnecting state
        self.win._on_tor_status_changed("disconnecting", "Stopping Tor...")
        self.assertTrue(self.win._tor_toolbar_progress.isVisible(), "toolbar spinner must show while disconnecting")
        self.assertTrue(self.win._tor_footer_progress.isVisible(), "footer spinner must show while disconnecting")
        self.assertIn("Disconnecting", self.win._tor_status_btn.text())
        self.assertIn("Disconnecting", self.win._act_tor.text())

        # Disconnected state
        self.manager.tor_config.enabled = False
        self.win._on_tor_status_changed("disconnected", "Tor deactivated")
        self.assertFalse(self.win._tor_toolbar_progress.isVisible(), "toolbar spinner must stop when disconnected")
        self.assertFalse(self.win._tor_footer_progress.isVisible(), "footer spinner must stop when disconnected")
        self.assertNotIn("#50fa7b", self.win._tor_status_btn.styleSheet())
        self.assertNotIn("#50fa7b", self.win._tor_toolbar_btn.styleSheet())


class TestTorSettingsDialogDetection(unittest.TestCase):
    """Test the smart alternative port detection in SettingsDialog."""

    def setUp(self):
        from my_idm.settings_dialog import SettingsDialog
        self.dialog = SettingsDialog()
        self.addCleanup(self.dialog.close)
        self.addCleanup(self.dialog.deleteLater)

    @patch("my_idm.settings_dialog.is_tor_reachable")
    def test_detects_tor_browser_on_port_9150(self, mock_reachable):
        """When checking port 9050 and it's unreachable, detect Tor Browser on 9150."""
        self.dialog._tor_port_spin.setValue(9050)
        mock_reachable.side_effect = lambda h, p: p == 9150

        self.dialog._on_test_tor()
        self.assertIn("Tor Browser is active on port 9150", self.dialog._tor_test_status_lbl.text())
        self.assertIn(
            9050, mock_reachable.call_args_list[0][0],
            "the configured port must be probed before the alternative one",
        )

    @patch("my_idm.settings_dialog.is_tor_reachable")
    def test_detects_tor_service_on_port_9050(self, mock_reachable):
        """When checking port 9150 and it's unreachable, detect Tor Service on 9050."""
        self.dialog._tor_port_spin.setValue(9150)
        mock_reachable.side_effect = lambda h, p: p == 9050

        self.dialog._on_test_tor()
        self.assertIn("Tor Service is active on port 9050", self.dialog._tor_test_status_lbl.text())


if __name__ == "__main__":
    unittest.main()
