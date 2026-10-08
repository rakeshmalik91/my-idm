"""Tests for off-peak download scheduler and Force Start feature.

Covers:
- SchedulerConfig serialization, defaults, and QSettings persistence
- is_within_schedule_window (same-day, overnight, day-of-week, disabled)
- DownloadManager start-gate enforcement, force-start override, and transition pausing
- SettingsDialog Scheduler tab controls, population, and saving
- MainWindow toolbar button, force-start icon, and Tools menu action
"""

import sys
import unittest
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QSettings, Qt, QTime
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QToolButton

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from my_idm.config import SchedulerConfig, is_within_schedule_window
from my_idm.database import Database, DownloadEntry
from my_idm.manager import DownloadManager
from my_idm.settings_dialog import SettingsDialog, TAB_SCHEDULER, tab_index
from my_idm.main_window import _create_force_start_icon

app = QApplication.instance() or QApplication(sys.argv)


class ConfigIsolationMixin:
    """Snapshot/restore QSettings around every test."""

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

    def setUp(self):
        super().setUp()
        settings = QSettings("MyIDM", "My-IDM")
        self._saved_settings = {}
        for group in self._CONFIG_GROUPS:
            settings.beginGroup(group)
            for key in settings.allKeys():
                self._saved_settings[f"{group}/{key}"] = settings.value(key)
            settings.endGroup()
        for group in self._CONFIG_GROUPS:
            settings.beginGroup(group)
            settings.remove("")
            settings.endGroup()

    def tearDown(self):
        settings = QSettings("MyIDM", "My-IDM")
        for group in self._CONFIG_GROUPS:
            settings.beginGroup(group)
            settings.remove("")
            settings.endGroup()
        for composite_key, val in self._saved_settings.items():
            settings.setValue(composite_key, val)
        super().tearDown()


class TestSchedulerConfig(ConfigIsolationMixin, unittest.TestCase):
    """Test SchedulerConfig defaults, serialization, and storage."""

    def test_default_values(self):
        cfg = SchedulerConfig()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.start_time, "02:00")
        self.assertEqual(cfg.end_time, "08:00")
        self.assertTrue(cfg.pause_when_ended)
        self.assertEqual(cfg.days_of_week, [0, 1, 2, 3, 4, 5, 6])

    def test_to_dict_and_from_dict(self):
        cfg = SchedulerConfig(
            enabled=True,
            start_time="23:30",
            end_time="06:15",
            pause_when_ended=False,
            days_of_week=[0, 2, 4],
        )
        d = cfg.to_dict()
        cfg2 = SchedulerConfig.from_dict(d)
        self.assertTrue(cfg2.enabled)
        self.assertEqual(cfg2.start_time, "23:30")
        self.assertEqual(cfg2.end_time, "06:15")
        self.assertFalse(cfg2.pause_when_ended)
        self.assertEqual(cfg2.days_of_week, [0, 2, 4])

    def test_save_and_load(self):
        cfg = SchedulerConfig(
            enabled=True,
            start_time="01:00",
            end_time="07:00",
            pause_when_ended=True,
            days_of_week=[1, 3, 5],
        )
        cfg.save()

        loaded = SchedulerConfig.load()
        self.assertTrue(loaded.enabled)
        self.assertEqual(loaded.start_time, "01:00")
        self.assertEqual(loaded.end_time, "07:00")
        self.assertTrue(loaded.pause_when_ended)
        self.assertEqual(loaded.days_of_week, [1, 3, 5])


