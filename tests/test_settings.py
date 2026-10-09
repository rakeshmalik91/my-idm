"""Unit tests for General preferences and Settings dialog."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from my_idm.config import (
    GeneralConfig,
    TorrentConfig,
    ExternalToolsConfig,
    DEFAULT_DOWNLOADS_DIR,
    DEFAULT_SEGMENT_START_DELAY_MS,
    MAX_SEGMENT_START_DELAY_MS,
    clamp_segment_start_delay,
)
from my_idm.database import Database
from my_idm.dialogs import AddDownloadDialog
from my_idm.manager import DownloadManager
from my_idm.network import NetworkConfig
from my_idm.security import SecurityConfig
from my_idm.settings_dialog import (
    TAB_APP,
    TAB_BROWSER,
    TAB_CLIPBOARD,
    TAB_EXTERNAL_TOOLS,
    TAB_GENERAL,
    TAB_ORDER,
    TAB_SECURITY,
    TAB_TOR,
    TAB_TORRENT,
    TAB_TITLES,
    TAB_VIEWS,
    TAB_VPN,
    TAB_YOUTUBE,
    TAB_BANDWIDTH,
    TAB_SCHEDULER,
    SettingsDialog,
    tab_index,
)

app = QApplication.instance() or QApplication(sys.argv)

# Every config group that production persists. The autouse guard below snapshots
# and restores all of them so a test can never leave a poisoned value (e.g. a
# `default_save_path` pointing into an already-deleted TemporaryDirectory) for
# the rest of the session.
_CONFIG_GROUPS = (
    "General",
    "Torrent",
    "Tor",
    "ExternalTools",
    "BrowserIntegration",
    "Network",
    "Security",
    "Scheduler",
)


class ConfigIsolationMixin:
    """Snapshot/restore the whole QSettings tree around every test.

    Replaces the previous ad-hoc ``GeneralConfig().save()`` "restore default"
    calls, which were inconsistent, ran only on the success path, and left a
    *stale* value (not the default) behind -- for example a
    ``default_save_path`` naming a directory that no longer exists.
    """

    def setUp(self):
        super().setUp()
        # Snapshot BOTH the production tree and the dedicated test-org tree in
        # full, so a test cannot leak into either. allKeys() covers every group
        # (including any group a test may introduce), which is why the explicit
        # _CONFIG_GROUPS list is only used to prove coverage in
        # test_qsettings_are_redirected_away_from_the_registry.
        settings = QSettings("MyIDM", "My-IDM")
        test_settings = QSettings("MyIDMTest", "My-IDMTest")
        self.addCleanup(
            self._restore_settings,
            settings, self._snapshot(settings),
            test_settings, self._snapshot(test_settings),
        )

    @staticmethod
    def _snapshot(settings):
        return {key: settings.value(key) for key in settings.allKeys()}

    @staticmethod
    def _unlink_strict(path: Path):
        """Remove a sqlite file, surfacing a genuinely stuck handle.

        The old code used ``except Exception: pass``, which hid a leaked
        connection behind a silently orphaned ``.db`` file in %TEMP% on Windows.
        """
        for suffix in ("", "-wal", "-shm"):
            target = Path(str(path) + suffix)
            if not target.exists():
                continue
            try:
                os.remove(target)
            except PermissionError as exc:
                raise AssertionError(
                    f"{target} could not be removed; a sqlite handle is still open"
                ) from exc
        if path.exists():  # pragma: no cover - defensive
            raise AssertionError(f"{path} still exists after removal")

    @staticmethod
    def _restore_settings(settings, snapshot, test_settings, test_snapshot):
        settings.clear()
        for group, values in snapshot.items():
            for key, value in values.items():
                settings.setValue(f"{group}/{key}", value)
        settings.sync()
        test_settings.clear()
        for key, value in test_snapshot.items():
            test_settings.setValue(key, value)
        test_settings.sync()


class TestGeneralConfig(ConfigIsolationMixin, unittest.TestCase):
    """Test GeneralConfig persistence and logic."""

    def setUp(self):
        super().setUp()
        self.test_settings = QSettings("MyIDMTest", "My-IDMTest")
        self.test_settings.clear()

    def test_default_values(self):
        cfg = GeneralConfig()
        self.assertEqual(cfg.default_save_path, DEFAULT_DOWNLOADS_DIR)
        self.assertFalse(cfg.remember_last_save_path)
        self.assertEqual(cfg.default_segments, 8)
        self.assertEqual(cfg.segment_start_delay_ms, DEFAULT_SEGMENT_START_DELAY_MS)
        self.assertEqual(cfg.max_concurrent_downloads, 3)
        self.assertEqual(cfg.max_retries, 5)
        self.assertEqual(cfg.retry_delay, 30.0)
        self.assertEqual(cfg.retry_backoff_factor, 2.0)
        self.assertEqual(cfg.retry_max_delay, 600.0)
        self.assertTrue(cfg.retry_exponential_backoff)
        self.assertTrue(cfg.auto_resume_startup)
        self.assertTrue(cfg.notify_on_completion)
        self.assertTrue(cfg.enable_system_tray)
        self.assertTrue(cfg.minimize_to_tray)
        self.assertTrue(cfg.close_to_tray)
        self.assertFalse(cfg.start_minimized)
        self.assertEqual(cfg.clipboard_min_file_size_kb, 1024)
        self.assertEqual(
            cfg.clipboard_ignored_extensions,
            [".txt", ".htm", ".html", ".jpg", ".jpeg", ".png", ".gif", ".webp"],
        )

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
            clipboard_monitor_enabled=True,
            clipboard_monitor_max_urls=50,
            clipboard_min_file_size_kb=2048,
            clipboard_ignored_extensions=[".pdf", ".docx"],
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
        self.assertTrue(loaded.clipboard_monitor_enabled)
        self.assertEqual(loaded.clipboard_monitor_max_urls, 50)
        self.assertEqual(loaded.clipboard_min_file_size_kb, 2048)
        self.assertEqual(loaded.clipboard_ignored_extensions, [".pdf", ".docx"])

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

    def test_segment_start_delay_round_trips(self):
        cfg = GeneralConfig(segment_start_delay_ms=250)
        cfg.save(self.test_settings)
        self.assertEqual(GeneralConfig.load(self.test_settings).segment_start_delay_ms, 250)
        self.assertEqual(
            GeneralConfig.from_dict(cfg.to_dict()).segment_start_delay_ms, 250
        )

    def test_segment_start_delay_is_clamped_on_the_way_in(self):
        self.assertEqual(clamp_segment_start_delay(-5), 0)
        self.assertEqual(clamp_segment_start_delay(10**6), MAX_SEGMENT_START_DELAY_MS)
        self.assertEqual(
            clamp_segment_start_delay("nonsense"), DEFAULT_SEGMENT_START_DELAY_MS,
            "a corrupt value must not silently disable a stagger the user asked for",
        )
        self.assertEqual(
            GeneralConfig.from_dict({"segment_start_delay_ms": -1}).segment_start_delay_ms, 0
        )
        self.test_settings.setValue("General/segment_start_delay_ms", 10**6)
        self.test_settings.sync()
        self.assertEqual(
            GeneralConfig.load(self.test_settings).segment_start_delay_ms,
            MAX_SEGMENT_START_DELAY_MS,
        )


class TestAddDownloadDialogSettings(ConfigIsolationMixin, unittest.TestCase):
    """Test that AddDownloadDialog uses and updates GeneralConfig."""

    def test_prefill_and_set_as_default(self):
        with tempfile.TemporaryDirectory() as custom_dir:
            # Set a custom default save path
            cfg = GeneralConfig(default_save_path=custom_dir, remember_last_save_path=False)
            cfg.save()

            dlg = AddDownloadDialog(initial_url="https://example.com/file.zip")
            self.addCleanup(dlg.close)
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

    def test_autouse_guard_restores_a_deleted_temp_save_path(self):
        """The file-wide guard must undo a persisted path to a deleted temp dir.

        Previously each test restored "defaults" by hand (``GeneralConfig().save()``)
        and several paths were simply left behind, so later tests in the same
        process resolved a save path that no longer existed.
        """
        with tempfile.TemporaryDirectory() as doomed:
            GeneralConfig(default_save_path=doomed, remember_last_save_path=False).save()
            self.assertEqual(GeneralConfig.load().default_save_path, doomed)

        # Run the registered cleanup exactly as the runner would, then re-check.
        self.doCleanups()
        after = GeneralConfig.load()
        self.assertNotEqual(
            after.default_save_path, doomed,
            "the guard must not leave a path that points at a deleted directory",
        )
        self.assertEqual(
            after.default_save_path, DEFAULT_DOWNLOADS_DIR,
            "with nothing saved beforehand the guard restores the pristine default",
        )

    def test_qsettings_are_redirected_away_from_the_registry(self):
        """Both QSettings trees must be backed by temp INI files, not the registry.

        On Windows a native-format QSettings writes to
        HKCU\\Software\\MyIDM\\... , so an unredirected test would mutate the real
        user preferences. The conftest patches QSettings.__init__ for the whole
        session; this pins the effect for the two org/app pairs this file uses.
        """
        for org, app_name in (("MyIDM", "My-IDM"), ("MyIDMTest", "My-IDMTest")):
            with self.subTest(app=app_name):
                s = QSettings(org, app_name)
                self.assertEqual(
                    s.format(), QSettings.Format.IniFormat,
                    f"{org}/{app_name} must not use the native (registry) backend",
                )
                filename = s.fileName()
                self.assertTrue(
                    filename.lower().endswith(".ini"),
                    f"{org}/{app_name} must live in an .ini file, got {filename}",
                )
                self.assertNotIn(
                    "\\Software\\", filename,
                    "the settings file must not be a registry hive path",
                )
                self.assertFalse(
                    Path(filename).exists(),
                    f"{filename} must not pre-exist; the conftest gives each test "
                    "a fresh temp directory",
                )
                s.setValue("__probe__", "1")
                self.assertEqual(s.value("__probe__"), "1")
                s.sync()
                self.assertTrue(
                    Path(filename).exists(),
                    "writing must materialise the file inside the temp directory",
                )
                s.remove("__probe__")
                s.sync()

    def test_every_production_config_group_round_trips_through_the_tree(self):
        """Proves the guard covers every group that actually persists values."""
        settings = QSettings("MyIDM", "My-IDM")
        for group in _CONFIG_GROUPS:
            with self.subTest(group=group):
                settings.setValue(f"{group}/__probe__", "x")
        written = {key for key in settings.allKeys() if key.endswith("__probe__")}
        self.assertEqual(
            sorted(written),
            sorted(f"{group}/__probe__" for group in _CONFIG_GROUPS),
            "every declared group must be writable, so the snapshot really covers it",
        )
        # Restoring must remove them again, proving the guard is not a no-op.
        self.doCleanups()
        after = QSettings("MyIDM", "My-IDM")
        self.assertEqual(
            [key for key in after.allKeys() if key.endswith("__probe__")], [],
            "the guard must undo anything a test wrote",
        )


class TestTorrentConfig(ConfigIsolationMixin, unittest.TestCase):
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
        # Restoring the pristine state is the autouse guard's job, not this
        # test's; previously a bare `TorrentConfig().save()` ran only on the
        # success path, so a failing assert left the poisoned values behind.
        self.assertEqual(
            TorrentConfig().max_seeding_speed, 200,
            "the default config is the pristine baseline the guard restores to",
        )

    def test_torrent_file_integration_defaults_are_off(self):
        # Each of these acts on the machine outside the app window — registering a handler,
        # watching a folder — so nothing is opted into until it is asked for.
        cfg = TorrentConfig()
        self.assertFalse(cfg.associate_torrent_files)
        self.assertFalse(cfg.watch_torrent_folder)
        self.assertFalse(cfg.clean_watched_torrent_files)
        self.assertEqual(cfg.torrent_watch_max_age_days, 3)
        # Empty means "the effective default download folder", resolved at use time. Baking a
        # path in here would freeze whatever the download folder was when the preference was
        # first saved and never follow a later change.
        self.assertEqual(cfg.torrent_watch_folder, "")

    def test_torrent_file_integration_round_trips(self):
        cfg = TorrentConfig(
            associate_torrent_files=True,
            watch_torrent_folder=True,
            torrent_watch_folder="/torrents/in",
            clean_watched_torrent_files=True,
            torrent_watch_max_age_days=7,
        )
        cfg.save()
        loaded = TorrentConfig.load()
        self.assertTrue(loaded.associate_torrent_files)
        self.assertTrue(loaded.watch_torrent_folder)
        self.assertEqual(loaded.torrent_watch_folder, "/torrents/in")
        self.assertTrue(loaded.clean_watched_torrent_files)
        self.assertEqual(loaded.torrent_watch_max_age_days, 7)

    def test_torrent_file_integration_survives_a_dict_round_trip(self):
        cfg = TorrentConfig(
            associate_torrent_files=True,
            watch_torrent_folder=True,
            torrent_watch_folder="/torrents/in",
            clean_watched_torrent_files=True,
            torrent_watch_max_age_days=7,
        )
        reconstructed = TorrentConfig.from_dict(cfg.to_dict())
        self.assertTrue(reconstructed.associate_torrent_files)
        self.assertTrue(reconstructed.watch_torrent_folder)
        self.assertEqual(reconstructed.torrent_watch_folder, "/torrents/in")
        self.assertTrue(reconstructed.clean_watched_torrent_files)
        self.assertEqual(reconstructed.torrent_watch_max_age_days, 7)

    def test_an_older_settings_file_without_the_new_keys_loads_defaults(self):
        # A user upgrading from a build that predates these settings must not get a crash or a
        # silently-enabled watcher on their downloads folder.
        loaded = TorrentConfig.from_dict({"max_seeding_speed": 128})
        self.assertFalse(loaded.associate_torrent_files)
        self.assertFalse(loaded.watch_torrent_folder)
        self.assertFalse(loaded.clean_watched_torrent_files)
        self.assertEqual(loaded.torrent_watch_max_age_days, 3)
        self.assertEqual(loaded.torrent_watch_folder, "")
        self.assertEqual(loaded.max_seeding_speed, 128)


class TestSettingsDialog(ConfigIsolationMixin, unittest.TestCase):
    """Test Preferences and SettingsDialog functionality."""

    def test_dialog_population_and_save(self):
        with tempfile.TemporaryDirectory() as custom_dir:
            gen_cfg = GeneralConfig(
                default_save_path=custom_dir,
                default_segments=12,
                max_concurrent_downloads=5,
                segment_start_delay_ms=150,
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
            self.assertEqual(dlg._segment_stagger_spin.value(), 150)
            self.assertEqual(dlg._concurrent_spin.value(), 5)
            self.assertTrue(dlg._proxy_enable_cb.isChecked())
            self.assertEqual(dlg._proxy_port_spin.value(), 9050)
            self.assertFalse(dlg._warn_ext_cb.isChecked())

            # Change a few settings
            with tempfile.TemporaryDirectory() as new_dir:
                dlg._save_path_edit.setText(new_dir)
                dlg._segments_spin.setValue(16)
                dlg._segment_stagger_spin.setValue(75)
                dlg._concurrent_spin.setValue(8)

                # Save
                dlg._on_save()

                # Verify result
                saved_gen = dlg.general_config
                self.assertEqual(saved_gen.default_save_path, new_dir)
                self.assertEqual(saved_gen.default_segments, 16)
                self.assertEqual(saved_gen.segment_start_delay_ms, 75)
                self.assertEqual(saved_gen.max_concurrent_downloads, 8)

                # Check persistence in QSettings
                persisted = GeneralConfig.load()
                self.assertEqual(persisted.default_save_path, new_dir)
                self.assertEqual(persisted.default_segments, 16)
                self.assertEqual(persisted.segment_start_delay_ms, 75)

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

    def test_clipboard_tab_settings_ui(self):
        cfg = GeneralConfig(
            clipboard_monitor_enabled=True,
            clipboard_monitor_max_urls=25,
            clipboard_min_file_size_kb=2048,
            clipboard_ignored_extensions=[".txt", ".htm", ".html", ".jpg", ".jpeg"],
        )
        dlg = SettingsDialog(general_config=cfg)
        self.assertTrue(dlg._clipboard_monitor_cb.isChecked())
        self.assertEqual(dlg._clipboard_max_urls_spin.value(), 25)
        self.assertEqual(dlg._clipboard_min_size_spin.value(), 2048)
        self.assertEqual(dlg._clipboard_ignored_exts_edit.text(), ".txt, .htm, .html, .jpg, .jpeg")

        # Disable monitor and verify controls are disabled
        dlg._clipboard_monitor_cb.setChecked(False)
        self.assertFalse(dlg._clipboard_max_urls_spin.isEnabled())
        self.assertFalse(dlg._clipboard_min_size_spin.isEnabled())
        self.assertFalse(dlg._clipboard_ignored_exts_edit.isEnabled())

        # Re-enable and modify values
        dlg._clipboard_monitor_cb.setChecked(True)
        self.assertTrue(dlg._clipboard_max_urls_spin.isEnabled())
        self.assertTrue(dlg._clipboard_min_size_spin.isEnabled())
        self.assertTrue(dlg._clipboard_ignored_exts_edit.isEnabled())

        dlg._clipboard_max_urls_spin.setValue(50)
        dlg._clipboard_min_size_spin.setValue(0)
        dlg._clipboard_ignored_exts_edit.setText(".zip, rar, 7z")
        dlg._on_save()

        saved = dlg.general_config
        self.assertTrue(saved.clipboard_monitor_enabled)
        self.assertEqual(saved.clipboard_monitor_max_urls, 50)
        self.assertEqual(saved.clipboard_min_file_size_kb, 0)
        self.assertEqual(saved.clipboard_ignored_extensions, [".zip", ".rar", ".7z"])

        persisted = GeneralConfig.load()
        self.assertTrue(persisted.clipboard_monitor_enabled)
        self.assertEqual(persisted.clipboard_monitor_max_urls, 50)
        self.assertEqual(persisted.clipboard_min_file_size_kb, 0)
        self.assertEqual(persisted.clipboard_ignored_extensions, [".zip", ".rar", ".7z"])
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
        dlg.close()

    def test_settings_dialog_retry_reset_to_defaults(self):
        gen_cfg = GeneralConfig(
            max_retries=15,
            retry_delay=5.0,
            retry_backoff_factor=3.0,
            retry_max_delay=120.0,
            retry_exponential_backoff=False,
        )
        dlg = SettingsDialog(general_config=gen_cfg)
        self.assertEqual(dlg._retries_spin.value(), 15)
        self.assertFalse(dlg._retry_exp_cb.isChecked())
        self.assertEqual(dlg._retry_delay_spin.value(), 5.0)

        # Click Reset to Default button
        dlg._retry_reset_btn.click()

        # Check all values are back to defaults
        self.assertEqual(dlg._retries_spin.value(), 5)
        self.assertTrue(dlg._retry_exp_cb.isChecked())
        self.assertEqual(dlg._retry_delay_spin.value(), 30.0)
        self.assertEqual(dlg._retry_factor_spin.value(), 2.0)
        self.assertEqual(dlg._retry_max_delay_spin.value(), 600)
        self.assertTrue(dlg._retry_factor_spin.isEnabled())
        self.assertTrue(dlg._retry_max_delay_spin.isEnabled())

        # Save and verify GeneralConfig received default retry settings
        dlg._on_save()
        saved = dlg.general_config
        self.assertEqual(saved.max_retries, 5)
        self.assertTrue(saved.retry_exponential_backoff)
        self.assertEqual(saved.retry_delay, 30.0)
        self.assertEqual(saved.retry_backoff_factor, 2.0)
        self.assertEqual(saved.retry_max_delay, 600.0)
        dlg.close()

    def test_preferences_window_width_and_db_persistence(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        tmp_path = Path(tmp_dir.name) / "prefs.db"
        # Registered BEFORE db.close so it runs AFTER it (addCleanup is LIFO):
        # that is the only order in which the handle is actually released. The
        # old `except Exception: pass` hid a leaked connection behind a
        # silently orphaned .db file in %TEMP%.
        self.addCleanup(self._unlink_strict, tmp_path)
        db = Database(tmp_path)
        db.open()
        self.addCleanup(db.close)

        # First instance: should have increased default width 820 and minimum width 740
        dlg = SettingsDialog(db=db)
        self.addCleanup(dlg.close)
        self.assertGreaterEqual(dlg.minimumWidth(), 740)
        # Queue Manager tab has wider content (table with queue columns), so default is ~874
        self.assertGreaterEqual(dlg.width(), 820)
        # 668, not 600: the Views page is the tallest and has to fit without the dialog
        # scrolling at its own minimum size. `resize(820, 600)` is clamped up to it.
        self.assertEqual(dlg.height(), 668)
        self.assertEqual(dlg.height(), dlg.minimumHeight())

        # Resize the dialog and simulate closing
        dlg.resize(960, 700)
        dlg.done(0)

        # Check persisted size in database
        saved_size = db.get_preferences_window_size()
        self.assertEqual(saved_size, {"width": 960, "height": 700})

        # New instance with same DB should restore resized dimensions
        dlg2 = SettingsDialog(db=db)
        self.addCleanup(dlg2.close)
        self.assertEqual(dlg2.width(), 960)
        self.assertEqual(dlg2.height(), 700)

    def test_settings_dialog_search_input_and_escape(self):
        from PySide6.QtCore import Qt, QEvent
        from PySide6.QtGui import QKeyEvent

        dlg = SettingsDialog()
        self.addCleanup(dlg.close)
        self.assertTrue(hasattr(dlg, "_search_input"))
        self.assertTrue(dlg._search_input.isClearButtonEnabled())
        self.assertIn("Ctrl+F", dlg._search_input.placeholderText())

        # Test Esc key clears non-empty input
        dlg._search_input.setText("bittorrent")
        self.assertEqual(dlg._search_input.text(), "bittorrent")
        esc_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        consumed = dlg.eventFilter(dlg._search_input, esc_event)
        self.assertTrue(consumed)
        self.assertEqual(dlg._search_input.text(), "")

    def test_settings_dialog_search_filtering_and_restore(self):
        from my_idm.settings_dialog import TAB_VIEWS, TAB_VPN, tab_index

        dlg = SettingsDialog()
        self.addCleanup(dlg.close)

        total_tabs = dlg._tab_sidebar.count()
        self.assertEqual(total_tabs, 14)

        # Search for a setting on the Views tab (e.g. "dark")
        dlg._search_input.setText("dark")
        visible_rows = [i for i in range(total_tabs) if not dlg._tab_sidebar.item(i).isHidden()]
        views_idx = tab_index(TAB_VIEWS)
        self.assertIn(views_idx, visible_rows)
        self.assertEqual(dlg._tabs.currentIndex(), views_idx)
        self.assertFalse(dlg._tab_sidebar.isHidden())
        self.assertTrue(dlg._no_results_label.isHidden())

        # Search for a multi-word setting on the VPN tab ("kill switch")
        dlg._search_input.setText("kill switch")
        visible_rows = [i for i in range(total_tabs) if not dlg._tab_sidebar.item(i).isHidden()]
        vpn_idx = tab_index(TAB_VPN)
        self.assertIn(vpn_idx, visible_rows)
        self.assertEqual(dlg._tabs.currentIndex(), vpn_idx)

        # Search for something non-existent
        dlg._search_input.setText("xyz_nonexistent_setting_12345")
        self.assertTrue(dlg._tab_sidebar.isHidden())
        self.assertFalse(dlg._no_results_label.isHidden())
        self.assertIn("No matching settings found", dlg._no_results_label.text())

        # Clearing restores all tabs
        dlg._search_input.clear()
        visible_rows = [i for i in range(total_tabs) if not dlg._tab_sidebar.item(i).isHidden()]
        self.assertEqual(len(visible_rows), total_tabs)
        self.assertFalse(dlg._tab_sidebar.isHidden())
        self.assertTrue(dlg._no_results_label.isHidden())

    def test_settings_dialog_search_highlights_matching_items_and_restores(self):
        dlg = SettingsDialog()
        self.addCleanup(dlg.close)

        # Initially no highlights
        self.assertEqual(getattr(dlg, "_highlighted_widgets", {}), {})

        # Search for a setting: "kill switch"
        dlg._search_input.setText("kill switch")
        highlighted = getattr(dlg, "_highlighted_widgets", {})
        self.assertGreater(len(highlighted), 0, "matching widgets should be highlighted")

        # Verify that highlighted widgets carry the yellow tint background rule
        sample_w = next(iter(highlighted.keys()))
        sample_style = sample_w.styleSheet()
        self.assertIn("background-color: rgba(", sample_style)
        self.assertTrue(
            "234, 179, 8" in sample_style or "250, 204, 21" in sample_style,
            f"style should contain yellow tint: {sample_style}",
        )

        # Clearing the search input must restore original styles and empty the highlight registry
        dlg._search_input.clear()
        self.assertEqual(dlg._highlighted_widgets, {})
        # The sample widget should no longer have the yellow background tint
        restored_style = sample_w.styleSheet()
        self.assertNotIn("234, 179, 8", restored_style)
        self.assertNotIn("250, 204, 21", restored_style)


class TestPreferencesPageRegistry(unittest.TestCase):
    """``TAB_ORDER`` / ``TAB_TITLES`` are the single source of truth for page identity.

    ``SettingsDialog`` used to take an ``initial_tab`` *index* and clamp it with
    ``0 <= initial_tab < count()``. Every index was in range, so when a page was inserted
    the dialog raised nothing and simply opened the wrong one - the 6 -> 9 tab split left
    six live Tools-menu items one or two pages off. A name that does not resolve is now a
    loud ``ValueError`` instead of a plausible wrong page.
    """

    def test_every_tab_name_is_distinct(self):
        self.assertEqual(
            len(set(TAB_ORDER)), len(TAB_ORDER),
            "a duplicated name would make tab_index() ambiguous",
        )

    def test_every_tab_has_a_non_empty_title(self):
        for name in TAB_ORDER:
            with self.subTest(tab=name):
                self.assertIn(name, TAB_TITLES)
                self.assertTrue(TAB_TITLES[name].strip())

    def test_no_title_exists_without_a_tab(self):
        self.assertEqual(
            set(TAB_TITLES), set(TAB_ORDER),
            "an orphan title is a page that was renamed but never registered",
        )

    def test_tab_index_returns_the_position_in_tab_order(self):
        for position, name in enumerate(TAB_ORDER):
            with self.subTest(tab=name):
                self.assertEqual(tab_index(name), position)

    def test_an_unknown_page_raises_rather_than_guessing(self):
        """The whole point of the registry: a typo must not become a wrong page."""
        with self.assertRaises(ValueError):
            tab_index("no-such-tab")
        with self.assertRaises(ValueError):
            tab_index("")

    def test_the_generic_tab_is_first(self):
        """Two menu items point at General with no argument; it must stay index 0."""
        self.assertEqual(tab_index(TAB_GENERAL), 0)

    def test_the_app_and_clipboard_tabs_sit_after_general(self):
        self.assertEqual(tab_index(TAB_APP), 1)
        self.assertEqual(tab_index(TAB_CLIPBOARD), 2)

    def test_the_views_tab_sits_after_core_tabs(self):
        self.assertEqual(
            tab_index(TAB_VIEWS), 3,
            "the Views tab belongs after the core General, App, and Clipboard tabs",
        )


class TestPreferencesDialogOpensTheNamedPage(ConfigIsolationMixin, unittest.TestCase):
    """``SettingsDialog`` must honour the page it is handed."""

    def setUp(self):
        super().setUp()
        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)

    def dialog(self, **kw):
        dlg = SettingsDialog(db=self.db, **kw)
        self.addCleanup(dlg.close)
        self.addCleanup(dlg.deleteLater)
        QApplication.processEvents()
        return dlg

    def test_each_name_opens_its_own_page(self):
        for name in TAB_ORDER:
            with self.subTest(tab=name):
                dlg = self.dialog(initial_tab=name)
                self.assertEqual(
                    dlg.current_tab_name(), name,
                    f"asked for {name!r}, landed on {dlg.current_tab_name()!r} "
                    f"({dlg._tabs.tabText(dlg._tabs.currentIndex())!r})",
                )
                dlg.close()
                dlg.deleteLater()

    def test_the_page_order_matches_the_registry(self):
        self.assertEqual(
            self.dialog()._tab_names, list(TAB_ORDER),
            "the insertion order and TAB_ORDER have drifted apart",
        )

    def test_the_page_titles_match_the_registry(self):
        dlg = self.dialog()
        self.assertEqual(
            [dlg._tabs.tabText(i) for i in range(dlg._tabs.count())],
            [TAB_TITLES[n] for n in TAB_ORDER],
        )

    def test_the_tab_count_matches_the_registry(self):
        self.assertEqual(self.dialog()._tabs.count(), len(TAB_ORDER))

    def test_an_unknown_page_name_is_rejected_loudly(self):
        with self.assertRaises(ValueError):
            SettingsDialog(db=self.db, initial_tab="nope")

    def test_a_legacy_integer_index_still_works(self):
        """Existing callers pass 0; that must not become a hard error."""
        self.assertEqual(self.dialog(initial_tab=0).current_tab_name(), TAB_GENERAL)

    def test_an_out_of_range_integer_is_clamped_rather_than_crashing(self):
        """Documented legacy behaviour; the point is that it must not raise."""
        self.assertEqual(self.dialog(initial_tab=9999)._tabs.currentIndex(), 0)

    def test_opening_with_no_page_defaults_to_general(self):
        self.assertEqual(self.dialog().current_tab_name(), TAB_GENERAL)

    def test_the_scheduler_tab_sits_last(self):
        self.assertEqual(tab_index(TAB_SCHEDULER), len(TAB_ORDER) - 1)

    def test_scheduler_tab_controls_exist_and_populate(self):
        dlg = self.dialog(initial_tab=TAB_SCHEDULER)
        self.assertIsNotNone(dlg._scheduler_enable_cb)
        self.assertIsNotNone(dlg._scheduler_start_time)
        self.assertIsNotNone(dlg._scheduler_end_time)
        self.assertIsNotNone(dlg._scheduler_pause_cb)
        self.assertEqual(len(dlg._scheduler_day_cbs), 7)

    def test_bandwidth_tab_has_expected_columns(self):
        dlg = self.dialog(initial_tab=TAB_BANDWIDTH)
        table = dlg._bw_table
        self.assertEqual(table.columnCount(), 7)
        headers = [table.horizontalHeaderItem(i).text() for i in range(7)]
        expected = [
            "Queue",
            "Enabled",
            "Limit",
            "Limit Type",
            "Progress",
            "Percetage for Warning",
            "Actions",
        ]
        self.assertEqual(headers, expected)

    def test_current_tab_name_is_empty_without_any_pages(self):
        """Must not IndexError on a dialog whose tab widget was never built."""
        from PySide6.QtWidgets import QTabWidget

        stub = SettingsDialog.__new__(SettingsDialog)
        stub._tabs = QTabWidget()
        stub._tab_names = []
        self.assertEqual(stub.current_tab_name(), "")

    def test_current_tab_name_is_empty_for_a_negative_index(self):
        from PySide6.QtWidgets import QTabWidget

        stub = SettingsDialog.__new__(SettingsDialog)
        stub._tabs = QTabWidget()
        stub._tab_names = list(TAB_ORDER)
        stub._tabs.setCurrentIndex(-1)
        self.assertEqual(stub.current_tab_name(), "")


class TestGeneralConfigReachesBothEngines(ConfigIsolationMixin, unittest.TestCase):
    """``GeneralConfig`` is read by *both* engines, so both must be updated together.

    The disk-space gate and ``metadata_fetch_timeout_days`` both live on
    ``GeneralConfig``, and ``TorrentEngine`` reads them from there. ``SettingsDialog``
    works on a **copy**, so before this was wired, unticking "check free disk space" in
    Preferences stopped HTTP downloads being checked while torrents carried on being
    refused with the settings as they were at startup - a half-applied preference that is
    impossible to spot from the UI.
    """

    def setUp(self):
        super().setUp()
        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)
        self.manager = DownloadManager(self.db)
        self.addCleanup(self.manager.stop)

    def replacement(self, **overrides):
        from my_idm.config import GeneralConfig

        replacement = GeneralConfig.from_dict(self.manager.general_config.to_dict())
        for key, value in overrides.items():
            setattr(replacement, key, value)
        return replacement

    def test_the_torrent_engine_receives_the_new_config_object(self):
        replacement = self.replacement(disk_space_check=False)
        self.manager.set_general_config(replacement)
        self.assertIs(
            self.manager._torrent._general_config, replacement,
            "TorrentEngine kept the config it was built with",
        )

    def test_the_http_engine_receives_the_same_object(self):
        replacement = self.replacement()
        self.manager.set_general_config(replacement)
        self.assertIs(self.manager._http._general_config, replacement)
        self.assertIs(
            self.manager._http._general_config,
            self.manager._torrent._general_config,
            "the two engines must never read different settings",
        )

    def test_turning_the_disk_space_gate_off_reaches_the_torrent_engine(self):
        self.manager.set_general_config(self.replacement(disk_space_check=False))
        self.assertFalse(self.manager._torrent._general_config.disk_space_check)

    def test_turning_the_disk_space_gate_on_reaches_the_torrent_engine(self):
        self.manager.set_general_config(
            self.replacement(disk_space_check=True, disk_space_headroom_mb=4096)
        )
        self.assertTrue(self.manager._torrent._general_config.disk_space_check)
        self.assertEqual(
            self.manager._torrent._general_config.disk_space_headroom_mb, 4096
        )

    def test_the_metadata_timeout_reaches_the_torrent_engine_too(self):
        """The same stale object silently governed this setting already."""
        self.manager.set_general_config(
            self.replacement(metadata_fetch_timeout_days=9)
        )
        self.assertEqual(
            self.manager._torrent._general_config.metadata_fetch_timeout_days, 9
        )

    def test_the_engine_object_itself_is_not_replaced(self):
        """The push must be a config swap, not a re-instantiation mid-session."""
        before = self.manager._torrent
        self.manager.set_general_config(self.manager.general_config)
        self.assertIs(self.manager._torrent, before)

    def test_the_concurrency_limit_reaches_the_torrent_engine(self):
        self.manager.set_general_config(
            self.replacement(max_concurrent_downloads=3)
        )
        self.assertEqual(
            self.manager._torrent._general_config.max_concurrent_downloads, 3
        )

    def test_the_segment_stagger_reaches_the_http_engine(self):
        """A `GeneralConfig` field the engine never reads is a preference that does nothing."""
        self.manager.set_general_config(
            self.replacement(segment_start_delay_ms=120)
        )
        self.assertEqual(
            self.manager._http._general_config.segment_start_delay_ms, 120
        )
        self.assertAlmostEqual(
            self.manager._http._segment_stagger_step(4), 0.12, places=6
        )


class TestManagerGeneralConfigIntegration(ConfigIsolationMixin, unittest.TestCase):
    """Test DownloadManager with GeneralConfig."""

    def setUp(self):
        super().setUp()
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.tmp_path = Path(tmp_dir.name) / "mgr.db"
        # Registered before db.close so it runs after it (addCleanup is LIFO).
        self.addCleanup(self._unlink_strict, self.tmp_path)
        self.db = Database(self.tmp_path)
        self.db.open()
        self.addCleanup(self.db.close)
        self.test_settings = QSettings("MyIDMTest", "My-IDMTest")
        self.test_settings.clear()

    def test_manager_preferences_window_size_persistence(self):
        mgr = DownloadManager(self.db)
        self.addCleanup(mgr.stop)
        mgr.save_preferences_window_size(920, 650)
        size = mgr.get_preferences_window_size()
        self.assertEqual(size, {"width": 920, "height": 650})
        # A second manager reading the same row must see the same geometry.
        mgr2 = DownloadManager(self.db)
        self.addCleanup(mgr2.stop)
        self.assertEqual(
            mgr2.get_preferences_window_size(), {"width": 920, "height": 650},
            "the geometry is persisted in the database, not kept in memory",
        )

    def test_manager_uses_configured_default_save_path(self):
        with tempfile.TemporaryDirectory() as custom_dir:
            mgr = DownloadManager(self.db)
            self.addCleanup(mgr.stop)
            cfg = GeneralConfig(default_save_path=custom_dir, remember_last_save_path=False)
            mgr.set_general_config(cfg)

            did = mgr.add_download("https://example.com/testfile.bin")
            self.assertIsNotNone(did)

            entry = mgr.get_entry(did)
            self.assertIsNotNone(entry)
            from my_idm.utils import normalize_path
            self.assertEqual(entry.save_path, normalize_path(custom_dir))
            self.assertTrue(
                entry.file_path.startswith(normalize_path(custom_dir)),
                f"the file must be resolved under the configured dir, got {entry.file_path!r}",
            )

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
        # The values are persisted, so the guard is what undoes them -- a bare
        # `GeneralConfig().save()` here only ran on the success path and left a
        # `backlog_poll_enabled=True` value behind for later tests.
        persisted = GeneralConfig.load()
        self.assertTrue(persisted.backlog_poll_enabled)
        self.assertEqual(persisted.backlog_poll_interval, 120)
        self.assertEqual(persisted.backlog_locations, dlg.general_config.backlog_locations)
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


class TestTorrentFileIntegrationSettings(ConfigIsolationMixin, unittest.TestCase):
    """The .torrent drag-and-drop / file-association / watched-folder controls.

    Separate from `TestSettingsDialog` because the behaviour under test is the *policy* around
    these three: the association is only reconciled when the checkbox actually moved, and the
    watched folder is stored blank when it matches the default download folder.
    """

    def _dialog(self, torrent_cfg=None, general_cfg=None):
        dlg = SettingsDialog(
            torrent_config=torrent_cfg or TorrentConfig(),
            general_config=general_cfg or GeneralConfig(),
            initial_tab=TAB_TORRENT,
        )
        self.addCleanup(dlg.close)
        return dlg

    def test_the_watch_folder_field_defaults_to_the_download_folder(self):
        with tempfile.TemporaryDirectory() as downloads:
            dlg = self._dialog(general_cfg=GeneralConfig(default_save_path=downloads))
            # A blank stored value means "the default download folder", and an empty field with a
            # greyed Browse button reads as a setting that cannot be changed.
            self.assertEqual(
                dlg._torrent_watch_edit.text().strip(),
                os.path.normpath(downloads),
            )

    def test_the_watch_folder_path_is_greyed_out_until_it_is_enabled(self):
        dlg = self._dialog()
        self.assertFalse(dlg._torrent_watch_edit.isEnabled())
        self.assertFalse(dlg._torrent_watch_clean_cb.isEnabled())
        self.assertFalse(dlg._torrent_watch_max_age_spin.isEnabled())
        dlg._torrent_watch_cb.setChecked(True)
        self.assertTrue(dlg._torrent_watch_edit.isEnabled())
        self.assertTrue(dlg._torrent_watch_clean_cb.isEnabled())
        self.assertTrue(dlg._torrent_watch_max_age_spin.isEnabled())

    def test_saving_clean_watched_torrent_files_stores_it(self):
        dlg = self._dialog(TorrentConfig(clean_watched_torrent_files=False))
        self.assertFalse(dlg._torrent_watch_clean_cb.isChecked())
        dlg._torrent_watch_clean_cb.setChecked(True)
        with patch("os.path.exists", return_value=True):
            dlg._on_save()
        self.assertTrue(dlg.torrent_config.clean_watched_torrent_files)

    def test_saving_torrent_watch_max_age_days_stores_it(self):
        dlg = self._dialog(TorrentConfig(torrent_watch_max_age_days=3))
        self.assertEqual(dlg._torrent_watch_max_age_spin.value(), 3)
        dlg._torrent_watch_max_age_spin.setValue(14)
        with patch("os.path.exists", return_value=True):
            dlg._on_save()
        self.assertEqual(dlg.torrent_config.torrent_watch_max_age_days, 14)

    def test_the_association_is_only_reconciled_when_the_checkbox_moved(self):
        dlg = self._dialog(TorrentConfig(associate_torrent_files=False))
        with patch("my_idm.file_assoc.reconcile", return_value=(True, "")) as reconcile:
            dlg._on_save()
            reconcile.assert_not_called()

        dlg2 = self._dialog(TorrentConfig(associate_torrent_files=False))
        with patch("my_idm.file_assoc.reconcile", return_value=(True, "")) as reconcile:
            dlg2._torrent_assoc_cb.setChecked(True)
            dlg2._on_save()
            reconcile.assert_called_once_with(True)

    def test_a_failed_association_still_records_the_preference(self):
        # Losing the toggle would be a worse lie than a failed registration the user is told
        # about: the intent survives a transient locked-hive failure and is retried next time.
        dlg = self._dialog(TorrentConfig(associate_torrent_files=False))
        with patch("my_idm.file_assoc.reconcile", return_value=(False, "hive is locked")), \
             patch("my_idm.settings_dialog.QMessageBox.warning"):
            dlg._torrent_assoc_cb.setChecked(True)
            dlg._on_save()
        self.assertTrue(dlg.torrent_config.associate_torrent_files)

    def test_saving_an_unchanged_watch_folder_stores_it_blank(self):
        # Blank is what makes the folder follow a later change to the download folder instead of
        # being pinned to whatever it was when the preference was first saved.
        with tempfile.TemporaryDirectory() as downloads:
            dlg = self._dialog(general_cfg=GeneralConfig(default_save_path=downloads))
            with patch("os.path.exists", return_value=True):
                dlg._on_save()
            self.assertEqual(dlg.torrent_config.torrent_watch_folder, "")

    def test_saving_a_custom_watch_folder_stores_it(self):
        dlg = self._dialog()
        dlg._torrent_watch_edit.setText("/torrents/in")
        with patch("os.path.exists", return_value=True):
            dlg._on_save()
        self.assertEqual(dlg.torrent_config.torrent_watch_folder, "/torrents/in")

    def test_the_association_status_line_reflects_the_real_state(self):
        from my_idm.file_assoc import FileAssocState

        dlg = self._dialog()
        with patch("my_idm.file_assoc.status", return_value=FileAssocState.REGISTERED):
            dlg._refresh_torrent_assoc_status()
        # Registered-but-not-default is the normal Windows state and must not be reported as
        # "it works", or double-clicking a .torrent would silently open something else.
        self.assertIn("not the default", dlg._torrent_assoc_status_lbl.text())

        with patch("my_idm.file_assoc.status", return_value=FileAssocState.DEFAULT):
            dlg._refresh_torrent_assoc_status()
        self.assertIn("open in My-IDM", dlg._torrent_assoc_status_lbl.text())

        with patch("my_idm.file_assoc.status", return_value=FileAssocState.STALE):
            dlg._refresh_torrent_assoc_status()
        self.assertIn("points somewhere else", dlg._torrent_assoc_status_lbl.text())
        # isHidden(), not isVisible(): the dialog is never shown, so isVisible() is False either
        # way and would assert nothing about what the code asked for.
        self.assertFalse(dlg._torrent_assoc_repair_btn.isHidden())

    def test_an_unsupported_platform_disables_the_checkbox(self):
        from my_idm.file_assoc import FileAssocState

        dlg = self._dialog()
        with patch("my_idm.file_assoc.status", return_value=FileAssocState.UNSUPPORTED):
            dlg._refresh_torrent_assoc_status()
        self.assertFalse(dlg._torrent_assoc_cb.isEnabled())

    def test_the_default_apps_button_is_only_offered_where_it_can_help(self):
        dlg = self._dialog()
        with patch("my_idm.file_assoc.backend_name", return_value="windows"):
            dlg._refresh_torrent_assoc_status()
        self.assertFalse(dlg._torrent_assoc_default_btn.isHidden())
        # On Linux the application really may set the default itself, so a button that opens a
        # system settings page would have nothing for the user to click.
        with patch("my_idm.file_assoc.backend_name", return_value="linux"):
            dlg._refresh_torrent_assoc_status()
        self.assertTrue(dlg._torrent_assoc_default_btn.isHidden())


class TestExternalToolsSettings(ConfigIsolationMixin, unittest.TestCase):
    """Tests for ExternalToolsConfig and SettingsDialog External Tools tab."""

    def setUp(self):
        super().setUp()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.ini_path = Path(self.tmp_dir.name) / "test_settings.ini"
        self.settings = QSettings(str(self.ini_path), QSettings.Format.IniFormat)

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
        # By name, not by index: `initial_tab=5` used to mean AnimePahe and quietly became
        # Tor when the 6 -> 9 tab split landed, and the assertion below - which only
        # checked the index it had just passed in - did not notice.
        dlg = SettingsDialog(
            external_tools_config=cfg, initial_tab=TAB_EXTERNAL_TOOLS
        )
        self.assertEqual(dlg._tabs.currentIndex(), tab_index(TAB_EXTERNAL_TOOLS))
        self.assertEqual(dlg.current_tab_name(), TAB_EXTERNAL_TOOLS)
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

    def test_download_via_animepahe_button_closes_dialog_silently(self):
        """The Download via AnimePahe button must not pop a confirm dialog.

        Instead it starts the scraper and closes Preferences, leaving the main
        window to switch the bottom panel to the live console. Only failures
        warn, and a missing repository warns without starting anything.
        """
        from unittest.mock import MagicMock
        mock_mgr = MagicMock()
        mock_mgr.is_animepahe_running.return_value = False
        mock_mgr.start_animepahe_scraper.return_value = (True, "Started PID: 1234")

        cfg = ExternalToolsConfig(animepahe_repo_path="/valid/path")
        dlg = SettingsDialog(external_tools_config=cfg, initial_tab=5, manager=mock_mgr)
        self.assertFalse(dlg.animepahe_download_started)
        dlg._animepahe_url_edit.setText("https://animepahe.ru/anime/4380")

        with patch("os.path.isdir", return_value=True), \
             patch("PySide6.QtWidgets.QMessageBox.information") as mock_info, \
             patch("PySide6.QtWidgets.QMessageBox.warning") as mock_warn:
            dlg._on_download_animepahe_url()
            # No confirm dialog on success.
            mock_info.assert_not_called()
            mock_warn.assert_not_called()
            # The dialog is closed and the flag is set so the main window can
            # switch to the console panel.
            self.assertTrue(dlg.animepahe_download_started)
            mock_mgr.start_animepahe_scraper.assert_called_once()

        dlg.close()

    def test_download_via_animepahe_button_warns_on_failure(self):
        from unittest.mock import MagicMock
        mock_mgr = MagicMock()
        mock_mgr.is_animepahe_running.return_value = False
        mock_mgr.start_animepahe_scraper.return_value = (False, "tor.exe not found")

        cfg = ExternalToolsConfig(animepahe_repo_path="/valid/path")
        dlg = SettingsDialog(external_tools_config=cfg, initial_tab=5, manager=mock_mgr)
        dlg._animepahe_url_edit.setText("https://animepahe.ru/anime/4380")

        with patch("os.path.isdir", return_value=True), \
             patch("PySide6.QtWidgets.QMessageBox.warning") as mock_warn:
            dlg._on_download_animepahe_url()
            mock_warn.assert_called_once()
            self.assertIn("tor.exe not found", mock_warn.call_args[0][2])
            # A failure must not close the dialog or claim a download started.
            self.assertFalse(dlg.animepahe_download_started)

        dlg.close()


class TestLaunchAtLoginPreference(ConfigIsolationMixin, unittest.TestCase):
    """Preferences -> Application: launch at login.

    The interesting behaviour is not the checkbox but *when* the OS registration is touched. The
    rule under test: a Save that did not move the checkbox must not touch the registration, or
    merely opening Preferences would resurrect a login item the user removed on purpose through
    Task Manager or their desktop's startup panel.

    ``my_idm.autostart`` is stubbed here rather than used for real - a live call would write a
    genuine ``HKCU\\...\\Run`` value on a Windows developer machine.
    """

    def _dialog(self, launch_at_login=False):
        from my_idm.settings_dialog import SettingsDialog

        dlg = SettingsDialog(general_config=GeneralConfig(launch_at_login=launch_at_login))
        self.addCleanup(dlg.close)
        return dlg

    @staticmethod
    def _fake_autostart(status):
        from my_idm import autostart

        calls = []

        def record(reconcile_or_none=None):
            calls.append(reconcile_or_none)
            return True, ""

        return calls, patch.multiple(
            autostart,
            status=staticmethod(lambda: status),
            reconcile=staticmethod(record),
        )

    def test_the_checkbox_reflects_the_stored_preference(self):
        from my_idm import autostart

        for stored in (False, True):
            with self.subTest(stored=stored):
                with patch.object(autostart, "status", return_value=autostart.AutostartState.DISABLED):
                    with patch.object(autostart, "location", return_value="test://here"):
                        dlg = self._dialog(launch_at_login=stored)
                        self.assertEqual(dlg._launch_at_login_cb.isChecked(), stored)

    def test_ticking_it_registers_the_entry(self):
        from my_idm import autostart

        with patch.object(autostart, "status", return_value=autostart.AutostartState.DISABLED):
            with patch.object(autostart, "location", return_value="test://here"):
                dlg = self._dialog(launch_at_login=False)
        calls, fake = self._fake_autostart(autostart.AutostartState.DISABLED)
        dlg._launch_at_login_cb.setChecked(True)
        with fake, patch("PySide6.QtWidgets.QMessageBox.warning") as warn:
            dlg._on_save()
        self.assertEqual(calls, [True], "ticking the box must register the entry")
        warn.assert_not_called()
        self.assertTrue(dlg._general_cfg.launch_at_login)

    def test_unticking_it_removes_the_entry(self):
        from my_idm import autostart

        with patch.object(autostart, "status", return_value=autostart.AutostartState.ENABLED):
            with patch.object(autostart, "location", return_value="test://here"):
                dlg = self._dialog(launch_at_login=True)
        calls, fake = self._fake_autostart(autostart.AutostartState.ENABLED)
        dlg._launch_at_login_cb.setChecked(False)
        with fake, patch("PySide6.QtWidgets.QMessageBox.warning"):
            dlg._on_save()
        self.assertEqual(calls, [False])
        self.assertFalse(dlg._general_cfg.launch_at_login)

    def test_a_save_that_does_not_touch_the_box_leaves_the_registration_alone(self):
        from my_idm import autostart

        with patch.object(autostart, "status", return_value=autostart.AutostartState.ENABLED):
            with patch.object(autostart, "location", return_value="test://here"):
                dlg = self._dialog(launch_at_login=True)
        calls, fake = self._fake_autostart(autostart.AutostartState.ENABLED)
        with fake, patch("PySide6.QtWidgets.QMessageBox.warning"):
            dlg._on_save()
        self.assertEqual(
            calls, [],
            "re-registering on every Save would override a removal made outside this app",
        )

    def test_a_failed_registration_warns_but_still_saves_the_preference(self):
        """Losing the toggle would be a worse lie than a registration the user is told failed."""
        from my_idm import autostart

        with patch.object(autostart, "status", return_value=autostart.AutostartState.DISABLED):
            with patch.object(autostart, "location", return_value="test://here"):
                dlg = self._dialog(launch_at_login=False)
        dlg._launch_at_login_cb.setChecked(True)
        with patch.object(autostart, "reconcile", return_value=(False, "access is denied")):
            with patch("PySide6.QtWidgets.QMessageBox.warning") as warn:
                dlg._on_save()
        warn.assert_called_once()
        self.assertIn("access is denied", warn.call_args[0][2])
        self.assertTrue(dlg._general_cfg.launch_at_login,
                        "the user's intent must survive a transient failure")

    def test_a_stale_entry_is_surfaced_with_a_repair_affordance(self):
        from my_idm import autostart

        with patch.object(autostart, "status", return_value=autostart.AutostartState.STALE):
            with patch.object(autostart, "location", return_value="test://here"):
                dlg = self._dialog(launch_at_login=True)
        # isHidden() rather than isVisible(): the dialog is never shown, and a child of a hidden
        # parent reports isVisible() == False regardless of what was asked for.
        self.assertFalse(dlg._autostart_repair_btn.isHidden())
        self.assertIn("will not start", dlg._autostart_status_lbl.text())

    def test_a_working_entry_hides_the_repair_button(self):
        from my_idm import autostart

        with patch.object(autostart, "status", return_value=autostart.AutostartState.ENABLED):
            with patch.object(autostart, "location", return_value="test://here"):
                dlg = self._dialog(launch_at_login=True)
        # isHidden(), not isVisible(): the dialog is never shown, so isVisible() is False either
        # way and would assert nothing about what the code asked for.
        self.assertTrue(dlg._autostart_repair_btn.isHidden())

    def test_an_unsupported_platform_disables_the_box_and_says_why(self):
        from my_idm import autostart

        with patch.object(autostart, "status", return_value=autostart.AutostartState.UNSUPPORTED):
            with patch.object(autostart, "location", return_value="not supported"):
                dlg = self._dialog(launch_at_login=True)
        self.assertFalse(dlg._launch_at_login_cb.isEnabled())
        self.assertFalse(dlg._launch_at_login_cb.isChecked())
        self.assertIn("not supported", dlg._autostart_status_lbl.text())

    def test_repair_rewrites_the_entry_and_refreshes_the_label(self):
        from my_idm import autostart

        with patch.object(autostart, "status", return_value=autostart.AutostartState.STALE):
            with patch.object(autostart, "location", return_value="test://here"):
                dlg = self._dialog(launch_at_login=True)
        with patch.object(autostart, "repair", return_value=(True, "")) as repair:
            with patch.object(autostart, "status", return_value=autostart.AutostartState.ENABLED):
                with patch("PySide6.QtWidgets.QMessageBox.information"):
                    dlg._on_repair_autostart()
        repair.assert_called_once()

    def test_the_status_probe_failing_does_not_break_preferences(self):
        from my_idm import autostart

        with patch.object(autostart, "status", side_effect=RuntimeError("probe exploded")):
            dlg = self._dialog(launch_at_login=False)
        self.assertIn("probe exploded", dlg._autostart_status_lbl.text())
        self.assertTrue(dlg._autostart_repair_btn.isHidden())


if __name__ == "__main__":
    unittest.main()
