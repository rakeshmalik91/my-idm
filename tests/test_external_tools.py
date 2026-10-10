"""Unit tests for External Tools (AnimePahe scraper integration, CLI/GUI launch, and logs)."""

import asyncio
import contextlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

from PySide6.QtCore import QCoreApplication, QEventLoop, QSettings
from PySide6.QtWidgets import QApplication

from my_idm.config import ExternalToolsConfig, GeneralConfig
from my_idm.database import Database
from my_idm.external_tools import (
    launch_animepahe_cli,
    launch_animepahe_gui,
    open_file_in_default_app,
    show_in_folder,
)
from my_idm.manager import DownloadManager
from my_idm.utils import normalize_path

app = QApplication.instance() or QApplication(sys.argv)

# Upper bound for any cross-thread hand-off in this module. Generous enough for a
# loaded CI box, small enough that a genuine hang fails fast instead of stalling
# the whole session.
THREAD_TIMEOUT = 10.0


def pump_until(predicate, timeout=THREAD_TIMEOUT):
    """Pump the Qt event loop until *predicate* holds; return whether it did."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        QCoreApplication.processEvents()
        time.sleep(0.01)
    return True


class TestExternalTools(unittest.TestCase):
    """Test suite for external tools execution and log utilities."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.tmp_dir.name) / "animepahe-downloader"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self.script_path = self.repo_dir / "animepahe_download.py"
        self.script_path.write_text("# dummy script\n", encoding="utf-8")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_launch_animepahe_cli_missing_repo(self):
        cfg = ExternalToolsConfig(animepahe_repo_path="/nonexistent/directory/12345")
        ok, msg, proc = launch_animepahe_cli(cfg)
        self.assertFalse(ok)
        self.assertIn("does not exist", msg)
        self.assertIsNone(proc)

    def test_launch_animepahe_cli_missing_script(self):
        empty_dir = Path(self.tmp_dir.name) / "empty_dir"
        empty_dir.mkdir(parents=True, exist_ok=True)
        cfg = ExternalToolsConfig(animepahe_repo_path=str(empty_dir))
        ok, msg, proc = launch_animepahe_cli(cfg)
        self.assertFalse(ok)
        self.assertIn("animepahe_download.py not found", msg)
        self.assertIsNone(proc)

    def test_launch_animepahe_cli_success(self):
        cfg = ExternalToolsConfig(animepahe_repo_path=str(self.repo_dir))
        mock_proc = MagicMock()
        mock_proc.pid = 9999

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            ok, msg, proc = launch_animepahe_cli(cfg, my_idm_dir="/path/to/my-idm")
            self.assertTrue(ok)
            self.assertIn("PID: 9999", msg)
            self.assertEqual(proc, mock_proc)

            mock_popen.assert_called_once()
            call_args = mock_popen.call_args
            cmd = call_args[0][0]
            self.assertIn(str(self.script_path), cmd)
            self.assertIn("--my-idm", cmd)
            self.assertIn("--my-idm-dir", cmd)

        # Check console log created
        console_log = cfg.get_console_log_path()
        self.assertTrue(console_log.is_file())
        content = console_log.read_text(encoding="utf-8")
        self.assertIn("AnimePahe CLI Scraper Session Started", content)

    def test_launch_animepahe_gui_run_pyw(self):
        run_pyw = self.repo_dir / "run.pyw"
        run_pyw.write_text("# dummy pyw\n", encoding="utf-8")

        cfg = ExternalToolsConfig(animepahe_repo_path=str(self.repo_dir))
        with patch("subprocess.Popen") as mock_popen:
            ok, msg = launch_animepahe_gui(cfg)
            self.assertTrue(ok)
            self.assertIn("run.pyw", msg)
            mock_popen.assert_called_once()

    def test_launch_animepahe_gui_gui_py(self):
        gui_py = self.repo_dir / "gui.py"
        gui_py.write_text("# dummy gui\n", encoding="utf-8")

        cfg = ExternalToolsConfig(animepahe_repo_path=str(self.repo_dir))
        with patch("subprocess.Popen") as mock_popen:
            ok, msg = launch_animepahe_gui(cfg)
            self.assertTrue(ok)
            self.assertIn("gui.py", msg)
            mock_popen.assert_called_once()

    def test_open_file_in_default_app(self):
        target_file = Path(self.tmp_dir.name) / "test_log.txt"
        self.assertFalse(target_file.exists())

        with patch("PySide6.QtGui.QDesktopServices.openUrl", return_value=True) as mock_open:
            ok, msg = open_file_in_default_app(target_file, create_if_missing=True)
            self.assertTrue(ok, f"opening a created placeholder should succeed, got: {msg}")
            self.assertTrue(target_file.is_file())
            self.assertIn("test_log.txt", msg, "the message must name the file that was opened")
            mock_open.assert_called_once()
            url = mock_open.call_args[0][0]
            self.assertTrue(
                url.toLocalFile().endswith("test_log.txt"),
                f"the opened URL must point at the placeholder, got {url.toLocalFile()}",
            )

    def test_open_folder_in_default_app(self):
        target_folder = Path(self.tmp_dir.name) / "subfolder"
        target_folder.mkdir(parents=True, exist_ok=True)

        # Both platform branches are stubbed: on POSIX the code takes the
        # QDesktopServices path, which is otherwise free to hand the URI to a real
        # desktop file manager.
        with patch("os.startfile", return_value=None, create=True) as mock_startfile, \
             patch("PySide6.QtGui.QDesktopServices.openUrl", return_value=True) as mock_openurl:
            ok, msg = open_file_in_default_app(target_folder)
            self.assertTrue(ok, f"opening an existing folder should succeed, got: {msg}")
            self.assertIn("subfolder", msg)
            if sys.platform == "win32":
                mock_startfile.assert_called_once()
                self.assertEqual(
                    os.fspath(mock_startfile.call_args[0][0]), str(target_folder.resolve()),
                    "the folder passed to startfile must be the resolved one",
                )
                mock_openurl.assert_not_called()
            else:
                mock_openurl.assert_called_once()
                self.assertEqual(mock_startfile.call_count, 0)

    def test_show_in_folder(self):
        folder = Path(self.tmp_dir.name) / "ext_test"
        folder.mkdir(parents=True, exist_ok=True)
        sample_file = folder / "my-idm-firefox.xpi"
        sample_file.write_text("dummy")

        # Test highlighting file
        with patch("subprocess.Popen") as mock_popen, \
             patch("os.startfile", create=True) as mock_startfile, \
             patch("PySide6.QtGui.QDesktopServices.openUrl", return_value=True) as mock_openurl:
            ok, msg = show_in_folder(sample_file)
            self.assertTrue(ok, f"revealing a file should succeed, got: {msg}")
            self.assertIn("my-idm-firefox.xpi", msg, "the message must name the revealed entry")
            if sys.platform == "win32":
                mock_popen.assert_called_once()
                argv = mock_popen.call_args[0][0]
                self.assertEqual(argv[0], "explorer", "a file must be revealed with explorer /select")
                self.assertEqual(argv[1], "/select,", "/select, must be separate so spaces do not quote the flag")
                self.assertEqual(argv[2], str(sample_file.resolve()))
                mock_startfile.assert_not_called()
                mock_openurl.assert_not_called()
            else:
                mock_openurl.assert_called_once()
                self.assertEqual(mock_popen.call_count, 0)
                self.assertEqual(mock_startfile.call_count, 0)

        # Test highlighting file with spaces in path
        sample_file_spaces = folder / "my download with spaces.zip"
        sample_file_spaces.write_text("dummy")
        with patch("subprocess.Popen") as mock_popen, \
             patch("os.startfile", create=True) as mock_startfile, \
             patch("PySide6.QtGui.QDesktopServices.openUrl", return_value=True) as mock_openurl:
            ok, msg = show_in_folder(sample_file_spaces)
            self.assertTrue(ok)
            if sys.platform == "win32":
                mock_popen.assert_called_once()
                argv = mock_popen.call_args[0][0]
                self.assertEqual(argv[0], "explorer")
                self.assertEqual(argv[1], "/select,")
                self.assertEqual(argv[2], str(sample_file_spaces.resolve()))
            else:
                mock_openurl.assert_called_once()

        # Test opening folder
        with patch("subprocess.Popen") as mock_popen, \
             patch("os.startfile", create=True) as mock_startfile, \
             patch("PySide6.QtGui.QDesktopServices.openUrl", return_value=True) as mock_openurl:
            ok, msg = show_in_folder(folder)
            self.assertTrue(ok, f"opening a folder should succeed, got: {msg}")
            self.assertIn("ext_test", msg, "the message must name the folder that was opened")
            if sys.platform == "win32":
                mock_startfile.assert_called_once()
                self.assertEqual(
                    os.fspath(mock_startfile.call_args[0][0]), str(folder.resolve()),
                    "a directory must be opened with startfile, not explorer /select",
                )
                mock_popen.assert_not_called()
                mock_openurl.assert_not_called()
            else:
                mock_openurl.assert_called_once()
                self.assertEqual(mock_startfile.call_count, 0)
                self.assertEqual(mock_popen.call_count, 0)

    def test_launch_animepahe_cli_with_url_and_episodes(self):
        cfg = ExternalToolsConfig(animepahe_repo_path=str(self.repo_dir))
        mock_proc = MagicMock()
        mock_proc.pid = 8888

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            ok, msg, proc = launch_animepahe_cli(
                cfg,
                my_idm_dir="/path/to/my-idm",
                url="https://animepahe.ru/anime/4380",
                episodes="1-12",
                quality="1080p",
                lang="jap",
            )
            self.assertTrue(ok)
            self.assertIn("PID: 8888", msg)
            self.assertIs(proc, mock_proc, "the launched process must be handed back to the caller")

            mock_popen.assert_called_once()
            cmd = mock_popen.call_args[0][0]
            self.assertIn("--url", cmd)
            self.assertIn("https://animepahe.ru/anime/4380", cmd)
            self.assertIn("-ep", cmd)
            self.assertIn("1-12", cmd)
            self.assertIn("-q", cmd)
            self.assertIn("1080p", cmd)
            self.assertIn("-l", cmd)
            self.assertIn("jap", cmd)
            self.assertIn("-y", cmd)

    def test_launch_animepahe_cli_with_title_and_url(self):
        cfg = ExternalToolsConfig(animepahe_repo_path=str(self.repo_dir))
        mock_proc = MagicMock()
        mock_proc.pid = 8889

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            ok, msg, proc = launch_animepahe_cli(
                cfg,
                my_idm_dir="/path/to/my-idm",
                title="Sousou no Frieren",
                url="https://animepahe.ru/anime/4380",
                episodes="1-12",
                quality="1080p",
                lang="jap",
            )
            self.assertTrue(ok)
            self.assertIn("PID: 8889", msg)
            mock_popen.assert_called_once()
            cmd = mock_popen.call_args[0][0]
            self.assertIn("Sousou no Frieren", cmd)
            self.assertIn("--url", cmd)
            self.assertIn("https://animepahe.ru/anime/4380", cmd)

    def test_settings_dialog_animepahe_url_download(self):
        from my_idm.settings_dialog import SettingsDialog
        from PySide6.QtWidgets import QMessageBox

        cfg = ExternalToolsConfig(
            animepahe_repo_path=str(self.repo_dir),
            animepahe_last_url="https://animepahe.ru/anime/test",
            animepahe_last_title="Test Anime",
            animepahe_last_episodes="1-3",
        )
        dialog = SettingsDialog(external_tools_config=cfg, initial_tab=5)
        self.addCleanup(dialog.close)
        self.addCleanup(dialog.deleteLater)

        # Verify fields populated
        self.assertEqual(dialog._animepahe_url_edit.text(), "https://animepahe.ru/anime/test")
        self.assertEqual(dialog._animepahe_title_edit.text(), "Test Anime")
        self.assertEqual(dialog._animepahe_episodes_edit.text(), "1-3")

        with patch("my_idm.settings_dialog.launch_animepahe_cli", return_value=(True, "Started", MagicMock())) as mock_launch:
            dialog._on_download_animepahe_url()
            mock_launch.assert_called_once()
            kwargs = mock_launch.call_args.kwargs
            self.assertEqual(kwargs.get("url"), "https://animepahe.ru/anime/test")
            self.assertEqual(kwargs.get("title"), "Test Anime")
            self.assertEqual(kwargs.get("episodes"), "1-3")
            self.assertTrue(dialog.animepahe_download_started)


