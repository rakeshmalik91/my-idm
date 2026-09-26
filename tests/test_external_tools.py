"""Unit tests for External Tools (AnimePahe scraper integration, CLI/GUI launch, and logs)."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication

from my_idm.config import ExternalToolsConfig
from my_idm.database import Database
from my_idm.external_tools import (
    launch_animepahe_cli,
    launch_animepahe_gui,
    open_file_in_default_app,
    show_in_folder,
)
from my_idm.manager import DownloadManager

app = QApplication.instance() or QApplication(sys.argv)


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
            self.assertTrue(ok)
            self.assertTrue(target_file.is_file())
            mock_open.assert_called_once()

    def test_open_folder_in_default_app(self):
        target_folder = Path(self.tmp_dir.name) / "subfolder"
        target_folder.mkdir(parents=True, exist_ok=True)

        with patch("os.startfile", return_value=None) as mock_startfile:
            ok, msg = open_file_in_default_app(target_folder)
            self.assertTrue(ok)
            self.assertIn("subfolder", msg)
            if sys.platform == "win32":
                mock_startfile.assert_called_once()

    def test_show_in_folder(self):
        folder = Path(self.tmp_dir.name) / "ext_test"
        folder.mkdir(parents=True, exist_ok=True)
        sample_file = folder / "my-idm-firefox.xpi"
        sample_file.write_text("dummy")

        # Test highlighting file
        with patch("subprocess.Popen") as mock_popen, patch("os.startfile") as mock_startfile:
            ok, msg = show_in_folder(sample_file)
            self.assertTrue(ok)
            if sys.platform == "win32":
                mock_popen.assert_called_once()

        # Test opening folder
        with patch("os.startfile") as mock_startfile:
            ok, msg = show_in_folder(folder)
            self.assertTrue(ok)
            if sys.platform == "win32":
                mock_startfile.assert_called_once()

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

    def test_settings_dialog_animepahe_url_download(self):
        from my_idm.settings_dialog import SettingsDialog
        from PySide6.QtWidgets import QMessageBox

        cfg = ExternalToolsConfig(
            animepahe_repo_path=str(self.repo_dir),
            animepahe_last_url="https://animepahe.ru/anime/test",
            animepahe_last_episodes="1-3",
        )
        dialog = SettingsDialog(external_tools_config=cfg, initial_tab=5)

        # Verify fields populated
        self.assertEqual(dialog._animepahe_url_edit.text(), "https://animepahe.ru/anime/test")
        self.assertEqual(dialog._animepahe_episodes_edit.text(), "1-3")

        with patch.object(QMessageBox, "information") as mock_info, \
             patch("my_idm.settings_dialog.launch_animepahe_cli", return_value=(True, "Started", MagicMock())) as mock_launch:
            dialog._on_download_animepahe_url()
            mock_launch.assert_called_once()
            kwargs = mock_launch.call_args.kwargs
            self.assertEqual(kwargs.get("url"), "https://animepahe.ru/anime/test")
            self.assertEqual(kwargs.get("episodes"), "1-3")
            mock_info.assert_called_once()

        dialog.close()


class TestManagerExternalToolsLifecycle(unittest.TestCase):
    """Test DownloadManager startup launch and termination of external tools."""

    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.manager = DownloadManager(self.db)

    def tearDown(self):
        self.manager.stop()
        self.db.close()

    def test_startup_launches_animepahe_if_enabled(self):
        cfg = ExternalToolsConfig(
            animepahe_repo_path="/mock/repo",
            animepahe_launch_on_startup=True,
        )
        self.manager.set_external_tools_config(cfg)

        with patch.object(self.manager, "start_animepahe_scraper") as mock_start:
            # Re-run start check
            if self.manager._external_tools_config.animepahe_launch_on_startup:
                self.manager.start_animepahe_scraper()
            mock_start.assert_called_once()

    def test_stop_terminates_running_process(self):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Process is running
        self.manager._animepahe_process = mock_proc

        self.manager.stop()
        mock_proc.terminate.assert_called_once()
        self.assertIsNone(self.manager._animepahe_process)

    def test_start_and_stop_animepahe_scraper(self):
        import threading
        status_updates = []
        self.manager.animepahe_status_changed.connect(status_updates.append)

        stop_event = threading.Event()
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = lambda: 0 if stop_event.is_set() else None
        mock_proc.wait.side_effect = stop_event.wait
        mock_proc.terminate.side_effect = stop_event.set

        with patch("my_idm.manager.launch_animepahe_cli", return_value=(True, "Started PID: 1234", mock_proc)):
            ok, msg = self.manager.start_animepahe_scraper()
            self.assertTrue(ok)
            self.assertTrue(self.manager.is_animepahe_running())
            self.assertIn(True, status_updates)

            # Calling again while already running queues the task
            ok2, msg2 = self.manager.start_animepahe_scraper()
            self.assertTrue(ok2)
            self.assertIn("queued", msg2.lower())

            # Stop scraper
            ok_stop, msg_stop = self.manager.stop_animepahe_scraper()
            self.assertTrue(ok_stop)
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
            mock_launch.assert_called_once()
            kwargs = mock_launch.call_args.kwargs
            self.assertEqual(kwargs.get("url"), "https://animepahe.ru/anime/1234")
            self.assertEqual(kwargs.get("episodes"), "5-10")
            self.assertEqual(kwargs.get("quality"), "720p")
            self.assertEqual(kwargs.get("lang"), "en")

    def test_manager_animepahe_monitor_polls_backlog_on_completion(self):
        import time
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = 0

        with patch("my_idm.manager.launch_animepahe_cli", return_value=(True, "Started", mock_proc)), \
             patch.object(self.manager, "process_backlogs") as mock_poll:
            ok, msg = self.manager.start_animepahe_scraper()
            self.assertTrue(ok)
            # Give daemon thread a brief moment to run wait() and trigger process_backlogs()
            time.sleep(0.05)
            mock_poll.assert_called_once()
            self.assertFalse(self.manager.is_animepahe_running())

    def test_manager_animepahe_queue_sequential_execution(self):
        import time
        queue_events = []
        self.manager.animepahe_queue_changed.connect(queue_events.append)

        # Mock process 1
        proc1 = MagicMock()
        proc1.poll.return_value = None
        proc1_finished = False
        def proc1_wait():
            while not proc1_finished:
                time.sleep(0.01)
            return 0
        proc1.wait.side_effect = proc1_wait

        # Mock process 2
        proc2 = MagicMock()
        proc2.poll.return_value = None
        proc2_finished = False
        def proc2_wait():
            while not proc2_finished:
                time.sleep(0.01)
            return 0
        proc2.wait.side_effect = proc2_wait

        launched_args = []
        def mock_launch(cfg, **kwargs):
            launched_args.append(kwargs)
            if len(launched_args) == 1:
                return True, "Started PID: 101", proc1
            else:
                return True, "Started PID: 102", proc2

        with patch("my_idm.manager.launch_animepahe_cli", side_effect=mock_launch):
            # 1. Start task 1
            ok1, msg1 = self.manager.start_animepahe_scraper(url="https://animepahe.ru/anime/series1")
            self.assertTrue(ok1)
            self.assertEqual(len(launched_args), 1)
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

            # 3. Complete task 1 -> triggers execution of task 2
            proc1.poll.return_value = 0
            proc1_finished = True
            time.sleep(0.08)
            from PySide6.QtCore import QCoreApplication
            QCoreApplication.processEvents()

            self.assertEqual(len(launched_args), 2)
            self.assertEqual(launched_args[1].get("url"), "https://animepahe.ru/anime/series2")
            self.assertEqual(launched_args[1].get("episodes"), "1-5")
            self.assertEqual(self.manager.get_animepahe_queue_length(), 0)

            # 4. Complete task 2 -> finishes queue
            proc2.poll.return_value = 0
            proc2_finished = True
            time.sleep(0.08)
            QCoreApplication.processEvents()

            self.assertFalse(self.manager.is_animepahe_running())

    def test_manager_stop_animepahe_clears_queue(self):
        proc = MagicMock()
        proc.poll.return_value = None

        with patch("my_idm.manager.launch_animepahe_cli", return_value=(True, "Started", proc)):
            self.manager.start_animepahe_scraper(url="https://animepahe.ru/anime/1")
            self.manager.start_animepahe_scraper(url="https://animepahe.ru/anime/2")
            self.manager.start_animepahe_scraper(url="https://animepahe.ru/anime/3")
            self.assertEqual(self.manager.get_animepahe_queue_length(), 2)

            ok, msg = self.manager.stop_animepahe_scraper()
            self.assertTrue(ok)
            self.assertIn("cleared 2 queued tasks", msg)
            self.assertEqual(self.manager.get_animepahe_queue_length(), 0)
            self.assertFalse(self.manager.is_animepahe_running())
            proc.terminate.assert_called_once()


class TestSettingsDialogAnimePaheEnhancements(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.tmp_dir.name) / "animepahe"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "animepahe_download.py").write_text("# mock", encoding="utf-8")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_url_normalization_and_validation(self):
        from my_idm.settings_dialog import SettingsDialog
        from PySide6.QtWidgets import QMessageBox

        cfg = ExternalToolsConfig(animepahe_repo_path=str(self.repo_dir))
        dialog = SettingsDialog(external_tools_config=cfg, initial_tab=5)

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

        dialog.close()

    def test_settings_dialog_queues_download_when_scraper_active(self):
        from my_idm.settings_dialog import SettingsDialog
        from PySide6.QtWidgets import QMessageBox

        mock_mgr = MagicMock()
        mock_mgr.is_animepahe_running.return_value = True
        mock_mgr.get_animepahe_queue_length.return_value = 1
        mock_mgr.start_animepahe_scraper.return_value = (True, "AnimePahe task queued at position #2 for 'https://animepahe.ru/anime/5678'.")

        cfg = ExternalToolsConfig(animepahe_repo_path=str(self.repo_dir))
        dialog = SettingsDialog(external_tools_config=cfg, manager=mock_mgr, initial_tab=5)

        dialog._animepahe_url_edit.setText("https://animepahe.ru/anime/5678")
        dialog._animepahe_episodes_edit.setText("1-10")

        with patch.object(QMessageBox, "information") as mock_info:
            dialog._on_download_animepahe_url()
            mock_mgr.start_animepahe_scraper.assert_called_once_with(
                url="https://animepahe.ru/anime/5678",
                episodes="1-10",
                quality=None,
                lang=None,
            )
            mock_info.assert_called_once()
            self.assertEqual(mock_info.call_args[0][1], "AnimePahe Download Queued")

        dialog.close()


if __name__ == "__main__":
    unittest.main()


