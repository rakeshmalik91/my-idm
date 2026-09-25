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

            # Calling again while already running
            ok2, msg2 = self.manager.start_animepahe_scraper()
            self.assertTrue(ok2)
            self.assertIn("already running", msg2)

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


if __name__ == "__main__":
    unittest.main()
