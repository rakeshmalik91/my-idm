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
        self.assertTrue(cfg.auto_resume_startup)
        self.assertTrue(cfg.notify_on_completion)

    def test_save_and_load(self):
        cfg = GeneralConfig(
            default_save_path="D:/Custom/Downloads",
            remember_last_save_path=False,
            last_save_path="D:/Other/Path",
            default_segments=16,
            max_concurrent_downloads=6,
            max_retries=3,
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
        QSettings("MyIDM", "My-IDM").clear()

    def tearDown(self):
        QSettings("MyIDM", "My-IDM").clear()

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
        QSettings("MyIDM", "My-IDM").clear()

    def tearDown(self):
        QSettings("MyIDM", "My-IDM").clear()

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
        QSettings("MyIDM", "My-IDM").clear()

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.tmp.name):
            try:
                os.remove(self.tmp.name)
            except Exception:
                pass
        QSettings("MyIDM", "My-IDM").clear()

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
            self.assertEqual(entry.save_path, custom_dir)


if __name__ == "__main__":
    unittest.main()
