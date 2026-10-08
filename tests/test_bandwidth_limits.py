"""Unit tests for Bandwidth Limits (database, manager, dialogs, and menubar UI)."""

import os
import sys
import tempfile
import unittest
from datetime import datetime, date, timedelta
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from my_idm.database import Database, DownloadEntry
from my_idm.manager import DownloadManager
from my_idm.settings_dialog import (
    TAB_BANDWIDTH,
    BandwidthLimitDialog,
    SettingsDialog,
    tab_index,
)

app = QApplication.instance() or QApplication(sys.argv)


class TestBandwidthLimitsDatabase(unittest.TestCase):
    """Test database schema, CRUD, usage tracking, and limit checks."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.db.open()

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def test_create_and_get_bandwidth_limit(self):
        # Global limit
        lim_id = self.db.create_bandwidth_limit(
            queue_id="",
            enabled=True,
            limit_bytes=5 * 1024 * 1024 * 1024,
            limit_type="monthly",
            warning_percent=80,
        )
        self.assertGreater(lim_id, 0)

        limits = self.db.get_bandwidth_limits("")
        self.assertEqual(len(limits), 1)
        self.assertEqual(limits[0]["queue_id"], "")
        self.assertEqual(limits[0]["limit_bytes"], 5 * 1024 * 1024 * 1024)
        self.assertEqual(limits[0]["limit_type"], "monthly")
        self.assertEqual(limits[0]["warning_percent"], 80)
        self.assertTrue(limits[0]["enabled"])

    def test_update_and_delete_bandwidth_limit(self):
        lim_id = self.db.create_bandwidth_limit(
            queue_id="q1",
            enabled=True,
            limit_bytes=1000,
            limit_type="daily",
            warning_percent=75,
        )
        # Update
        updated = self.db.update_bandwidth_limit(
            limit_id=lim_id,
            enabled=False,
            limit_bytes=2000,
            limit_type="weekly",
            warning_percent=90,
        )
        self.assertTrue(updated)
        limits = self.db.get_bandwidth_limits("q1")
        self.assertEqual(len(limits), 1)
        self.assertFalse(limits[0]["enabled"])
        self.assertEqual(limits[0]["limit_bytes"], 2000)
        self.assertEqual(limits[0]["limit_type"], "weekly")
        self.assertEqual(limits[0]["warning_percent"], 90)

        # Delete
        deleted = self.db.delete_bandwidth_limit(lim_id)
        self.assertTrue(deleted)
        self.assertEqual(len(self.db.get_bandwidth_limits("q1")), 0)

    def test_record_bandwidth_usage_bucketing(self):
        now = datetime(2026, 10, 8, 12, 0, 0)
        self.db.record_bandwidth("q1", downloaded_bytes=500, uploaded_bytes=200, now=now)

        # Daily usage
        dl, ul = self.db.get_current_period_usage("q1", "daily", now=now)
        self.assertEqual(dl, 500)
        self.assertEqual(ul, 200)

        # Global usage sums across queues
        self.db.record_bandwidth("q2", downloaded_bytes=300, uploaded_bytes=100, now=now)
        g_dl, g_ul = self.db.get_current_period_usage("", "daily", now=now)
        self.assertEqual(g_dl, 800)
        self.assertEqual(g_ul, 300)

    def test_check_bandwidth_limit_warning_and_exceeded(self):
        now = datetime(2026, 10, 8, 12, 0, 0)
        # Set 1000 bytes limit with 80% warning
        self.db.create_bandwidth_limit(
            queue_id="q1",
            enabled=True,
            limit_bytes=1000,
            limit_type="daily",
            warning_percent=80,
        )

        # At 500 bytes (50%), no warning
        self.db.record_bandwidth("q1", downloaded_bytes=500, uploaded_bytes=0, now=now)
        allowed, msg, pct = self.db.check_bandwidth_limit("q1", now=now)
        self.assertTrue(allowed)
        self.assertEqual(msg, "")
        self.assertEqual(pct, 0.0)

        # At 850 bytes (85%), warning triggered
        self.db.record_bandwidth("q1", downloaded_bytes=350, uploaded_bytes=0, now=now)
        allowed, msg, pct = self.db.check_bandwidth_limit("q1", now=now)
        self.assertTrue(allowed)
        self.assertIn("at 85.0%", msg)
        self.assertAlmostEqual(pct, 85.0)

        # At 1050 bytes (> 100%), limit exceeded
        self.db.record_bandwidth("q1", downloaded_bytes=200, uploaded_bytes=0, now=now)
        allowed, msg, pct = self.db.check_bandwidth_limit("q1", now=now)
        self.assertFalse(allowed)
        self.assertIn("exceeded", msg.lower())
        self.assertEqual(pct, 100.0)

    def test_global_limit_overrides_per_queue_limit(self):
        now = datetime(2026, 10, 8, 12, 0, 0)
        # Global limit: 1000 bytes
        self.db.create_bandwidth_limit(
            queue_id="",
            enabled=True,
            limit_bytes=1000,
            limit_type="daily",
            warning_percent=80,
        )
        # Queue limit: 5000 bytes (well above 1000)
        self.db.create_bandwidth_limit(
            queue_id="q1",
            enabled=True,
            limit_bytes=5000,
            limit_type="daily",
            warning_percent=80,
        )

        # Record 1000 bytes in q2, exhausting the global limit
        self.db.record_bandwidth("q2", downloaded_bytes=1000, uploaded_bytes=0, now=now)

        # Checking q1: even though q1 has 0 usage, global limit is exceeded and overrides q1!
        allowed, msg, pct = self.db.check_bandwidth_limit("q1", now=now)
        self.assertFalse(allowed, "global limit must override per-queue limit")
        self.assertIn("Global", msg)
        self.assertEqual(pct, 100.0)


class TestBandwidthLimitsManager(unittest.TestCase):
    """Test DownloadManager bandwidth limit enforcement, signals, and delta tracking."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.db.open()
        self.manager = DownloadManager(self.db)

    def tearDown(self):
        self.manager.stop()
        self.db.close()
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def test_may_start_blocked_when_limit_exceeded(self):
        # Create a download entry
        entry = DownloadEntry(
            id="dl1",
            url="http://example.com/test.bin",
            save_path=self.tmp.name,
            file_path=self.tmp.name,
            queue_id="default",
            status="queued",
        )
        self.db.add_download(entry)

        # Within limit: _may_start allows it
        limits = {"default": 5}
        counts = {"default": 0}
        self.assertTrue(self.manager._may_start(entry, limits, counts, global_max=10))

        # Set limit and exceed it
        self.db.create_bandwidth_limit(
            queue_id="",
            enabled=True,
            limit_bytes=100,
            limit_type="daily",
            warning_percent=80,
        )
        self.db.record_bandwidth("default", downloaded_bytes=150)

        # Now _may_start must return False!
        self.assertFalse(self.manager._may_start(entry, limits, counts, global_max=10))

    def test_check_all_bandwidth_limits_emits_signals(self):
        warn_mock = MagicMock()
        exceed_mock = MagicMock()
        cleared_mock = MagicMock()

        self.manager.bandwidth_warning.connect(warn_mock)
        self.manager.bandwidth_limit_exceeded.connect(exceed_mock)
        self.manager.bandwidth_warning_cleared.connect(cleared_mock)

        # No limits: emits cleared
        self.manager.check_all_bandwidth_limits()
        cleared_mock.assert_called_once()

        # Add limit with warning
        lim_id = self.db.create_bandwidth_limit(
            queue_id="",
            enabled=True,
            limit_bytes=1000,
            limit_type="daily",
            warning_percent=80,
        )
        self.db.record_bandwidth("default", downloaded_bytes=850)
        self.manager.check_all_bandwidth_limits()
        warn_mock.assert_called_once()
        self.assertTrue(warn_mock.call_args[0][3])  # is_global=True

        # Exceed limit
        self.db.record_bandwidth("default", downloaded_bytes=200)
        self.manager.check_all_bandwidth_limits()
        exceed_mock.assert_called_once()
        self.assertTrue(exceed_mock.call_args[0][3])  # is_global=True

    def test_progress_delta_tracking_not_accumulating_cumulative(self):
        # Test that _on_http_progress calculates delta (new - prev)
        entry = DownloadEntry(
            id="dl_progress",
            url="http://example.com/test.bin",
            save_path=self.tmp.name,
            file_path=self.tmp.name,
            queue_id="default",
            status="downloading",
        )
        self.db.add_download(entry)

        # First tick: 100 bytes downloaded
        self.manager._on_http_progress("dl_progress", 100, 1000, 50.0, 18.0)
        # Second tick: 150 bytes downloaded (delta = 50)
        self.manager._on_http_progress("dl_progress", 150, 1000, 50.0, 17.0)

        # Total usage recorded should be 50 bytes (first tick has no prev, second tick has delta 50)
        # Plus if 3rd tick is 200, delta = 50
        self.manager._on_http_progress("dl_progress", 200, 1000, 50.0, 16.0)

        dl, _ = self.db.get_current_period_usage("default", "daily")
        self.assertEqual(dl, 100)  # 50 + 50


