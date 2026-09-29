"""Unit tests for General preferences and Settings dialog."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from my_idm.config import GeneralConfig, TorrentConfig, ExternalToolsConfig, DEFAULT_DOWNLOADS_DIR
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
        self.assertTrue(cfg.enable_system_tray)
        self.assertTrue(cfg.minimize_to_tray)
        self.assertTrue(cfg.close_to_tray)
        self.assertFalse(cfg.start_minimized)

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
            enable_system_tray=False,
            minimize_to_tray=False,
            close_to_tray=False,
            start_minimized=True,
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
        self.assertFalse(loaded.enable_system_tray)
        self.assertFalse(loaded.minimize_to_tray)
        self.assertFalse(loaded.close_to_tray)
        self.assertTrue(loaded.start_minimized)

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
            self.assertEqual(dlg._save_edit.currentText(), custom_dir)
            self.assertEqual(dlg._seg_spin.value(), 8)

            # Simulate user changing folder and checking "Set as default download folder"
            with tempfile.TemporaryDirectory() as new_default_dir:
                dlg._save_edit.setEditText(new_default_dir)
                dlg._set_as_default_cb.setChecked(True)
                dlg._accept()

                # Verify GeneralConfig was updated
                reloaded = GeneralConfig.load()
                self.assertEqual(reloaded.default_save_path, new_default_dir)


class TestTorrentConfig(unittest.TestCase):
    """Test TorrentConfig persistence and speed limit calculations."""

    def test_default_values(self):
        cfg = TorrentConfig()
        self.assertTrue(cfg.seeding_after_complete)
        self.assertEqual(cfg.max_seeding_speed, 200)
        self.assertEqual(cfg.download_to_seeding_ratio, 10.0)
        self.assertEqual(cfg.seeding_time_limit_minutes, 240)
        self.assertEqual(cfg.metadata_fetch_timeout_days, 1)

    def test_effective_seeding_speed_limit(self):
        # 1. Unlimited by default
        cfg = TorrentConfig(max_seeding_speed=0, download_to_seeding_ratio=2.0)
        self.assertEqual(cfg.get_effective_seeding_speed_limit(0), 0)

        # 2. Fixed speed limit only (100 KB/s = 102400 B/s)
        cfg = TorrentConfig(max_seeding_speed=100, download_to_seeding_ratio=0.0)
        self.assertEqual(cfg.get_effective_seeding_speed_limit(0), 102400)

        # 3. Derived ratio only (download_limit = 1,000,000 B/s, ratio = 2.0 -> 500,000 B/s)
        cfg = TorrentConfig(max_seeding_speed=0, download_to_seeding_ratio=2.0)
        self.assertEqual(cfg.get_effective_seeding_speed_limit(1_000_000), 500_000)

        # 4. Both configured -> takes minimum (max_seeding_speed 100 KB/s vs derived 500 KB/s -> 102400 B/s)
        cfg = TorrentConfig(max_seeding_speed=100, download_to_seeding_ratio=2.0)
        self.assertEqual(cfg.get_effective_seeding_speed_limit(1_000_000), 102400)

        # 5. Both configured -> derived is lower (derived 200 KB/s vs max 500 KB/s -> 204800 B/s)
        cfg = TorrentConfig(max_seeding_speed=500, download_to_seeding_ratio=2.0)
        self.assertEqual(cfg.get_effective_seeding_speed_limit(409_600), 204800)

    def test_to_and_from_dict(self):
        cfg = TorrentConfig(
            seeding_after_complete=False,
            max_seeding_speed=128,
            download_to_seeding_ratio=1.5,
            metadata_fetch_timeout_days=3,
        )
        d = cfg.to_dict()
        self.assertEqual(d["seeding_after_complete"], False)
        self.assertEqual(d["max_seeding_speed"], 128)
        self.assertEqual(d["download_to_seeding_ratio"], 1.5)
        self.assertEqual(d["metadata_fetch_timeout_days"], 3)

        reconstructed = TorrentConfig.from_dict(d)
        self.assertEqual(reconstructed.seeding_after_complete, False)
        self.assertEqual(reconstructed.max_seeding_speed, 128)
        self.assertEqual(reconstructed.download_to_seeding_ratio, 1.5)
        self.assertEqual(reconstructed.metadata_fetch_timeout_days, 3)

    def test_save_and_load(self):
        cfg = TorrentConfig(
            seeding_after_complete=False,
            max_seeding_speed=300,
            download_to_seeding_ratio=2.5,
            metadata_fetch_timeout_days=7,
        )
        cfg.save()
        loaded = TorrentConfig.load()
        self.assertEqual(loaded.seeding_after_complete, False)
        self.assertEqual(loaded.max_seeding_speed, 300)
        self.assertEqual(loaded.download_to_seeding_ratio, 2.5)
        self.assertEqual(loaded.metadata_fetch_timeout_days, 7)
        # Restore default
        TorrentConfig().save()


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

    def test_system_tray_settings_ui(self):
        cfg = GeneralConfig(
            enable_system_tray=True,
            minimize_to_tray=True,
            close_to_tray=True,
            start_minimized=False,
        )
        dlg = SettingsDialog(general_config=cfg)
        self.assertTrue(dlg._enable_system_tray_cb.isChecked())
        self.assertTrue(dlg._minimize_to_tray_cb.isChecked())
        self.assertTrue(dlg._close_to_tray_cb.isChecked())
        self.assertFalse(dlg._start_minimized_cb.isChecked())

        # Modify values and test toggling enable checkbox
        dlg._enable_system_tray_cb.setChecked(False)
        dlg._on_system_tray_toggled(False)
        self.assertFalse(dlg._minimize_to_tray_cb.isEnabled())
        self.assertFalse(dlg._close_to_tray_cb.isEnabled())
        self.assertFalse(dlg._start_minimized_cb.isEnabled())

        dlg._enable_system_tray_cb.setChecked(True)
        dlg._on_system_tray_toggled(True)
        self.assertTrue(dlg._minimize_to_tray_cb.isEnabled())

        dlg._minimize_to_tray_cb.setChecked(False)
        dlg._start_minimized_cb.setChecked(True)
        dlg._on_save()

        saved = dlg.general_config
        self.assertTrue(saved.enable_system_tray)
        self.assertFalse(saved.minimize_to_tray)
        self.assertTrue(saved.close_to_tray)
        self.assertTrue(saved.start_minimized)
        dlg.close()

    def test_initial_tab(self):
        dlg_gen = SettingsDialog(initial_tab=0)
        self.assertEqual(dlg_gen._tabs.currentIndex(), 0)

        dlg_tor = SettingsDialog(initial_tab=1)
        self.assertEqual(dlg_tor._tabs.currentIndex(), 1)

        dlg_net = SettingsDialog(initial_tab=2)
        self.assertEqual(dlg_net._tabs.currentIndex(), 2)

        dlg_tor_net = SettingsDialog(initial_tab=3)
        self.assertEqual(dlg_tor_net._tabs.currentIndex(), 3)

        dlg_sec = SettingsDialog(initial_tab=4)
        self.assertEqual(dlg_sec._tabs.currentIndex(), 4)

    def test_torrent_tab_settings(self):
        tor_cfg = TorrentConfig(
            seeding_after_complete=True,
            max_seeding_speed=256,
            download_to_seeding_ratio=3.0,
            metadata_fetch_timeout_days=2,
        )
        dlg = SettingsDialog(torrent_config=tor_cfg)

        # Check populated values
        self.assertTrue(dlg._seeding_after_complete_cb.isChecked())
        self.assertEqual(dlg._max_seeding_speed_spin.value(), 256)
        self.assertEqual(dlg._seeding_ratio_spin.value(), 3.0)
        self.assertEqual(dlg._metadata_timeout_spin.value(), 2)

        # Modify values
        dlg._seeding_after_complete_cb.setChecked(False)
        dlg._max_seeding_speed_spin.setValue(1024)
        dlg._seeding_ratio_spin.setValue(1.5)
        dlg._metadata_timeout_spin.setValue(5)

        with tempfile.TemporaryDirectory() as td:
            dlg._save_path_edit.setText(td)
            dlg._on_save()

        # Verify saved values on dialog
        saved_tor = dlg.torrent_config
        self.assertFalse(saved_tor.seeding_after_complete)
        self.assertEqual(saved_tor.max_seeding_speed, 1024)
        self.assertEqual(saved_tor.download_to_seeding_ratio, 1.5)
        self.assertEqual(saved_tor.metadata_fetch_timeout_days, 5)

        # Verify persistence
        persisted = TorrentConfig.load()
        self.assertFalse(persisted.seeding_after_complete)
        self.assertEqual(persisted.max_seeding_speed, 1024)
        self.assertEqual(persisted.download_to_seeding_ratio, 1.5)
        self.assertEqual(persisted.metadata_fetch_timeout_days, 5)

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

    def test_torrent_seeding_config_load_and_save(self):
        """BitTorrent seeding duration, ratio limit, and startup resume load and save in SettingsDialog."""
        from unittest.mock import patch
        cfg = TorrentConfig(
            seeding_after_complete=True,
            seeding_time_limit_minutes=45,
            seeding_ratio_limit=2.5,
            resume_seeding_on_startup=False,
        )
        dlg = SettingsDialog(torrent_config=cfg)

        self.assertTrue(dlg._seeding_after_complete_cb.isChecked())
        self.assertFalse(dlg._resume_seeding_cb.isChecked())
        self.assertEqual(dlg._seeding_time_spin.value(), 45)
        self.assertEqual(dlg._seeding_ratio_limit_spin.value(), 2.5)

        # Modify values
        dlg._resume_seeding_cb.setChecked(True)
        dlg._seeding_time_spin.setValue(90)
        dlg._seeding_ratio_limit_spin.setValue(3.0)

        with patch("os.path.exists", return_value=True):
            dlg._on_save()

        self.assertTrue(dlg.torrent_config.resume_seeding_on_startup)
        self.assertEqual(dlg.torrent_config.seeding_time_limit_minutes, 90)
        self.assertEqual(dlg.torrent_config.seeding_ratio_limit, 3.0)
        dlg.close()


class TestExternalToolsSettings(unittest.TestCase):
    """Tests for ExternalToolsConfig and SettingsDialog External Tools tab."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.ini_path = Path(self.tmp_dir.name) / "test_settings.ini"
        self.settings = QSettings(str(self.ini_path), QSettings.Format.IniFormat)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_external_tools_config_defaults_and_save_load(self):
        cfg = ExternalToolsConfig(
            animepahe_repo_path="/custom/animepahe",
            animepahe_launch_on_startup=True,
        )
        cfg.save(self.settings)

        loaded = ExternalToolsConfig.load(self.settings)
        self.assertEqual(loaded.animepahe_repo_path, "/custom/animepahe")
        self.assertTrue(loaded.animepahe_launch_on_startup)

    def test_settings_dialog_external_tools_tab(self):
        cfg = ExternalToolsConfig(
            animepahe_repo_path="/path/to/repo",
            animepahe_launch_on_startup=False,
        )
        dlg = SettingsDialog(external_tools_config=cfg, initial_tab=5)
        self.assertEqual(dlg._tabs.currentIndex(), 5)
        self.assertEqual(dlg._animepahe_repo_edit.text(), "/path/to/repo")
        self.assertFalse(dlg._animepahe_startup_cb.isChecked())

        dlg._animepahe_repo_edit.setText("/new/path")
        dlg._animepahe_startup_cb.setChecked(True)

        with patch("os.path.exists", return_value=True):
            dlg._on_save()

        self.assertEqual(dlg.external_tools_config.animepahe_repo_path, "/new/path")
        self.assertTrue(dlg.external_tools_config.animepahe_launch_on_startup)
        dlg.close()

    def test_settings_dialog_run_cli_button(self):
        """SettingsDialog includes Run CLI button that starts/stops AnimePahe scraper.

        Success is deliberately silent - the button label and the footer badge
        already reflect the new state, so no dialog is raised. Only failures
        warn, and a missing repository warns without starting anything.
        """
        from unittest.mock import MagicMock
        mock_mgr = MagicMock()
        mock_mgr.is_animepahe_running.return_value = False
        mock_mgr.start_animepahe_scraper.return_value = (True, "Started PID: 1234")
        mock_mgr.stop_animepahe_scraper.return_value = (True, "Stopped scraper")

        cfg = ExternalToolsConfig(animepahe_repo_path="/valid/path")
        dlg = SettingsDialog(external_tools_config=cfg, initial_tab=5, manager=mock_mgr)
        self.assertIn("Run CLI Now", dlg._btn_run_cli_now.text())

        with patch("os.path.isdir", return_value=True), \
             patch("PySide6.QtWidgets.QMessageBox.information") as mock_info, \
             patch("PySide6.QtWidgets.QMessageBox.warning") as mock_warn:
            dlg._on_run_animepahe_cli_from_settings()
            mock_mgr.start_animepahe_scraper.assert_called_once()
            # Silent on success: state is conveyed by the button/footer badge.
            mock_info.assert_not_called()
            mock_warn.assert_not_called()

            # Simulate scraper now running
            mock_mgr.is_animepahe_running.return_value = True
            dlg._on_animepahe_status_changed(True)
            self.assertIn("Stop CLI Scraper", dlg._btn_run_cli_now.text())

            # Clicking again stops scraper
            dlg._on_run_animepahe_cli_from_settings()
            mock_mgr.stop_animepahe_scraper.assert_called_once()
            mock_info.assert_not_called()
            mock_warn.assert_not_called()

            # A failure does warn, and the message is surfaced.
            mock_mgr.is_animepahe_running.return_value = False
            mock_mgr.start_animepahe_scraper.return_value = (False, "tor.exe not found")
            dlg._on_run_animepahe_cli_from_settings()
            mock_warn.assert_called_once()
            self.assertIn("tor.exe not found", mock_warn.call_args[0][2])

        dlg.close()

    def test_settings_dialog_run_cli_missing_repo_warns_without_starting(self):
        """A missing repository is reported and the scraper is never started."""
        from unittest.mock import MagicMock
        mock_mgr = MagicMock()
        mock_mgr.is_animepahe_running.return_value = False

        cfg = ExternalToolsConfig(animepahe_repo_path="")
        dlg = SettingsDialog(external_tools_config=cfg, initial_tab=5, manager=mock_mgr)

        with patch("os.path.isdir", return_value=False), \
             patch("PySide6.QtWidgets.QMessageBox.warning") as mock_warn:
            dlg._on_run_animepahe_cli_from_settings()
            mock_warn.assert_called_once()
            self.assertEqual(mock_warn.call_args[0][1], "Repository Not Found")
            mock_mgr.start_animepahe_scraper.assert_not_called()

        dlg.close()


if __name__ == "__main__":
    unittest.main()