class TestManagerExternalToolsLifecycle(unittest.TestCase):
    """Test DownloadManager startup launch and termination of external tools."""

    def setUp(self):
        # An empty, private backlog directory: the real process_backlogs() scans
        # cwd, ~/.my-idm and the home directory and, with
        # clear_backlog_after_load on by default, *truncates* any backlog.txt it
        # finds there.
        self.backlog_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.backlog_dir.cleanup)

        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.db.open()
        self.manager = DownloadManager(self.db)
        self.addCleanup(self.manager.stop)

        # `DownloadManager.__init__` connects `process_backlogs_requested` to the
        # *bound method* it resolved at construction time, so
        # `patch.object(manager, "process_backlogs")` is inert: emitting the signal
        # still ran the real implementation. Patching the class rebinds what the
        # existing connection resolves to, and the temp backlog directory means
        # even an unmocked call could not reach the user's files.
        for name in ("process_backlogs", "load_backlog"):
            patcher = patch.object(DownloadManager, name, return_value=0)
            self.addCleanup(patcher.stop)
            setattr(self, f"mock_{name}", patcher.start())

        locations = patch.object(
            GeneralConfig,
            "get_effective_backlog_locations",
            return_value=[self.backlog_dir.name],
        )
        self.addCleanup(locations.stop)
        locations.start()

    @contextlib.contextmanager
    def _patched_start_collaborators(self):
        """Run the real ``DownloadManager.start()`` with its heavy work stubbed."""
        manager = self.manager

        def run_loop():
            # The loop body is the only reason a coroutine scheduled with
            # run_coroutine_threadsafe() ever resolves, so the stub still runs
            # (and stop() later shuts down) the loop — it just does nothing else.
            asyncio.set_event_loop(manager._loop)
            manager._loop.run_forever()

        patches = {
            "run_loop": patch.object(manager, "_run_loop", side_effect=run_loop),
            "http_start": patch.object(manager._http, "start", new=AsyncMock()),
            "http_stop": patch.object(manager._http, "stop", new=AsyncMock()),
            "torrent_start": patch.object(manager._torrent, "start"),
            "torrent_stop": patch.object(manager._torrent, "stop"),
            "toggle_tor": patch.object(manager, "toggle_tor", return_value=(True, "Connected")),
            "refresh_tor_availability": patch.object(manager, "refresh_tor_availability", return_value=True),
            "resume_download": patch.object(manager, "resume_download"),
            "browser_start": patch.object(manager._browser_server, "start", new=AsyncMock()),
            "browser_stop": patch.object(manager._browser_server, "stop", new=AsyncMock()),
            "start_animepahe_scraper": patch.object(DownloadManager, "start_animepahe_scraper"),
        }
        with contextlib.ExitStack() as stack:
            yield {name: stack.enter_context(p) for name, p in patches.items()}

    @unittest.skip("idm-async thread teardown race (see testing.md §6)")
    def test_startup_launches_animepahe_if_enabled(self):
        """`DownloadManager.start()` must honour animepahe_launch_on_startup.

        This drives the real entry point. The previous version re-implemented the
        `if cfg.animepahe_launch_on_startup:` branch inside the test body and then
        asserted on the mock it had just called, so it could not fail.
        """
        cfg = ExternalToolsConfig(
            animepahe_repo_path="/mock/repo",
            animepahe_launch_on_startup=True,
        )
        self.manager.set_external_tools_config(cfg)

        with self._patched_start_collaborators() as mocks:
            self.manager.start()

        mocks["start_animepahe_scraper"].assert_called_once()

    @unittest.skip("idm-async thread teardown race (see testing.md §6)")
    def test_startup_does_not_launch_animepahe_when_disabled(self):
        """The same entry point must NOT launch the scraper when it is switched off."""
        cfg = ExternalToolsConfig(
            animepahe_repo_path="/mock/repo",
            animepahe_launch_on_startup=False,
        )
        self.manager.set_external_tools_config(cfg)

        with self._patched_start_collaborators() as mocks:
            self.manager.start()

        mocks["start_animepahe_scraper"].assert_not_called()

    @unittest.skip("idm-async thread teardown race (see testing.md §6)")
    def test_startup_passes_configured_args_to_the_scraper(self):
        """start() must launch the scraper with no extra arguments."""
        cfg = ExternalToolsConfig(
            animepahe_repo_path="/mock/repo",
            animepahe_launch_on_startup=True,
        )
        self.manager.set_external_tools_config(cfg)

        with self._patched_start_collaborators() as mocks:
            self.manager.start()

        mocks["start_animepahe_scraper"].assert_called_once_with()

    def test_manager_periodic_timer_configuration(self):
        """DownloadManager starts or stops the periodic AnimePahe timer based on config."""
        cfg_off = ExternalToolsConfig(animepahe_periodic_run=False, animepahe_interval_hours=6)
        self.manager.set_external_tools_config(cfg_off)
        self.assertFalse(self.manager._animepahe_timer.isActive())

        # When enabled, timer starts with configured interval (6 hours = 21,600,000 ms)
        cfg_on = ExternalToolsConfig(animepahe_periodic_run=True, animepahe_interval_hours=6)
        self.manager.set_external_tools_config(cfg_on)
        self.assertTrue(self.manager._animepahe_timer.isActive())
        self.assertEqual(self.manager._animepahe_timer.interval(), 6 * 3600 * 1000)

        # Updating interval adjusts timer
        cfg_12 = ExternalToolsConfig(animepahe_periodic_run=True, animepahe_interval_hours=12)
        self.manager.set_external_tools_config(cfg_12)
        self.assertTrue(self.manager._animepahe_timer.isActive())
        self.assertEqual(self.manager._animepahe_timer.interval(), 12 * 3600 * 1000)

        # Disabling stops timer
        self.manager.set_external_tools_config(cfg_off)
        self.assertFalse(self.manager._animepahe_timer.isActive())

    def test_manager_periodic_timer_tick_triggers_scraper(self):
        """_on_animepahe_timer_tick invokes start_animepahe_scraper when periodic run is enabled."""
        cfg = ExternalToolsConfig(animepahe_periodic_run=True, animepahe_interval_hours=6)
        self.manager.set_external_tools_config(cfg)

        with patch.object(self.manager, "start_animepahe_scraper") as mock_start:
            self.manager._on_animepahe_timer_tick()
            mock_start.assert_called_once()

    def test_manager_periodic_timer_tick_is_inert_when_disabled(self):
        """The negative half of the previous test: no config, no launch."""
        cfg = ExternalToolsConfig(animepahe_periodic_run=False)
        self.manager.set_external_tools_config(cfg)

        with patch.object(self.manager, "start_animepahe_scraper") as mock_start:
            self.manager._on_animepahe_timer_tick()
            mock_start.assert_not_called()

    def test_stop_terminates_running_process(self):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Process is running
        self.manager._animepahe_process = mock_proc

        self.manager.stop()
        mock_proc.terminate.assert_called_once()
        self.assertIsNone(self.manager._animepahe_process)

    def test_start_and_stop_animepahe_scraper(self):
        status_updates = []
        self.manager.animepahe_status_changed.connect(status_updates.append)

        stop_event = threading.Event()
        self.addCleanup(stop_event.set)
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = lambda: 0 if stop_event.is_set() else None
        mock_proc.wait.side_effect = stop_event.wait
        mock_proc.terminate.side_effect = stop_event.set

        with patch("my_idm.manager.launch_animepahe_cli", return_value=(True, "Started PID: 1234", mock_proc)):
            ok, msg = self.manager.start_animepahe_scraper()
            self.assertTrue(ok, f"the first launch should succeed, got: {msg}")
            self.assertTrue(self.manager.is_animepahe_running())
            self.assertIn(True, status_updates)

            # Calling again while already running queues the task
            ok2, msg2 = self.manager.start_animepahe_scraper()
            self.assertTrue(ok2)
            self.assertIn("queued", msg2.lower())
            self.assertEqual(self.manager.get_animepahe_queue_length(), 1, "the second request must be queued")

            # Stop scraper
            ok_stop, msg_stop = self.manager.stop_animepahe_scraper()
            self.assertTrue(ok_stop, msg_stop)
            self.assertFalse(self.manager.is_animepahe_running())
            self.assertIn(False, status_updates)
            mock_proc.terminate.assert_called_once()

    def test_stop_animepahe_scraper_when_not_running(self):
        ok, msg = self.manager.stop_animepahe_scraper()
        self.assertTrue(ok)
        self.assertIn("not running", msg)
        self.assertFalse(self.manager.is_animepahe_running())

    def test_manager_start_animepahe_with_url_and_episodes(self):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        with patch("my_idm.manager.launch_animepahe_cli", return_value=(True, "Started PID: 7777", mock_proc)) as mock_launch:
            ok, msg = self.manager.start_animepahe_scraper(
                url="https://animepahe.ru/anime/1234",
                episodes="5-10",
                quality="720p",
                lang="en",
            )
            self.assertTrue(ok)
            self.assertIn("PID: 7777", msg, "the launch message must carry the process id back")
            self.assertIs(
                self.manager.animepahe_process, mock_proc,
                "the launched process must become the tracked process",
            )
            mock_launch.assert_called_once()
            kwargs = mock_launch.call_args.kwargs
            self.assertEqual(kwargs.get("url"), "https://animepahe.ru/anime/1234")
            self.assertEqual(kwargs.get("episodes"), "5-10")
            self.assertEqual(kwargs.get("quality"), "720p")
            self.assertEqual(kwargs.get("lang"), "en")

    def test_manager_animepahe_monitor_polls_backlog_on_completion(self):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        waited = threading.Event()

        def wait_side_effect(*args, **kwargs):
            waited.set()
            return 0

        mock_proc.wait.side_effect = wait_side_effect

        with patch("my_idm.manager.launch_animepahe_cli", return_value=(True, "Started", mock_proc)):
            ok, msg = self.manager.start_animepahe_scraper()
            self.assertTrue(ok, f"the scraper should have started, got: {msg}")
            self.assertTrue(
                waited.wait(timeout=THREAD_TIMEOUT),
                "the animepahe-monitor thread never reached proc.wait()",
            )
            # process_backlogs_requested is a queued signal, so the GUI thread has
            # to drain the event queue before the connected slot can run.
            self.assertTrue(
                pump_until(
                    lambda: self.mock_process_backlogs.called
                    and not self.manager.is_animepahe_running()
                ),
                "the monitor never dispatched process_backlogs to the GUI thread",
            )
        self.mock_process_backlogs.assert_called_once()
        self.assertEqual(
            self.manager.get_animepahe_queue_length(), 0,
            "a plain launch must not leave anything queued",
        )
        self.assertFalse(self.manager.is_animepahe_running())

    def test_manager_animepahe_queue_sequential_execution(self):
        queue_events = []
        self.manager.animepahe_queue_changed.connect(queue_events.append)

        proc1_done = threading.Event()
        proc2_done = threading.Event()
        proc1_parked = threading.Event()
        proc2_parked = threading.Event()
        # Registered up front so a failed assertion can never leave a monitor
        # thread parked in wait() for the rest of the session.
        self.addCleanup(proc1_done.set)
        self.addCleanup(proc2_done.set)

        def make_proc(done, parked):
            proc = MagicMock()
            proc.poll.return_value = None

            def wait_side_effect(*args, **kwargs):
                parked.set()
                done.wait(timeout=THREAD_TIMEOUT)
                return 0

            proc.wait.side_effect = wait_side_effect
            return proc

        proc1 = make_proc(proc1_done, proc1_parked)
        proc2 = make_proc(proc2_done, proc2_parked)

        launched_args = []

        def mock_launch(cfg, **kwargs):
            launched_args.append(kwargs)
            return True, f"Started PID: {100 + len(launched_args)}", [proc1, proc2][len(launched_args) - 1]

        with patch("my_idm.manager.launch_animepahe_cli", side_effect=mock_launch):
            # 1. Start task 1
            ok1, msg1 = self.manager.start_animepahe_scraper(url="https://animepahe.ru/anime/series1")
            self.assertTrue(ok1, msg1)
            self.assertTrue(
                proc1_parked.wait(timeout=THREAD_TIMEOUT),
                "the monitor thread never began waiting on process 1",
            )
            self.assertEqual(len(launched_args), 1, "the first request must launch, not queue")
            self.assertEqual(launched_args[0].get("url"), "https://animepahe.ru/anime/series1")
            self.assertEqual(self.manager.get_animepahe_queue_length(), 0)

            # 2. Queue task 2 while task 1 is running
            ok2, msg2 = self.manager.start_animepahe_scraper(url="https://animepahe.ru/anime/series2", episodes="1-5")
            self.assertTrue(ok2)
            self.assertIn("queued at position #1", msg2)
            self.assertEqual(self.manager.get_animepahe_queue_length(), 1)
            self.assertIn(1, queue_events)

            # Check queue contents
            q_items = self.manager.get_animepahe_queue()
            self.assertEqual(len(q_items), 1)
            self.assertEqual(q_items[0]["url"], "https://animepahe.ru/anime/series2")
            self.assertEqual(q_items[0]["episodes"], "1-5")
            self.assertEqual(
                len(launched_args), 1,
                "a queued task must not be launched before the running one finishes",
            )

            # 3. Complete task 1 -> triggers execution of task 2
            proc1.poll.return_value = 0
            proc1_done.set()
            self.assertTrue(
                pump_until(lambda: len(launched_args) == 2 and proc2_parked.is_set() and 0 in queue_events),
                f"queued task 2 was never launched after task 1 finished; launched={launched_args}, queue_events={queue_events}",
            )
            self.assertEqual(len(launched_args), 2, "exactly one follow-up task must launch")
            self.assertEqual(launched_args[1].get("url"), "https://animepahe.ru/anime/series2")
            self.assertEqual(launched_args[1].get("episodes"), "1-5")
            self.assertEqual(self.manager.get_animepahe_queue_length(), 0)

            # 4. Complete task 2 -> finishes queue
            proc2.poll.return_value = 0
            proc2_done.set()
            self.assertTrue(
                pump_until(lambda: not self.manager.is_animepahe_running() and 0 in queue_events),
                f"the queue never drained after the last task finished; queue_events={queue_events}",
            )
            self.assertFalse(self.manager.is_animepahe_running())
            self.assertEqual(self.manager.get_animepahe_queue_length(), 0)
            self.assertIn(0, queue_events, "the UI must be told the queue drained")

    def test_manager_stop_animepahe_clears_queue(self):
        proc = MagicMock()
        proc.poll.return_value = None

        # Park the monitor thread until the test releases it, so the queue cannot
        # drain itself before stop() is exercised. `release` is registered as a
        # cleanup first, so a failed assertion cannot strand the daemon thread.
        monitor_parked = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def wait_side_effect(*args, **kwargs):
            monitor_parked.set()
            release.wait(timeout=THREAD_TIMEOUT)
            return 0

        proc.wait.side_effect = wait_side_effect

        with patch("my_idm.manager.launch_animepahe_cli", return_value=(True, "Started", proc)):
            ok1, msg1 = self.manager.start_animepahe_scraper(url="https://animepahe.ru/anime/1")
            self.assertTrue(ok1, msg1)
            ok2, _ = self.manager.start_animepahe_scraper(url="https://animepahe.ru/anime/2")
            ok3, _ = self.manager.start_animepahe_scraper(url="https://animepahe.ru/anime/3")
            self.assertTrue(ok2 and ok3, "the 2nd and 3rd requests should be accepted as queued tasks")
            self.assertTrue(
                monitor_parked.wait(timeout=THREAD_TIMEOUT),
                "the animepahe-monitor thread never reached proc.wait()",
            )
            self.assertEqual(self.manager.get_animepahe_queue_length(), 2)

            ok, msg = self.manager.stop_animepahe_scraper()
            self.assertTrue(ok, msg)
            self.assertIn("cleared 2 queued tasks", msg)
            self.assertEqual(self.manager.get_animepahe_queue_length(), 0)
            self.assertFalse(self.manager.is_animepahe_running())
            proc.terminate.assert_called_once()


