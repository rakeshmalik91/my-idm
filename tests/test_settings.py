"""Unit tests for General preferences and Settings dialog."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from my_idm.config import GeneralConfig, DEFAULT_DOWNLOADS_DIR
from my_idm.database import Database
from my_idm.dialogs import AddDownloadDialog
from my_idm.manager import DownloadManager
from my_idm.network import NetworkConfig
from my_idm.security import SecurityConfig
from my_idm.settings_dialog import SettingsDialog

app = QApplication.instance() or QApplication(sys.argv)


class TestGeneralConfig(unittest.TestCase):
    """Test GeneralConfig persistence and logic."""

    def setUp(self):
        self.test_settings = QSettings("MyIDMTest", "My-IDMTest")
        self.test_settings.clear()

    def tearDown(self):
        self.test_settings.clear()

    def test_default_values(self):
        cfg = GeneralConfig()
        self.assertEqual(cfg.default_save_path, DEFAULT_DOWNLOADS_DIR)
        self.assertFalse(cfg.remember_last_save_path)
        self.assertEqual(cfg.default_segments, 8)
        self.assertEqual(cfg.max_concurrent_downloads, 3)
        self.assertEqual(cfg.max_retries, 5)
        self.assertEqual(cfg.retry_delay, 2.0)
        self.assertEqual(cfg.retry_backoff_factor, 2.0)
        self.assertEqual(cfg.retry_max_delay, 60.0)
        self.assertTrue(cfg.retry_exponential_backoff)
        self.assertTrue(cfg.auto_resume_startup)
        self.assertTrue(cfg.notify_on_completion)

    def test_get_retry_delay_exponential(self):
        cfg = GeneralConfig(
            retry_delay=2.0,
            retry_backoff_factor=2.0,
            retry_max_delay=30.0,
            retry_exponential_backoff=True,
        )
        self.assertEqual(cfg.get_retry_delay(0), 2.0)
        self.assertEqual(cfg.get_retry_delay(1), 4.0)
        self.assertEqual(cfg.get_retry_delay(2), 8.0)
        self.assertEqual(cfg.get_retry_delay(3), 16.0)
        # Cap at max delay 30.0
        self.assertEqual(cfg.get_retry_delay(4), 30.0)
        self.assertEqual(cfg.get_retry_delay(10), 30.0)

        # Linear delay when exponential backoff is disabled
        cfg.retry_exponential_backoff = False
        self.assertEqual(cfg.get_retry_delay(0), 2.0)
        self.assertEqual(cfg.get_retry_delay(3), 2.0)
        self.assertEqual(cfg.get_retry_delay(10), 2.0)

    def test_save_and_load(self):
        cfg = GeneralConfig(
            default_save_path="D:/Custom/Downloads",
            remember_last_save_path=False,
            last_save_path="D:/Other/Path",
            default_segments=16,
            max_concurrent_downloads=6,
            max_retries=3,
            retry_delay=4.5,
            retry_backoff_factor=3.0,
            retry_max_delay=120.0,
            retry_exponential_backoff=False,
            auto_resume_startup=False,
            notify_on_completion=False,
        )
        cfg.save(self.test_settings)

        loaded = GeneralConfig.load(self.test_settings)
        self.assertEqual(loaded.default_save_path, "D:/Custom/Downloads")
        self.assertFalse(loaded.remember_last_save_path)
        self.assertEqual(loaded.last_save_path, "D:/Other/Path")
        self.assertEqual(loaded.default_segments, 16)
        self.assertEqual(loaded.max_concurrent_downloads, 6)
        self.assertEqual(loaded.max_retries, 3)
        self.assertEqual(loaded.retry_delay, 4.5)
        self.assertEqual(loaded.retry_backoff_factor, 3.0)
        self.assertEqual(loaded.retry_max_delay, 120.0)
        self.assertFalse(loaded.retry_exponential_backoff)
        self.assertFalse(loaded.auto_resume_startup)
        self.assertFalse(loaded.notify_on_completion)

    def test_get_effective_save_path(self):
        with tempfile.TemporaryDirectory() as default_dir, tempfile.TemporaryDirectory() as last_dir:
            cfg = GeneralConfig(
                default_save_path=default_dir,
                remember_last_save_path=True,
                last_save_path=last_dir,
            )
            # When remember_last_save_path is True and last_save_path exists
            self.assertEqual(cfg.get_effective_save_path(), last_dir)

            # When remember_last_save_path is False
            cfg.remember_last_save_path = False
            self.assertEqual(cfg.get_effective_save_path(), default_dir)


class TestAddDownloadDialogSettings(unittest.TestCase):
    """Test that AddDownloadDialog uses and updates GeneralConfig."""

    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_prefill_and_set_as_default(self):
        with tempfile.TemporaryDirectory() as custom_dir:
            # Set a custom default save path
            cfg = GeneralConfig(default_save_path=custom_dir, remember_last_save_path=False)
            cfg.save()

            dlg = AddDownloadDialog(initial_url="https://example.com/file.zip")
            self.assertEqual(dlg._save_edit.text(), custom_dir)
            self.assertEqual(dlg._seg_spin.value(), 8)

            # Simulate user changing folder and checking "Set as default download folder"
            with tempfile.TemporaryDirectory() as new_default_dir:
                dlg._save_edit.setText(new_default_dir)
                dlg._set_as_default_cb.setChecked(True)
                dlg._accept()

                # Verify GeneralConfig was updated
                reloaded = GeneralConfig.load()
                self.assertEqual(reloaded.default_save_path, new_default_dir)


class TestSettingsDialog(unittest.TestCase):
    """Test Preferences and SettingsDialog functionality."""

    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_dialog_population_and_save(self):
        with tempfile.TemporaryDirectory() as custom_dir:
            gen_cfg = GeneralConfig(
                default_save_path=custom_dir,
                default_segments=12,
                max_concurrent_downloads=5,
            )
            net_cfg = NetworkConfig(proxy_enabled=True, proxy_port=9050)
            sec_cfg = SecurityConfig(warn_high_risk_extensions=False)

            dlg = SettingsDialog(
                general_config=gen_cfg,
                network_config=net_cfg,
                security_config=sec_cfg,
            )

            # Check populated fields
            self.assertEqual(dlg._save_path_edit.text(), custom_dir)
            self.assertEqual(dlg._segments_spin.value(), 12)
            self.assertEqual(dlg._concurrent_spin.value(), 5)
            self.assertTrue(dlg._proxy_enable_cb.isChecked())
            self.assertEqual(dlg._proxy_port_spin.value(), 9050)
            self.assertFalse(dlg._warn_ext_cb.isChecked())

            # Change a few settings
            with tempfile.TemporaryDirectory() as new_dir:
                dlg._save_path_edit.setText(new_dir)
                dlg._segments_spin.setValue(16)
                dlg._concurrent_spin.setValue(8)

                # Save
                dlg._on_save()

                # Verify result
                saved_gen = dlg.general_config
                self.assertEqual(saved_gen.default_save_path, new_dir)
                self.assertEqual(saved_gen.default_segments, 16)
                self.assertEqual(saved_gen.max_concurrent_downloads, 8)

                # Check persistence in QSettings
                persisted = GeneralConfig.load()
                self.assertEqual(persisted.default_save_path, new_dir)
                self.assertEqual(persisted.default_segments, 16)

    def test_initial_tab(self):
        dlg_net = SettingsDialog(initial_tab=1)
        self.assertEqual(dlg_net._tabs.currentIndex(), 1)

        dlg_sec = SettingsDialog(initial_tab=2)
        self.assertEqual(dlg_sec._tabs.currentIndex(), 2)

    def test_settings_dialog_threat_exclusions_list_and_scan_timing(self):
        sec_cfg = SecurityConfig(
            scan_timing="after_complete",
            ignored_threat_categories=["HackTool", "CrackTool"],
        )
        dlg = SettingsDialog(security_config=sec_cfg)
        self.assertTrue(dlg._timing_auto_rb.isChecked())
        self.assertEqual(dlg._threat_excl_list.count(), 2)

        # Switch to manual scan only
        dlg._timing_manual_rb.setChecked(True)

        # Add threat exclusions
        dlg._new_threat_excl_edit.setText("Win32/AutoKMS, PUA")
        dlg._on_add_threat_exclusion()
        self.assertEqual(dlg._threat_excl_list.count(), 4)

        # Remove item
        dlg._threat_excl_list.setCurrentRow(0)
        dlg._on_remove_threat_exclusion()
        self.assertEqual(dlg._threat_excl_list.count(), 3)

        # Reset defaults
        dlg._on_reset_threat_exclusions_defaults()
        self.assertGreaterEqual(dlg._threat_excl_list.count(), 3)
        items = [dlg._threat_excl_list.item(i).text() for i in range(dlg._threat_excl_list.count())]
        self.assertIn("HackTool", items)
        self.assertIn("CrackTool", items)
        self.assertIn("PUA", items)

        # Save and verify
        dlg._on_save()
        self.assertEqual(dlg.security_config.scan_timing, "manual_only")
        self.assertIn("HackTool", dlg.security_config.ignored_threat_categories)

    def test_settings_dialog_exponential_retry_controls(self):
        gen_cfg = GeneralConfig(
            max_retries=7,
            retry_delay=3.0,
            retry_backoff_factor=2.5,
            retry_max_delay=90.0,
            retry_exponential_backoff=True,
        )
        dlg = SettingsDialog(general_config=gen_cfg)
        self.assertEqual(dlg._retries_spin.value(), 7)
        self.assertTrue(dlg._retry_exp_cb.isChecked())
        self.assertEqual(dlg._retry_delay_spin.value(), 3.0)
        self.assertEqual(dlg._retry_factor_spin.value(), 2.5)
        self.assertEqual(dlg._retry_max_delay_spin.value(), 90)
        self.assertTrue(dlg._retry_factor_spin.isEnabled())

        # Uncheck exponential backoff
        dlg._retry_exp_cb.setChecked(False)
        self.assertFalse(dlg._retry_factor_spin.isEnabled())
        self.assertFalse(dlg._retry_max_delay_spin.isEnabled())

        # Modify values and save
        dlg._retries_spin.setValue(4)
        dlg._retry_delay_spin.setValue(5.0)
        dlg._on_save()

        saved = dlg.general_config
        self.assertEqual(saved.max_retries, 4)
        self.assertFalse(saved.retry_exponential_backoff)
        self.assertEqual(saved.retry_delay, 5.0)
        GeneralConfig().save()
        dlg.close()



    def test_preferences_window_width_and_db_persistence(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        db = Database(tmp_path)
        db.open()
        try:
            # First instance: should have increased default width 820 and minimum width 740
            dlg = SettingsDialog(db=db)
            self.assertGreaterEqual(dlg.minimumWidth(), 740)
            self.assertEqual(dlg.width(), 820)
            self.assertEqual(dlg.height(), 600)

            # Resize the dialog and simulate closing
            dlg.resize(960, 700)
            dlg.done(0)

            # Check persisted size in database
            saved_size = db.get_preferences_window_size()
            self.assertEqual(saved_size, {"width": 960, "height": 700})

            # New instance with same DB should restore resized dimensions
            dlg2 = SettingsDialog(db=db)
            self.assertEqual(dlg2.width(), 960)
            self.assertEqual(dlg2.height(), 700)
        finally:
            db.close()
            if tmp_path.exists():
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass


class TestManagerGeneralConfigIntegration(unittest.TestCase):
    """Test DownloadManager with GeneralConfig."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(Path(self.tmp.name))
        self.db.open()
        self.test_settings = QSettings("MyIDMTest", "My-IDMTest")
        self.test_settings.clear()

    def tearDown(self):
        self.test_settings.clear()
        self.db.close()
        if os.path.exists(self.tmp.name):
            try:
                os.remove(self.tmp.name)
            except Exception:
                pass

    def test_manager_preferences_window_size_persistence(self):
        mgr = DownloadManager(self.db)
        mgr.save_preferences_window_size(920, 650)
        size = mgr.get_preferences_window_size()
        self.assertEqual(size, {"width": 920, "height": 650})

    def test_manager_uses_configured_default_save_path(self):
        with tempfile.TemporaryDirectory() as custom_dir:
            mgr = DownloadManager(self.db)
            cfg = GeneralConfig(default_save_path=custom_dir, remember_last_save_path=False)
            mgr.set_general_config(cfg)

            did = mgr.add_download("https://example.com/testfile.bin")
            self.assertIsNotNone(did)

            entry = mgr.get_entry(did)
            self.assertIsNotNone(entry)
            from my_idm.utils import normalize_path
            self.assertEqual(entry.save_path, normalize_path(custom_dir))

    def test_general_config_backlog_locations_and_clear_settings(self):
        cfg = GeneralConfig(
            backlog_locations=["D:/CustomBacklog", "D:/AnotherBacklog/urls.txt"],
            clear_backlog_after_load=False,
        )
        self.assertEqual(len(cfg.get_effective_backlog_locations()), 2)
        cfg.save(self.test_settings)

        loaded = GeneralConfig.load(self.test_settings)
        self.assertEqual(loaded.backlog_locations, ["D:/CustomBacklog", "D:/AnotherBacklog/urls.txt"])
        self.assertFalse(loaded.clear_backlog_after_load)

    def test_general_config_effective_backlog_locations_defaults(self):
        cfg = GeneralConfig(backlog_locations=[])
        effective = cfg.get_effective_backlog_locations()
        # Should contain cwd, APP_DIR, and home
        self.assertGreaterEqual(len(effective), 2)
        from my_idm.utils import normalize_path
        self.assertIn(normalize_path(Path.cwd()), effective)

    def test_settings_dialog_backlog_ui_and_actions(self):
        from unittest.mock import patch
        cfg = GeneralConfig(
            backlog_locations=["D:/Folder1", "D:/Folder2/backlog.txt"],
            clear_backlog_after_load=True,
        )
        dlg = SettingsDialog(general_config=cfg)

        # Verify initial population
        self.assertEqual(dlg._backlog_list.count(), 2)
        self.assertTrue(dlg._clear_backlog_cb.isChecked())

        # Test Remove action
        dlg._backlog_list.setCurrentRow(0)
        dlg._on_remove_backlog_loc()
        self.assertEqual(dlg._backlog_list.count(), 1)

        # Test Add Folder
        with patch("PySide6.QtWidgets.QFileDialog.getExistingDirectory", return_value="D:/NewFolder"):
            dlg._on_add_backlog_folder()
        self.assertEqual(dlg._backlog_list.count(), 2)

        # Test Add File
        with patch("PySide6.QtWidgets.QFileDialog.getOpenFileName", return_value=("D:/NewFile.txt", "")):
            dlg._on_add_backlog_file()
        self.assertEqual(dlg._backlog_list.count(), 3)

        # Test Reset Defaults
        dlg._on_reset_backlog_defaults()
        self.assertGreaterEqual(dlg._backlog_list.count(), 2)

        # Test saving
        dlg._clear_backlog_cb.setChecked(False)
        dlg._backlog_poll_cb.setChecked(True)
        dlg._backlog_poll_spin.setValue(120)
        with patch("os.path.exists", return_value=True):
            dlg._on_save()
        self.assertFalse(dlg.general_config.clear_backlog_after_load)
        self.assertTrue(dlg.general_config.backlog_poll_enabled)
        self.assertEqual(dlg.general_config.backlog_poll_interval, 120)
        GeneralConfig().save()
        dlg.close()

    def test_settings_dialog_test_antivirus_scanner(self):
        from unittest.mock import patch, MagicMock
        dlg = SettingsDialog()

        # Test clean scan result
        with patch("my_idm.settings_dialog.scan_file", return_value=(True, "Clean test report")):
            with patch("PySide6.QtWidgets.QMessageBox.information") as mock_info:
                dlg._on_test_scanner()
                mock_info.assert_called_once()
                self.assertIn("Clean", mock_info.call_args[0][2])

        # Test threat detected / non-zero code result
        with patch("my_idm.settings_dialog.scan_file", return_value=(False, "Threat detected: EICAR")):
            with patch("PySide6.QtWidgets.QMessageBox.warning") as mock_warn:
                dlg._on_test_scanner()
                mock_warn.assert_called_once()
                self.assertIn("Threat detected", mock_warn.call_args[0][2])

        dlg.close()


if __name__ == "__main__":
    unittest.main()