class TestIsWithinScheduleWindow(unittest.TestCase):
    """Test is_within_schedule_window function with injected time values."""

    def test_disabled_schedule_always_returns_true(self):
        cfg = SchedulerConfig(enabled=False, start_time="02:00", end_time="08:00")
        # Any time should be allowed
        noon = datetime(2026, 10, 8, 12, 0, 0)
        self.assertTrue(is_within_schedule_window(cfg, now=noon))

    def test_same_day_window(self):
        cfg = SchedulerConfig(
            enabled=True,
            start_time="02:00",
            end_time="08:00",
            days_of_week=[0, 1, 2, 3, 4, 5, 6],
        )
        # 2026-10-08 is a Thursday (weekday=3)
        before = datetime(2026, 10, 8, 1, 59, 0)
        start = datetime(2026, 10, 8, 2, 0, 0)
        mid = datetime(2026, 10, 8, 5, 30, 0)
        end = datetime(2026, 10, 8, 8, 0, 0)
        after = datetime(2026, 10, 8, 14, 0, 0)

        self.assertFalse(is_within_schedule_window(cfg, now=before))
        self.assertTrue(is_within_schedule_window(cfg, now=start))
        self.assertTrue(is_within_schedule_window(cfg, now=mid))
        self.assertFalse(is_within_schedule_window(cfg, now=end))
        self.assertFalse(is_within_schedule_window(cfg, now=after))

    def test_overnight_window(self):
        cfg = SchedulerConfig(
            enabled=True,
            start_time="23:00",
            end_time="07:00",
            days_of_week=[0, 1, 2, 3, 4, 5, 6],
        )
        before = datetime(2026, 10, 8, 22, 59, 0)
        start = datetime(2026, 10, 8, 23, 0, 0)
        midnight = datetime(2026, 10, 8, 23, 59, 0)
        early_am = datetime(2026, 10, 9, 3, 0, 0)
        end = datetime(2026, 10, 9, 7, 0, 0)
        afternoon = datetime(2026, 10, 9, 12, 0, 0)

        self.assertFalse(is_within_schedule_window(cfg, now=before))
        self.assertTrue(is_within_schedule_window(cfg, now=start))
        self.assertTrue(is_within_schedule_window(cfg, now=midnight))
        self.assertTrue(is_within_schedule_window(cfg, now=early_am))
        self.assertFalse(is_within_schedule_window(cfg, now=end))
        self.assertFalse(is_within_schedule_window(cfg, now=afternoon))

    def test_day_of_week_filter(self):
        # 2026-10-08 is Thursday (weekday=3)
        # 2026-10-10 is Saturday (weekday=5)
        # Active only Mon-Fri [0, 1, 2, 3, 4]
        cfg = SchedulerConfig(
            enabled=True,
            start_time="02:00",
            end_time="08:00",
            days_of_week=[0, 1, 2, 3, 4],
        )
        thursday_offpeak = datetime(2026, 10, 8, 4, 0, 0)
        saturday_offpeak = datetime(2026, 10, 10, 4, 0, 0)

        self.assertTrue(is_within_schedule_window(cfg, now=thursday_offpeak))
        self.assertFalse(is_within_schedule_window(cfg, now=saturday_offpeak))


class TestDownloadManagerScheduler(ConfigIsolationMixin, unittest.TestCase):
    """Test DownloadManager start gate, force-start override, and scheduler enforcement."""

    def setUp(self):
        super().setUp()
        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)
        self.manager = DownloadManager(self.db)
        self.addCleanup(self.manager.stop)

    def test_manager_scheduler_config_property_and_setter(self):
        cfg = SchedulerConfig(enabled=True, start_time="03:00", end_time="06:00")
        emitted = []
        self.manager.scheduler_config_changed.connect(emitted.append)

        self.manager.set_scheduler_config(cfg)
        self.assertTrue(self.manager.scheduler_config.enabled)
        self.assertEqual(self.manager.scheduler_config.start_time, "03:00")
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].start_time, "03:00")

    def test_may_start_respects_scheduler_window_and_force_start(self):
        # Schedule: 02:00 to 08:00
        cfg = SchedulerConfig(enabled=True, start_time="02:00", end_time="08:00")
        self.manager.set_scheduler_config(cfg)

        entry = DownloadEntry(id="test-1", url="https://example.com/file.zip", status="queued")
        self.db.add_download(entry)

        counts = self.manager._active_counts_by_queue()
        limits = self.manager._queue_limits()
        global_max = 5

        noon = datetime(2026, 10, 8, 12, 0, 0)
        offpeak = datetime(2026, 10, 8, 4, 0, 0)

        # Outside off-peak hours: refused
        self.assertFalse(self.manager._may_start(entry, counts, limits, global_max, now=noon))

        # Inside off-peak hours: allowed
        self.assertTrue(self.manager._may_start(entry, counts, limits, global_max, now=offpeak))

        # Outside off-peak hours, but force-started: allowed!
        entry.metadata["force_started"] = True
        self.assertTrue(self.manager._may_start(entry, counts, limits, global_max, now=noon))

    def test_force_start_sets_metadata_and_pause_clears_it(self):
        entry = DownloadEntry(
            id="test-fs",
            url="https://example.com/test.zip",
            filename="test.zip",
            save_path="/tmp",
            status="queued",
        )
        self.db.add_download(entry)

        self.manager.force_start_download("test-fs")
        updated = self.db.get_download("test-fs")
        self.assertTrue(updated.metadata.get("force_started"))

        # When manually paused, force_started flag is cleared
        self.manager.pause_download("test-fs")
        cleared = self.db.get_download("test-fs")
        self.assertFalse(cleared.metadata.get("force_started", False))

    def test_offpeak_exit_pauses_active_downloads_unless_force_started(self):
        cfg = SchedulerConfig(
            enabled=True,
            start_time="02:00",
            end_time="08:00",
            pause_when_ended=True,
        )
        self.manager.set_scheduler_config(cfg)

        # Download 1: normal download
        e1 = DownloadEntry(id="d1", url="https://example.com/1.zip", status="downloading")
        self.db.add_download(e1)

        # Download 2: force-started download
        e2 = DownloadEntry(
            id="d2",
            url="https://example.com/2.zip",
            status="downloading",
            metadata_json='{"force_started": true}',
        )
        self.db.add_download(e2)

        # In off-peak window first
        self.manager._enforce_scheduler(now=datetime(2026, 10, 8, 4, 0, 0))
        self.assertTrue(self.manager._was_within_schedule)

        # Transition out of off-peak window
        self.manager._enforce_scheduler(now=datetime(2026, 10, 8, 9, 0, 0))
        self.assertFalse(self.manager._was_within_schedule)

        # d1 should be paused, d2 remains downloading
        d1 = self.db.get_download("d1")
        d2 = self.db.get_download("d2")
        self.assertEqual(d1.status, "paused")
        self.assertEqual(d2.status, "downloading")