@pytest.mark.ui
class TestBandwidthLimitDialog(unittest.TestCase):
    """Test BandwidthLimitDialog creation, validation, and acceptance."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.db.open()

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def test_dialog_creation_and_values(self):
        dlg = BandwidthLimitDialog(db=self.db)
        self.assertEqual(dlg._unit_combo.currentText(), "GB")
        self.assertEqual(dlg._type_combo.currentText(), "Monthly")
        self.assertEqual(dlg._warn_spin.value(), 80)
        dlg.close()
        dlg.deleteLater()

    def test_dialog_population_from_existing_limit(self):
        limit = {
            "id": 1,
            "queue_id": "",
            "enabled": 1,
            "limit_bytes": 5 * 1024 * 1024 * 1024,
            "limit_type": "weekly",
            "warning_percent": 85,
        }
        dlg = BandwidthLimitDialog(db=self.db, limit=limit)
        self.assertEqual(dlg._type_combo.currentText(), "Weekly")
        self.assertEqual(dlg._limit_spin.value(), 5)
        self.assertEqual(dlg._unit_combo.currentText(), "GB")
        self.assertEqual(dlg._warn_spin.value(), 85)
        dlg.close()
        dlg.deleteLater()

    def test_settings_dialog_bandwidth_tab_with_existing_limits(self):
        """SettingsDialog must open and render table rows without error when limits exist in DB."""
        self.db.create_bandwidth_limit(
            queue_id="",
            enabled=True,
            limit_bytes=5 * 1024 * 1024 * 1024,
            limit_type="monthly",
            warning_percent=80,
        )
        dlg = SettingsDialog(db=self.db, initial_tab=TAB_BANDWIDTH)
        self.assertEqual(dlg._bw_table.rowCount(), 1)
        self.assertIn("5.0 GiB", dlg._bw_table.item(0, 2).text())
        self.assertIn("0 Bytes / 5.0 GiB", dlg._bw_table.item(0, 4).text())
        if getattr(dlg, "_probe_worker", None) is not None:
            dlg._probe_worker.join(timeout=2.0)
        dlg.close()
        dlg.deleteLater()


if __name__ == "__main__":
    unittest.main()