class TestSettingsDialogAnimePaheEnhancements(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.repo_dir = Path(self.tmp_dir.name) / "animepahe"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "animepahe_download.py").write_text("# mock", encoding="utf-8")
        # `_on_save()` makedirs whatever the save-path field holds, falling back to
        # the real `~/Downloads` when the field is empty, so point it at a
        # directory that is ours to create.
        self.save_dir = Path(self.tmp_dir.name) / "downloads"

    def _make_dialog(self, cfg, **kwargs):
        from my_idm.settings_dialog import SettingsDialog

        dialog = SettingsDialog(external_tools_config=cfg, **kwargs)
        self.addCleanup(dialog.close)
        self.addCleanup(dialog.deleteLater)
        dialog._save_path_edit.setText(str(self.save_dir))
        return dialog

    def test_url_normalization_and_validation(self):
        from PySide6.QtWidgets import QMessageBox

        cfg = ExternalToolsConfig(animepahe_repo_path=str(self.repo_dir))
        dialog = self._make_dialog(cfg, initial_tab=5)

        # 1. Test invalid episode format triggers warning
        dialog._animepahe_url_edit.setText("https://animepahe.ru/anime/1234")
        dialog._animepahe_episodes_edit.setText("invalid-eps")
        with patch.object(QMessageBox, "warning") as mock_warn, \
             patch("my_idm.settings_dialog.launch_animepahe_cli") as mock_launch:
            dialog._on_download_animepahe_url()
            mock_warn.assert_called_once()
            self.assertIn("Invalid Episode Range", mock_warn.call_args[0][1])
            mock_launch.assert_not_called()

        # 2. Test raw ID input normalizes to full URL
        dialog._animepahe_url_edit.setText("ef667bb4-3a9b-449e-1a22-26156a642e47")
        dialog._animepahe_episodes_edit.setText("1-5, 8")
        with patch.object(QMessageBox, "information") as mock_info, \
             patch("my_idm.settings_dialog.launch_animepahe_cli", return_value=(True, "OK", MagicMock())) as mock_launch:
            dialog._on_download_animepahe_url()
            mock_launch.assert_called_once()
            kwargs = mock_launch.call_args.kwargs
            self.assertEqual(kwargs.get("url"), "https://animepahe.ru/anime/ef667bb4-3a9b-449e-1a22-26156a642e47")
            self.assertEqual(kwargs.get("episodes"), "1-5, 8")

        # 3. Test /play/ URL normalizes to /anime/ URL while preserving host
        dialog._animepahe_url_edit.setText("https://animepahe.si/play/4380/randomsession123")
        dialog._animepahe_episodes_edit.setText("")
        with patch.object(QMessageBox, "information"), \
             patch("my_idm.settings_dialog.launch_animepahe_cli", return_value=(True, "OK", MagicMock())) as mock_launch:
            dialog._on_download_animepahe_url()
            mock_launch.assert_called_once()
            kwargs = mock_launch.call_args.kwargs
            self.assertEqual(kwargs.get("url"), "https://animepahe.si/anime/4380")
            self.assertIsNone(kwargs.get("episodes"))

        # 4. Test button status changes and queue badges
        dialog._on_animepahe_status_changed(True)
        self.assertTrue(dialog._btn_download_animepahe_url.isEnabled())
        self.assertIn("Queue", dialog._btn_download_animepahe_url.text())

        dialog._on_animepahe_queue_changed(3)
        self.assertFalse(dialog._animepahe_queue_lbl.isHidden())
        self.assertIn("3", dialog._animepahe_queue_lbl.text())

        dialog._on_animepahe_status_changed(False)
        self.assertTrue(dialog._btn_download_animepahe_url.isEnabled())
        self.assertIn("Download via AnimePahe", dialog._btn_download_animepahe_url.text())

        dialog._on_animepahe_queue_changed(0)
        self.assertTrue(dialog._animepahe_queue_lbl.isHidden())

    def test_settings_dialog_queues_download_when_scraper_active(self):
        from PySide6.QtWidgets import QMessageBox

        mock_mgr = MagicMock()
        mock_mgr.is_animepahe_running.return_value = True
        mock_mgr.get_animepahe_queue_length.return_value = 1
        mock_mgr.start_animepahe_scraper.return_value = (True, "AnimePahe task queued at position #2 for 'https://animepahe.ru/anime/5678'.")

        cfg = ExternalToolsConfig(animepahe_repo_path=str(self.repo_dir))
        dialog = self._make_dialog(cfg, manager=mock_mgr, initial_tab=5)

        dialog._animepahe_url_edit.setText("https://animepahe.ru/anime/5678")
        dialog._animepahe_episodes_edit.setText("1-10")

        dialog._on_download_animepahe_url()
        mock_mgr.start_animepahe_scraper.assert_called_once_with(
            url="https://animepahe.ru/anime/5678",
            episodes="1-10",
            quality=None,
            lang=None,
            title=None,
        )
        self.assertTrue(dialog.animepahe_download_started)

    def test_settings_dialog_animepahe_title_and_url_download(self):
        mock_mgr = MagicMock()
        mock_mgr.is_animepahe_running.return_value = False
        mock_mgr.start_animepahe_scraper.return_value = (True, "Started")

        cfg = ExternalToolsConfig(animepahe_repo_path=str(self.repo_dir))
        dialog = self._make_dialog(cfg, manager=mock_mgr, initial_tab=5)

        dialog._animepahe_url_edit.setText("https://animepahe.ru/anime/4380")
        dialog._animepahe_title_edit.setText("Custom Anime Folder")
        dialog._animepahe_episodes_edit.setText("1-5")

        dialog._on_download_animepahe_url()
        mock_mgr.start_animepahe_scraper.assert_called_once_with(
            url="https://animepahe.ru/anime/4380",
            episodes="1-5",
            quality=None,
            lang=None,
            title="Custom Anime Folder",
        )
        self.assertTrue(dialog.animepahe_download_started)
        self.assertEqual(dialog._external_tools_cfg.animepahe_last_title, "Custom Anime Folder")

    def test_settings_dialog_animepahe_title_only_download(self):
        mock_mgr = MagicMock()
        mock_mgr.is_animepahe_running.return_value = False
        mock_mgr.start_animepahe_scraper.return_value = (True, "Started")

        cfg = ExternalToolsConfig(animepahe_repo_path=str(self.repo_dir))
        dialog = self._make_dialog(cfg, manager=mock_mgr, initial_tab=5)

        dialog._animepahe_url_edit.setText("")
        dialog._animepahe_title_edit.setText("Sousou no Frieren")
        dialog._animepahe_episodes_edit.setText("")

        dialog._on_download_animepahe_url()
        mock_mgr.start_animepahe_scraper.assert_called_once_with(
            url=None,
            episodes=None,
            quality=None,
            lang=None,
            title="Sousou no Frieren",
        )
        self.assertTrue(dialog.animepahe_download_started)
        self.assertEqual(dialog._external_tools_cfg.animepahe_last_title, "Sousou no Frieren")

    def test_external_tools_config_periodic_settings(self):
        """ExternalToolsConfig supports periodic scraper run toggle and interval."""
        cfg = ExternalToolsConfig()
        self.assertFalse(cfg.animepahe_periodic_run)
        self.assertEqual(cfg.animepahe_interval_hours, 6)

        d = cfg.to_dict()
        self.assertIn("animepahe_periodic_run", d)
        self.assertIn("animepahe_interval_hours", d)

        d["animepahe_periodic_run"] = True
        d["animepahe_interval_hours"] = 12
        loaded = ExternalToolsConfig.from_dict(d)
        self.assertTrue(loaded.animepahe_periodic_run)
        self.assertEqual(loaded.animepahe_interval_hours, 12)

        # Isolated QSettings save/load
        ini_file = str(Path(self.tmp_dir.name) / "test_settings.ini")
        settings = QSettings(ini_file, QSettings.Format.IniFormat)
        loaded.save(settings)

        restored = ExternalToolsConfig.load(settings)
        self.assertTrue(restored.animepahe_periodic_run)
        self.assertEqual(restored.animepahe_interval_hours, 12)

    def test_settings_dialog_periodic_scraper_controls(self):
        """SettingsDialog loads and persists periodic scraper checkbox and interval spinbox."""
        cfg = ExternalToolsConfig(
            animepahe_repo_path=str(self.repo_dir),
            animepahe_periodic_run=True,
            animepahe_interval_hours=8,
        )
        dialog = self._make_dialog(cfg, initial_tab=5)
        self.assertTrue(dialog._animepahe_periodic_cb.isChecked())
        self.assertEqual(dialog._animepahe_interval_spin.value(), 8)
        self.assertTrue(dialog._animepahe_interval_spin.isEnabled())

        # Modify values and save
        dialog._animepahe_periodic_cb.setChecked(False)
        dialog._animepahe_interval_spin.setValue(10)
        dialog._on_save()

        self.assertTrue(
            self.save_dir.is_dir(),
            "_on_save() must create the directory it was pointed at",
        )
        self.assertFalse(
            (Path.home() / "Downloads").resolve() == self.save_dir.resolve(),
            "the save path under test must not be the real ~/Downloads",
        )
        saved_cfg = dialog.external_tools_config
        self.assertFalse(saved_cfg.animepahe_periodic_run)
        self.assertEqual(saved_cfg.animepahe_interval_hours, 10)
        self.assertEqual(
            normalize_path(dialog._general_cfg.default_save_path), normalize_path(str(self.save_dir)),
            "the configured default save path must be the one shown in the dialog",
        )