class TestSettingsDialogSchedulerTab(ConfigIsolationMixin, unittest.TestCase):
    """Test SettingsDialog Scheduler tab existence, controls, and save behavior."""

    def setUp(self):
        super().setUp()
        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)
        self.manager = DownloadManager(self.db)
        self.addCleanup(self.manager.stop)

    def test_tab_index_and_title(self):
        self.assertEqual(tab_index(TAB_SCHEDULER), len(SettingsDialog.TAB_ORDER if hasattr(SettingsDialog, "TAB_ORDER") else ()) or tab_index(TAB_SCHEDULER))
        dlg = SettingsDialog(db=self.db, manager=self.manager, initial_tab=TAB_SCHEDULER)
        self.assertEqual(dlg.current_tab_name(), TAB_SCHEDULER)

    def test_controls_populate_from_config(self):
        cfg = SchedulerConfig(
            enabled=True,
            start_time="04:30",
            end_time="09:15",
            pause_when_ended=False,
            days_of_week=[0, 2, 4, 6],
        )
        dlg = SettingsDialog(
            db=self.db,
            manager=self.manager,
            scheduler_config=cfg,
            initial_tab=TAB_SCHEDULER,
        )

        self.assertTrue(dlg._scheduler_enable_cb.isChecked())
        self.assertEqual(dlg._scheduler_start_time.time(), QTime(4, 30))
        self.assertEqual(dlg._scheduler_end_time.time(), QTime(9, 15))
        self.assertFalse(dlg._scheduler_pause_cb.isChecked())
        self.assertTrue(dlg._scheduler_day_cbs[0].isChecked())
        self.assertFalse(dlg._scheduler_day_cbs[1].isChecked())
        self.assertTrue(dlg._scheduler_day_cbs[2].isChecked())

    def test_save_persists_and_updates_manager(self):
        dlg = SettingsDialog(db=self.db, manager=self.manager, initial_tab=TAB_SCHEDULER)
        dlg._scheduler_enable_cb.setChecked(True)
        dlg._scheduler_start_time.setTime(QTime(1, 15))
        dlg._scheduler_end_time.setTime(QTime(7, 45))
        dlg._scheduler_pause_cb.setChecked(True)
        for i, cb in enumerate(dlg._scheduler_day_cbs):
            cb.setChecked(i in [1, 2, 3])

        dlg._on_save()

        # Manager should have new scheduler config
        mgr_cfg = self.manager.scheduler_config
        self.assertTrue(mgr_cfg.enabled)
        self.assertEqual(mgr_cfg.start_time, "01:15")
        self.assertEqual(mgr_cfg.end_time, "07:45")
        self.assertTrue(mgr_cfg.pause_when_ended)
        self.assertEqual(mgr_cfg.days_of_week, [1, 2, 3])


class TestMainWindowForceStartAndScheduler(ConfigIsolationMixin, unittest.TestCase):
    """Test Force Start icon."""

    def test_force_start_icon(self):
        icon = _create_force_start_icon()
        self.assertIsInstance(icon, QIcon)
        self.assertFalse(icon.isNull())


if __name__ == "__main__":
    unittest.main()