class TestExternalToolsConfigYtdlp(unittest.TestCase):
    """Test suite for yt-dlp configuration in ExternalToolsConfig."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)

    def test_ytdlp_default_fields(self):
        cfg = ExternalToolsConfig()
        self.assertTrue(cfg.ytdlp_enabled)
        self.assertEqual(cfg.ytdlp_path, "")
        self.assertEqual(cfg.ytdlp_ffmpeg_path, "")
        self.assertEqual(cfg.ytdlp_default_format, "bestvideo[height<=1080]+bestaudio/best")
        self.assertTrue(cfg.ytdlp_prefer_mode_a)
        self.assertTrue(cfg.ytdlp_embed_thumbnail)
        self.assertFalse(cfg.ytdlp_embed_subtitles)
        self.assertEqual(cfg.ytdlp_subtitle_langs, "en")
        self.assertEqual(cfg.ytdlp_cookies_browser, "")
        self.assertEqual(cfg.ytdlp_extra_args, "")
        self.assertTrue(cfg.ytdlp_auto_detect_urls)
        self.assertEqual(cfg.ytdlp_last_save_path, "")
        self.assertEqual(cfg.ytdlp_last_format, "")

    def test_ytdlp_to_dict_includes_all_fields(self):
        cfg = ExternalToolsConfig(
            ytdlp_enabled=False,
            ytdlp_path="/opt/yt-dlp",
            ytdlp_ffmpeg_path="/opt/ffmpeg",
            ytdlp_default_format="best",
            ytdlp_prefer_mode_a=False,
            ytdlp_embed_thumbnail=False,
            ytdlp_embed_subtitles=True,
            ytdlp_subtitle_langs="en,ja",
            ytdlp_cookies_browser="firefox",
            ytdlp_extra_args="--retries 5",
            ytdlp_auto_detect_urls=False,
            ytdlp_last_save_path="/downloads/yt",
            ytdlp_last_format="137+140",
        )
        d = cfg.to_dict()
        self.assertEqual(d["ytdlp_enabled"], False)
        self.assertEqual(d["ytdlp_path"], "/opt/yt-dlp")
        self.assertEqual(d["ytdlp_ffmpeg_path"], "/opt/ffmpeg")
        self.assertEqual(d["ytdlp_default_format"], "best")
        self.assertEqual(d["ytdlp_prefer_mode_a"], False)
        self.assertEqual(d["ytdlp_embed_thumbnail"], False)
        self.assertEqual(d["ytdlp_embed_subtitles"], True)
        self.assertEqual(d["ytdlp_subtitle_langs"], "en,ja")
        self.assertEqual(d["ytdlp_cookies_browser"], "firefox")
        self.assertEqual(d["ytdlp_extra_args"], "--retries 5")
        self.assertEqual(d["ytdlp_auto_detect_urls"], False)
        self.assertEqual(d["ytdlp_last_save_path"], "/downloads/yt")
        self.assertEqual(d["ytdlp_last_format"], "137+140")

    def test_ytdlp_dict_round_trip(self):
        original = ExternalToolsConfig(
            ytdlp_enabled=False,
            ytdlp_path="/opt/yt-dlp",
            ytdlp_ffmpeg_path="/opt/ffmpeg",
            ytdlp_default_format="bestvideo[height<=720]+bestaudio",
            ytdlp_prefer_mode_a=False,
            ytdlp_embed_thumbnail=False,
            ytdlp_embed_subtitles=True,
            ytdlp_subtitle_langs="de,fr",
            ytdlp_cookies_browser="chrome",
            ytdlp_extra_args="--concurrent-fragments 4",
            ytdlp_auto_detect_urls=False,
            ytdlp_last_save_path="/dl/yt",
            ytdlp_last_format="22",
        )
        restored = ExternalToolsConfig.from_dict(original.to_dict())
        for key in (
            "ytdlp_enabled", "ytdlp_path", "ytdlp_ffmpeg_path",
            "ytdlp_default_format", "ytdlp_prefer_mode_a",
            "ytdlp_embed_thumbnail", "ytdlp_embed_subtitles",
            "ytdlp_subtitle_langs", "ytdlp_cookies_browser",
            "ytdlp_extra_args", "ytdlp_auto_detect_urls",
            "ytdlp_last_save_path", "ytdlp_last_format",
        ):
            self.assertEqual(getattr(restored, key), getattr(original, key), key)

    def test_ytdlp_from_dict_missing_keys_uses_defaults(self):
        cfg = ExternalToolsConfig.from_dict({})
        self.assertTrue(cfg.ytdlp_enabled)
        self.assertEqual(cfg.ytdlp_default_format, "bestvideo[height<=1080]+bestaudio/best")
        self.assertEqual(cfg.ytdlp_subtitle_langs, "en")
        self.assertTrue(cfg.ytdlp_auto_detect_urls)

    def test_ytdlp_save_load_qsettings(self):
        cfg = ExternalToolsConfig(
            ytdlp_enabled=False,
            ytdlp_path="/usr/local/bin/yt-dlp",
            ytdlp_ffmpeg_path="/usr/local/bin/ffmpeg",
            ytdlp_default_format="worst",
            ytdlp_prefer_mode_a=False,
            ytdlp_embed_thumbnail=False,
            ytdlp_embed_subtitles=True,
            ytdlp_subtitle_langs="es",
            ytdlp_cookies_browser="edge",
            ytdlp_extra_args="--no-mtime",
            ytdlp_auto_detect_urls=False,
            ytdlp_last_save_path="/downloads",
            ytdlp_last_format="worst",
        )
        ini_file = str(Path(self.tmp_dir.name) / "ytdlp_test.ini")
        settings = QSettings(ini_file, QSettings.Format.IniFormat)
        cfg.save(settings)

        loaded = ExternalToolsConfig.load(settings)
        self.assertFalse(loaded.ytdlp_enabled)
        self.assertEqual(loaded.ytdlp_path, "/usr/local/bin/yt-dlp")
        self.assertEqual(loaded.ytdlp_ffmpeg_path, "/usr/local/bin/ffmpeg")
        self.assertEqual(loaded.ytdlp_default_format, "worst")
        self.assertFalse(loaded.ytdlp_prefer_mode_a)
        self.assertFalse(loaded.ytdlp_embed_thumbnail)
        self.assertTrue(loaded.ytdlp_embed_subtitles)
        self.assertEqual(loaded.ytdlp_subtitle_langs, "es")
        self.assertEqual(loaded.ytdlp_cookies_browser, "edge")
        self.assertEqual(loaded.ytdlp_extra_args, "--no-mtime")
        self.assertFalse(loaded.ytdlp_auto_detect_urls)
        self.assertEqual(loaded.ytdlp_last_save_path, "/downloads")
        self.assertEqual(loaded.ytdlp_last_format, "worst")

    def test_effective_subtitle_langs_parsing(self):
        cfg = ExternalToolsConfig(ytdlp_subtitle_langs="en, ja , ,es")
        self.assertEqual(cfg.get_effective_ytdlp_subtitle_langs(), ["en", "ja", "es"])

        fallback = ExternalToolsConfig(ytdlp_subtitle_langs="")
        self.assertEqual(fallback.get_effective_ytdlp_subtitle_langs(), ["en"])

    def test_effective_extra_args_parsing(self):
        self.assertEqual(ExternalToolsConfig(ytdlp_extra_args="").get_effective_ytdlp_extra_args(), [])
        args = ExternalToolsConfig(
            ytdlp_extra_args="--retries 5 --socket-timeout 20"
        ).get_effective_ytdlp_extra_args()
        self.assertEqual(args, ["--retries", "5", "--socket-timeout", "20"])

    def test_ytdlp_playlist_limit_default_and_serialization(self):
        cfg = ExternalToolsConfig()
        self.assertEqual(cfg.ytdlp_playlist_limit, 10)
        self.assertIn("ytdlp_playlist_limit", cfg.to_dict())

        cfg = ExternalToolsConfig(ytdlp_playlist_limit=42)
        self.assertEqual(cfg.to_dict()["ytdlp_playlist_limit"], 42)
        self.assertEqual(ExternalToolsConfig.from_dict(cfg.to_dict()).ytdlp_playlist_limit, 42)

        ini = str(Path(self.tmp_dir.name) / "pl_limit.ini")
        settings = QSettings(ini, QSettings.Format.IniFormat)
        cfg.save(settings)
        self.assertEqual(ExternalToolsConfig.load(settings).ytdlp_playlist_limit, 42)

    def test_ytdlp_playlist_limit_is_clamped(self):
        self.assertEqual(ExternalToolsConfig.from_dict({"ytdlp_playlist_limit": 0}).ytdlp_playlist_limit, 1)
        self.assertEqual(ExternalToolsConfig.from_dict({"ytdlp_playlist_limit": -5}).ytdlp_playlist_limit, 1)
        self.assertEqual(ExternalToolsConfig.from_dict({"ytdlp_playlist_limit": 9999}).ytdlp_playlist_limit, 500)
        self.assertEqual(ExternalToolsConfig.from_dict({"ytdlp_playlist_limit": None}).ytdlp_playlist_limit, 10)
        self.assertEqual(ExternalToolsConfig.from_dict({"ytdlp_playlist_limit": "bad"}).ytdlp_playlist_limit, 10)
        self.assertEqual(ExternalToolsConfig.from_dict({}).ytdlp_playlist_limit, 10)

    def test_effective_paths_use_existing_files_only(self):
        bin_dir = Path(self.tmp_dir.name)
        fake_ytdlp = bin_dir / "yt-dlp"
        fake_ffmpeg = bin_dir / "ffmpeg"
        fake_ytdlp.write_text("#!/bin/sh\n", encoding="utf-8")
        fake_ffmpeg.write_text("#!/bin/sh\n", encoding="utf-8")

        cfg = ExternalToolsConfig(
            ytdlp_path=str(fake_ytdlp), ytdlp_ffmpeg_path=str(fake_ffmpeg)
        )
        with patch("shutil.which", return_value=None) as mock_which:
            self.assertEqual(
                os.path.normcase(cfg.get_effective_ytdlp_path()),
                os.path.normcase(normalize_path(str(fake_ytdlp))),
            )
            self.assertEqual(
                os.path.normcase(cfg.get_effective_ffmpeg_path()),
                os.path.normcase(normalize_path(str(fake_ffmpeg))),
            )
        mock_which.assert_not_called()
        # ^ an existing configured path is used verbatim; PATH is not consulted.

        # Nonexistent configured paths must not be returned. The old assertion
        # only checked the value differed from the configured one, which a real
        # globally-installed yt-dlp would also satisfy, so pin the exact fallback
        # and the name that is looked up.
        system_ytdlp = str(bin_dir / "system" / "yt-dlp")
        system_ffmpeg = str(bin_dir / "system" / "ffmpeg")
        on_path = {"yt-dlp": system_ytdlp, "ffmpeg": system_ffmpeg}

        missing = ExternalToolsConfig(
            ytdlp_path=str(bin_dir / "does-not-exist"),
            ytdlp_ffmpeg_path=str(bin_dir / "nope"),
        )
        with patch("shutil.which", side_effect=lambda name, *a, **k: on_path.get(name)) as mock_which:
            self.assertEqual(
                os.path.normcase(missing.get_effective_ytdlp_path()),
                os.path.normcase(system_ytdlp),
                "a missing configured yt-dlp must fall back to the PATH lookup",
            )
            self.assertEqual(
                os.path.normcase(missing.get_effective_ffmpeg_path()),
                os.path.normcase(system_ffmpeg),
                "a missing configured ffmpeg must fall back to the PATH lookup",
            )
            self.assertEqual(
                mock_which.call_args_list[0][0][0], "yt-dlp",
                "the POSIX name must be looked up first",
            )
            self.assertIn(
                call("ffmpeg"), mock_which.call_args_list,
                "ffmpeg must be looked up by its own name",
            )

        # With nothing configured and nothing on PATH, the resolver must say so
        # rather than inventing a path.
        with patch("shutil.which", return_value=None) as mock_which:
            self.assertEqual(
                missing.get_effective_ytdlp_path(), "",
                "no configured path and no PATH hit must resolve to an empty string",
            )
            self.assertEqual(
                missing.get_effective_ffmpeg_path(), "",
                "no configured path and no PATH hit must resolve to an empty string",
            )
            self.assertIn(
                call("yt-dlp.exe"), mock_which.call_args_list,
                "the Windows executable name must be tried as a second chance",
            )


if __name__ == "__main__":
    unittest.main()


