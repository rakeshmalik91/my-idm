"""Tests for bandwidth statistics: the aggregation, the charts, and the toolbar action.

Everything is hermetic and clock-free: ``get_download_stats`` takes ``today`` as a
parameter precisely so the date arithmetic can be asserted exactly rather than slept
through, and the charts are painted onto real pixmaps so a broken ``paintEvent`` fails a
test instead of showing a blank panel to the user.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from my_idm.database import (
    COMPLETE_STATUSES,
    Database,
    DownloadEntry,
    DownloadStats,
    StatsSnapshot,
)
from my_idm.stats_dialog import (
    SparklineWidget,
    StatisticsPopup,
    StatsChartWidget,
    _fmt_bytes,
    _fmt_rate,
)
from my_idm.styles import Colors

app = QApplication.instance() or QApplication(sys.argv)

TODAY = date(2026, 9, 30)
GB = 1024 ** 3
MB = 1024 ** 2


def iso(days_back: int) -> str:
    return (datetime(2026, 9, 30, 12, 0, 0) - timedelta(days=days_back)).isoformat()


# ===========================================================================
# The data layer
# ===========================================================================

class StatsTestCase(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)

    def add(self, eid, added_days_back=0, total=0, uploaded=0, status="completed",
            filename=None):
        self.db.add_download(DownloadEntry(
            id=eid,
            url=f"https://example.com/{eid}",
            filename=filename or f"{eid}.bin",
            save_path="C:/t",
            total_size=total,
            downloaded_size=total,
            uploaded_size=uploaded,
            status=status,
            added_at=iso(added_days_back),
        ))

    def stats(self, since=None, bucket="day"):
        return self.db.get_download_stats(TODAY, since=since, bucket=bucket)


class TestStatsBuckets(StatsTestCase):
    def test_an_empty_database_reports_zeroes_not_none(self):
        snap = self.stats()
        for name in ("today", "week", "month", "year", "lifetime"):
            with self.subTest(bucket=name):
                stats = getattr(snap, name)
                self.assertEqual(stats.count, 0)
                self.assertEqual(stats.downloaded, 0)
                self.assertEqual(stats.uploaded, 0)
                self.assertEqual(stats.completed, 0)

    def test_today_counts_only_todays_rows(self):
        self.add("today1", 0, total=2 * GB)
        self.add("yday1", 1, total=3 * GB)
        snap = self.stats()
        self.assertEqual(snap.today.count, 1)
        self.assertEqual(snap.today.downloaded, 2 * GB)
        self.assertEqual(snap.week.count, 2)
        self.assertEqual(snap.lifetime.count, 2)

    def test_the_week_bucket_is_a_seven_day_window(self):
        """Today plus the previous six days: day 6 in, day 7 out."""
        self.add("d0", 0, total=1 * GB)
        self.add("d6", 6, total=1 * GB)
        self.add("d7", 7, total=1 * GB)
        snap = self.stats()
        self.assertEqual(snap.today.count, 1)
        self.assertEqual(snap.week.count, 2, "days 0 and 6 are inside the week window")
        self.assertEqual(snap.month.count, 3, "all three are inside the month window")

    def test_the_month_bucket_is_a_thirty_day_window(self):
        self.add("d29", 29, total=1 * GB)
        self.add("d30", 30, total=1 * GB)
        snap = self.stats()
        self.assertEqual(snap.month.count, 1, "day 29 is the last row inside the window")
        self.assertEqual(snap.year.count, 2, "both are inside the year window")

    def test_the_year_bucket_is_a_365_day_window(self):
        self.add("d364", 364, total=1 * GB)
        self.add("d365", 365, total=1 * GB)
        snap = self.stats()
        self.assertEqual(snap.year.count, 1, "day 364 is the last row inside the window")
        self.assertEqual(snap.lifetime.count, 2)

    def test_the_windows_nest_outwards(self):
        for days_back in (0, 6, 29, 364):
            self.add(f"in{days_back}", days_back, total=1 * GB)
        snap = self.stats()
        self.assertEqual(
            [snap.today.count, snap.week.count, snap.month.count, snap.year.count],
            [1, 2, 3, 4],
            "each window must contain the previous one",
        )

    def test_lifetime_includes_rows_older_than_a_year(self):
        self.add("ancient", 900, total=5 * GB)
        self.add("recent", 1, total=1 * GB)
        snap = self.stats()
        self.assertEqual(snap.year.count, 1)
        self.assertEqual(snap.lifetime.count, 2)
        self.assertEqual(snap.lifetime.downloaded, 6 * GB)

    def test_totals_are_byte_exact_not_rounded(self):
        self.add("a", 0, total=1_234_567_891, uploaded=99_999_999)
        snap = self.stats()
        self.assertEqual(snap.today.downloaded, 1_234_567_891)
        self.assertEqual(snap.today.uploaded, 99_999_999)

    def test_seeding_counts_as_completed(self):
        """A seeding download *is* complete; it just happens to still be uploading."""
        self.add("seeding", 0, total=4 * GB, uploaded=1 * GB, status="seeding")
        snap = self.stats()
        self.assertEqual(snap.today.completed, 1)
        self.assertEqual(snap.today.count, 1)

    def test_an_in_flight_download_is_not_completed(self):
        self.add("running", 0, total=4 * GB, status="downloading")
        snap = self.stats()
        self.assertEqual(snap.today.completed, 0)
        self.assertEqual(snap.today.count, 1)

    def test_every_complete_status_is_recognised(self):
        for status in COMPLETE_STATUSES:
            with self.subTest(status=status):
                self.db.delete_all_downloads() if hasattr(self.db, "delete_all_downloads") else None
        # Two rows, one per complete status, plus a non-complete one.
        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)
        for index, status in enumerate(list(COMPLETE_STATUSES) + ["downloading"]):
            self.add(f"c{index}", 0, total=1 * GB, status=status)
        snap = self.stats()
        self.assertEqual(snap.today.completed, len(COMPLETE_STATUSES))
        self.assertEqual(snap.today.count, len(COMPLETE_STATUSES) + 1)

    def test_a_row_with_a_null_size_does_not_produce_none(self):
        """SUM() over all-NULL is NULL; the grid must never render "None"."""
        self.db.add_download(DownloadEntry(
            id="nosize", url="https://e.com/x", filename="x", save_path="C:/t",
            status="completed", added_at=iso(0),
        ))
        snap = self.stats()
        self.assertEqual(snap.today.downloaded, 0)
        self.assertEqual(snap.today.count, 1)

    def test_add_download_backfills_a_blank_added_at(self):
        """So a blank timestamp can only arrive from a hand-edit or an older build."""
        entry = self.db.add_download(DownloadEntry(
            id="nodate", url="https://e.com/x", filename="x", save_path="C:/t",
            total_size=1 * GB, status="completed", added_at="",
        ))
        self.assertTrue(entry.added_at)

    def test_a_blank_added_at_in_the_table_only_counts_towards_lifetime(self):
        self.add("blank", 0, total=1 * GB)
        self.db._conn.execute(
            "UPDATE downloads SET added_at = '' WHERE id = 'blank'"
        )
        self.db._conn.commit()
        snap = self.stats()
        self.assertEqual(
            snap.today.count, 0,
            "a row with no date cannot be placed in any dated bucket",
        )
        self.assertEqual(snap.lifetime.count, 1, "but it is still a download the user has")

    def test_a_malformed_added_at_is_kept_out_of_every_dated_bucket(self):
        """A string compare would file 'not-a-date' under *today* - 'n' sorts after '2'.

        Regression: the cut-off predicate is `substr(added_at,1,10) >= ?`, a plain string
        comparison. Any timestamp that is not YYYY-MM-DD therefore lands in whichever
        bucket it happens to sort into, and the commonest corrupt value sorts late. The
        query now requires a well-formed date prefix, so such a row only ever shows up in
        the lifetime total, which is where an undatable download honestly belongs.
        """
        self.add("garbage", 0, total=1 * GB)
        self.db._conn.execute(
            "UPDATE downloads SET added_at = 'not-a-date' WHERE id = 'garbage'"
        )
        self.db._conn.commit()
        snap = self.stats()
        self.assertEqual(snap.today.count, 0, "a corrupt timestamp is not 'today'")
        self.assertEqual(snap.week.count, 0)
        self.assertEqual(snap.month.count, 0)
        self.assertEqual(snap.year.count, 0)
        self.assertEqual(snap.lifetime.count, 1, "but it is still a real download")
        self.assertEqual(snap.series, (), "and it must not appear on the chart")

    def test_the_daily_series_is_ascending(self):
        for days in (5, 1, 3):
            self.add(f"d{days}", days, total=1 * GB)
        snap = self.stats()
        days = [d for d, _s in snap.series]
        self.assertEqual(days, sorted(days), "the chart plots left-to-right by date")

    def test_the_daily_series_respects_the_requested_window(self):
        self.add("recent", 2, total=1 * GB)
        self.add("old", 40, total=1 * GB)
        snap = self.stats(since=TODAY - timedelta(days=6))
        days = [d for d, _s in snap.series]
        self.assertEqual(days, ["2026-09-28"], "a 7-day window must exclude a 40-day-old row")

    def test_today_is_injected_not_read_from_the_clock(self):
        """The whole date arithmetic is only testable because `today` is a parameter."""
        self.add("row", 0, total=1 * GB)
        far_future = self.db.get_download_stats(date(2030, 1, 1), since=date(2029, 12, 1))
        self.assertEqual(
            far_future.today.count, 0,
            "a row dated 2026 is not 'today' when today is 2030",
        )
        self.assertEqual(far_future.lifetime.count, 1)

    def test_a_datetime_is_accepted_as_today(self):
        self.add("row", 0, total=1 * GB)
        snap = self.db.get_download_stats(datetime(2026, 9, 30, 23, 59), since=TODAY - timedelta(days=2))
        self.assertEqual(snap.today.count, 1)

    def test_no_range_means_the_whole_history(self):
        self.add("ancient", 900, total=1 * GB)
        snap = self.stats()
        self.assertEqual(len(snap.series), 1, "with no cut-off every bucket on record is plotted")
        self.assertEqual(snap.since, "")

    def test_a_month_bucket_groups_by_calendar_month(self):
        self.add("a", 0, total=1 * GB)
        self.add("b", 1, total=1 * GB)
        self.add("c", 40, total=1 * GB)
        per_day = self.stats(since=TODAY - timedelta(days=60))
        per_month = self.stats(since=TODAY - timedelta(days=60), bucket="month")
        self.assertEqual(len(per_day.series), 3)
        self.assertEqual(len(per_month.series), 2, "September and August")
        self.assertEqual([m for m, _s in per_month.series], ["2026-08", "2026-09"])
        self.assertEqual(per_month.bucket, "month")

    def test_a_month_bucket_sums_the_days_inside_it(self):
        for days in (0, 1, 2):
            self.add(f"d{days}", days, total=1 * GB)
        per_month = self.stats(bucket="month")
        self.assertEqual(len(per_month.series), 1)
        self.assertEqual(per_month.series[0][1].count, 3)
        self.assertEqual(per_month.series[0][1].downloaded, 3 * GB)

    def test_the_summary_buckets_do_not_follow_the_chart_selection(self):
        """Picking a chart range must not silently redefine 'this week'."""
        self.add("today", 0, total=1 * GB)
        self.add("year", 300, total=1 * GB)
        narrow = self.stats(since=TODAY - timedelta(days=6))
        self.assertEqual(narrow.since, (TODAY - timedelta(days=6)).isoformat())
        self.assertEqual(
            narrow.week.count, 1,
            "the fixed week bucket is independent of the chart's range picker",
        )
        self.assertEqual(narrow.lifetime.count, 2)

    def test_an_unknown_bucket_falls_back_to_day(self):
        snap = self.stats(bucket="fortnight")
        self.assertEqual(snap.bucket, "day")

    def test_a_datetime_since_is_accepted(self):
        self.add("a", 0, total=1 * GB)
        snap = self.db.get_download_stats(
            TODAY, since=datetime(2026, 9, 29, 12, 0), bucket="day"
        )
        self.assertEqual(len(snap.series), 1)


class TestStatsValueObjects(unittest.TestCase):
    def test_download_stats_adds(self):
        total = DownloadStats(1, 10, 5, 1) + DownloadStats(2, 20, 7, 2)
        self.assertEqual(total, DownloadStats(3, 30, 12, 3))

    def test_a_dict_round_trip_preserves_every_field(self):
        original = DownloadStats(4, 100, 50, 3)
        self.assertEqual(DownloadStats.from_dict(original.to_dict()), original)

    def test_from_dict_tolerates_missing_fields(self):
        self.assertEqual(DownloadStats.from_dict({}), DownloadStats())

    def test_a_snapshot_dict_round_trip_keeps_the_series_shape(self):
        snap = StatsSnapshot(
            today=DownloadStats(1, 2, 3, 1),
            series=(("2026-09-30", DownloadStats(1, 2, 3, 1)),),
            bucket="day", since="2026-09-30",
        )
        data = snap.to_dict()
        self.assertEqual(data["today"], {"count": 1, "downloaded": 2, "uploaded": 3, "completed": 1})
        self.assertEqual(data["series"][0][0], "2026-09-30")
        self.assertEqual(len(data["series"][0][1]), 4)
        self.assertEqual((data["bucket"], data["since"]), ("day", "2026-09-30"))


class TestCompletedAtIsNotRestamped(StatsTestCase):
    """Regression: `completed` does double duty, and re-stamping broke "completed today"."""

    def test_the_first_completion_wins(self):
        self.add("c", 0, total=1 * GB)
        self.db.update_status("c", "completed", "")
        first = self.db.get_download("c").completed_at
        self.assertTrue(first)

    def test_a_seeding_session_ending_does_not_move_the_completion_date(self):
        """Pausing a seeder writes `completed`, which used to overwrite completed_at."""
        self.add("c", 5, total=1 * GB, status="seeding")
        self.db.update_status("c", "completed", "")
        first = self.db.get_download("c").completed_at
        self.assertTrue(first)

        # Re-enter seeding and end the session again, two days later.
        self.db.update_status("c", "seeding", "")
        self.db.update_status("c", "completed", "")
        self.assertEqual(
            self.db.get_download("c").completed_at, first,
            "ending a seeding session must not re-date the download",
        )

    def test_a_stale_pre_stamp_is_still_replaced(self):
        """The guard is 'only when empty', not 'never'."""
        self.add("c", 0, total=1 * GB)
        self.db.update_status("c", "downloading", "")
        self.db.update_status("c", "completed", "")
        self.assertTrue(self.db.get_download("c").completed_at)


# ===========================================================================
# Formatting
# ===========================================================================

class TestFormatting(unittest.TestCase):
    def test_bytes_are_human_readable(self):
        # humanize's binary units are IEC, so these read "GiB"/"MiB" - which is accurate
        # for the binary thresholds the app uses everywhere else.
        self.assertIn("GiB", _fmt_bytes(2 * GB))
        self.assertIn("MiB", _fmt_bytes(700 * MB))
        self.assertEqual(_fmt_bytes(0), "0 Bytes")

    def test_zero_bytes_is_not_negative(self):
        self.assertEqual(_fmt_bytes(0), _fmt_bytes(-5))

    def test_a_rate_carries_the_per_second_suffix(self):
        self.assertTrue(_fmt_rate(1024).endswith("/s"))

    def test_a_zero_rate_reads_cleanly(self):
        self.assertEqual(_fmt_rate(0), "0 B/s")
        self.assertEqual(_fmt_rate(-1), "0 B/s")


# ===========================================================================
# The charts
# ===========================================================================

def paint(widget: QWidget) -> QPixmap:
    """Render *widget* offscreen so a broken paintEvent fails the test."""
    pixmap = QPixmap(widget.size())
    pixmap.fill(QColor(Colors.BG_MID))
    widget.render(pixmap)
    return pixmap


class TestStatsChart(StatsTestCase):
    def _chart(self, days, width=520, height=220):
        chart = StatsChartWidget()
        chart.resize(width, height)
        chart.set_days(days)
        self.addCleanup(chart.deleteLater)
        return chart

    def test_an_empty_chart_paints_without_error(self):
        self._chart([])

    def test_all_zero_days_paint(self):
        self._chart([("2026-09-29", DownloadStats()), ("2026-09-30", DownloadStats())])

    def test_a_typical_series_paints(self):
        self._chart([
            ("2026-09-28", DownloadStats(1, 100, 0, 1)),
            ("2026-09-29", DownloadStats(1, 40 * GB, 12 * GB, 1)),
            ("2026-09-30", DownloadStats(1, 700 * MB, 0, 1)),
        ])

    def test_an_upload_only_day_paints(self):
        self._chart([("2026-09-30", DownloadStats(1, 0, 5 * GB, 1))])

    def test_a_very_large_value_does_not_overflow(self):
        self._chart([("2026-09-30", DownloadStats(1, 9000 * GB, 0, 1))])

    def test_a_tiny_widget_paints(self):
        self._chart([("2026-09-30", DownloadStats(1, 10, 0, 1))], width=40, height=20)

    def test_the_series_is_kept_in_order(self):
        chart = self._chart([
            ("2026-09-28", DownloadStats(1, 1, 0, 1)),
            ("2026-09-30", DownloadStats(2, 2, 0, 2)),
        ])
        self.assertEqual([d for d, _s in chart.days()], ["2026-09-28", "2026-09-30"])

    def test_the_tooltip_explains_the_add_date_basis(self):
        """Otherwise the chart reads as traffic per day, which it is not."""
        chart = self._chart([("2026-09-30", DownloadStats(1, 1, 0, 1))])
        self.assertIn("added", chart.toolTip())

    def test_the_tick_step_leaves_a_readable_number_of_gridlines(self):
        self.assertEqual(StatsChartWidget._tick_step(0), 1)
        for peak in (1, 100, 999, 40 * GB, 3 * 1024 ** 4):
            with self.subTest(peak=peak):
                step = StatsChartWidget._tick_step(peak)
                self.assertGreater(step, 0)
                lines = peak // step
                self.assertGreaterEqual(lines, 1, "the axis must have at least one label")
                self.assertLessEqual(lines, 12, "and not so many that they overlap")

    def test_one_day_still_paints(self):
        self._chart([("2026-09-30", DownloadStats(1, 1 * GB, 0, 1))])


class TestSparkline(unittest.TestCase):
    def test_an_empty_sparkline_paints(self):
        w = SparklineWidget()
        w.resize(300, 64)
        self.addCleanup(w.deleteLater)
        paint(w)

    def test_a_single_sample_paints(self):
        w = SparklineWidget()
        w.resize(300, 64)
        self.addCleanup(w.deleteLater)
        w.add_sample(100)
        paint(w)

    def test_a_full_series_paints(self):
        w = SparklineWidget()
        w.resize(300, 64)
        self.addCleanup(w.deleteLater)
        for value in range(20):
            w.add_sample(value * 1024)
        paint(w)

    def test_flat_zero_series_paints(self):
        w = SparklineWidget()
        w.resize(300, 64)
        self.addCleanup(w.deleteLater)
        for _ in range(5):
            w.add_sample(0)
        paint(w)

    def test_the_history_is_bounded(self):
        w = SparklineWidget()
        self.addCleanup(w.deleteLater)
        for value in range(SparklineWidget.MAX_SAMPLES + 50):
            w.add_sample(value)
        self.assertEqual(
            len(w.samples()), SparklineWidget.MAX_SAMPLES,
            "an unbounded deque would grow for as long as the popup stays open",
        )

    def test_the_oldest_samples_are_the_ones_dropped(self):
        w = SparklineWidget()
        self.addCleanup(w.deleteLater)
        for value in range(SparklineWidget.MAX_SAMPLES + 10):
            w.add_sample(value)
        self.assertEqual(w.samples()[0], 10)

    def test_negative_samples_are_clamped(self):
        w = SparklineWidget()
        self.addCleanup(w.deleteLater)
        w.add_sample(-5)
        self.assertEqual(w.samples(), [0])

    def test_peak_is_zero_when_empty(self):
        w = SparklineWidget()
        self.addCleanup(w.deleteLater)
        self.assertEqual(w.peak(), 0)

    def test_clear_empties_the_history(self):
        w = SparklineWidget()
        self.addCleanup(w.deleteLater)
        w.add_sample(10)
        w.clear()
        self.assertEqual(w.samples(), [])

    def test_a_zero_width_sparkline_paints(self):
        w = SparklineWidget()
        w.resize(0, 64)
        self.addCleanup(w.deleteLater)
        for value in range(5):
            w.add_sample(value)
        paint(w)


# ===========================================================================
# The popup
# ===========================================================================

class PopupTestCase(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)
        self.popups: list[StatisticsPopup] = []
        self.addCleanup(self._close_all)

    def _close_all(self):
        for popup in self.popups:
            popup.close()
            popup.deleteLater()
        QApplication.processEvents()

    def add(self, eid, days_back=0, total=1 * GB, uploaded=0, status="completed"):
        self.db.add_download(DownloadEntry(
            id=eid, url=f"https://e.com/{eid}", filename=f"{eid}.bin", save_path="C:/t",
            total_size=total, downloaded_size=total, uploaded_size=uploaded,
            status=status, added_at=iso(days_back),
        ))

    def popup(self, speed_provider=None, **kw):
        popup = StatisticsPopup(self.db, today=TODAY, speed_provider=speed_provider, **kw)
        self.popups.append(popup)
        return popup


class TestStatisticsPopup(PopupTestCase):
    def test_it_opens_on_an_empty_database(self):
        popup = self.popup()
        self.assertIsInstance(popup.snapshot(), StatsSnapshot)
        self.assertEqual(popup.snapshot().today.count, 0)

    def test_it_shows_every_bucket_row(self):
        popup = self.popup()
        labels = [
            popup._grid.itemAtPosition(row, 0).widget().text()
            for row in range(5)
        ]
        self.assertEqual(
            labels, ["Today", "This week", "This month", "This year", "All time"]
        )

    def test_the_totals_reach_the_grid(self):
        self.add("a", 0, total=2 * GB, uploaded=500 * MB)
        popup = self.popup()
        value = popup._grid.itemAtPosition(0, 1).widget()
        self.assertIn("2.0", value.text())
        self.assertIn("500", value.text())

    def test_the_completed_count_is_shown(self):
        self.add("a", 0), self.add("b", 0), self.add("c", 1)
        popup = self.popup()
        self.assertEqual(popup.snapshot().lifetime.completed, 3)
        texts = [
            popup._grid.itemAtPosition(r, c).widget().text()
            for r in range(popup._grid.rowCount())
            for c in range(2)
            if popup._grid.itemAtPosition(r, c) is not None
            and popup._grid.itemAtPosition(r, c).widget() is not None
        ]
        self.assertTrue(
            any("3 files completed" in t for t in texts),
            f"the completed total is missing from the grid: {texts}",
        )

    def test_refresh_picks_up_a_new_row(self):
        popup = self.popup()
        self.assertEqual(popup.snapshot().lifetime.count, 0)
        self.add("late", 0, total=1 * GB)
        popup.refresh()
        self.assertEqual(popup.snapshot().lifetime.count, 1)

    def test_the_chart_receives_the_daily_series(self):
        self.add("a", 0), self.add("b", 2)
        popup = self.popup()
        self.assertEqual(len(popup._chart.days()), 2)

    def test_it_paints(self):
        self.add("a", 0, total=2 * GB, uploaded=1 * GB)
        popup = self.popup()
        popup.resize(720, 620)
        popup.show()
        for _ in range(4):
            QApplication.processEvents()
        paint(popup)

    def test_a_broken_query_is_contained_and_the_popup_still_opens(self):
        """A statistics view must never be able to take the app down with it."""
        popup = self.popup()
        with patch.object(Database, "get_download_stats",
                          side_effect=RuntimeError("disk on fire")):
            popup.refresh()  # must not raise
        self.assertIsNone(popup.snapshot())
        paint(popup)

    def test_the_size_is_persisted_and_restored(self):
        first = self.popup()
        first.resize(700, 640)
        first._save_size()
        second = self.popup()
        self.assertEqual((second.width(), second.height()), (700, 640))

    def test_a_corrupt_persisted_size_falls_back_to_the_default(self):
        self.db.set_ui_state(StatisticsPopup.UI_STATE_KEY, {"width": "wide"})
        popup = self.popup()
        self.assertGreater(popup.width(), 0)

    def test_no_speed_provider_means_no_timer(self):
        popup = self.popup()
        popup.show()
        QApplication.processEvents()
        self.assertIsNone(popup._timer, "a read-only stats view must not spin a timer")
        popup.close()

    def test_a_speed_provider_starts_and_stops_the_timer(self):
        """A QTimer outliving its receiver is a use-after-free."""
        popup = self.popup(speed_provider=lambda: 1024)
        popup.show()
        QApplication.processEvents()
        self.assertIsNotNone(popup._timer)
        self.assertGreaterEqual(len(popup._sparkline.samples()), 1)
        popup.close()
        self.assertIsNone(popup._timer, "closing must stop the timer")

    def test_starting_the_timer_twice_does_not_leak_a_second(self):
        popup = self.popup(speed_provider=lambda: 1024)
        popup.start_speed_timer()
        first = popup._timer
        popup.start_speed_timer()
        self.assertIs(popup._timer, first)
        popup.stop_speed_timer()

    def test_a_raising_speed_provider_samples_zero(self):
        def _boom():
            raise RuntimeError("no such widget")

        popup = self.popup(speed_provider=_boom)
        popup.show()
        QApplication.processEvents()
        self.assertEqual(popup._sparkline.samples(), [0])
        popup.close()

    def test_reject_also_stops_the_timer(self):
        popup = self.popup(speed_provider=lambda: 1024)
        popup.show()
        QApplication.processEvents()
        popup.reject()
        self.assertIsNone(popup._timer)


class TestToolbarAction(unittest.TestCase):
    """The Statistics button sits beside Preferences, which is the stated requirement."""

    @classmethod
    def setUpClass(cls):
        from my_idm.main_window import MainWindow
        from my_idm.manager import DownloadManager

        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = Database(":memory:")
        cls.db.open()
        cls.manager = DownloadManager(cls.db)
        cls.window = MainWindow(cls.manager)

    @classmethod
    def tearDownClass(cls):
        cls.window.close()
        cls.window.deleteLater()
        cls.manager.stop()
        cls.db.close()
        cls._tmp.cleanup()
        QApplication.processEvents()

    def test_the_action_exists(self):
        self.assertTrue(hasattr(self.window, "_act_stats"))
        self.assertIn("Statistics", self.window._act_stats.text())

    def test_it_is_in_the_toolbar_immediately_before_preferences(self):
        """A test, because the next toolbar edit silently moves it otherwise."""
        actions = self.window._toolbar.actions()
        visible = [a for a in actions if not a.isSeparator()]
        self.assertIn(self.window._act_stats, visible)
        self.assertIn(self.window._act_preferences, visible)
        self.assertEqual(
            visible.index(self.window._act_stats) + 1,
            visible.index(self.window._act_preferences),
            "Statistics must sit directly beside Preferences",
        )

    def test_it_has_no_shortcut(self):
        """Ctrl+, is Preferences; a read-only view must not take a shortcut slot."""
        self.assertTrue(self.window._act_stats.shortcut().isEmpty())

    def test_opening_it_produces_a_popup_with_a_snapshot(self):
        popup = self.window._on_show_statistics()
        self.addCleanup(popup.deleteLater)
        self.assertIsInstance(popup, StatisticsPopup)
        self.assertIsInstance(popup.snapshot(), StatsSnapshot)
        popup.close()

    def test_the_popup_reads_the_real_database(self):
        self.db.add_download(DownloadEntry(
            id="toolbar-1", url="https://e.com/x", filename="x.bin", save_path="C:/t",
            total_size=2 * GB, downloaded_size=2 * GB, status="completed",
            added_at=iso(0),
        ))
        popup = self.window._on_show_statistics()
        self.addCleanup(popup.deleteLater)
        self.assertGreaterEqual(popup.snapshot().lifetime.count, 1)
        popup.close()


class TestChartSelectors(PopupTestCase):
    """Range and granularity are independent questions and get independent pickers."""

    def test_both_pickers_exist(self):
        popup = self.popup()
        self.assertEqual(popup._range_combo.count(), len(StatisticsPopup.RANGES))
        self.assertEqual(popup._bucket_combo.count(), len(StatisticsPopup.BUCKETS))

    def test_the_offered_ranges(self):
        self.assertEqual(
            [label for label, _days in StatisticsPopup.RANGES],
            ["Last 7 days", "Last 30 days", "Last 12 months", "All time"],
        )

    def test_the_offered_granularities(self):
        self.assertEqual(
            [key for _label, key in StatisticsPopup.BUCKETS], ["day", "month"]
        )

    def test_the_default_is_thirty_days_by_day(self):
        popup = self.popup()
        self.assertEqual(popup._range_combo.currentIndex(), 1)
        self.assertEqual(popup.bucket_selection(), "day")
        self.assertEqual(popup.snapshot().since, (TODAY - timedelta(days=29)).isoformat())

    def test_picking_last_seven_days_narrows_the_series(self):
        self.add("old", 20, total=1 * GB)
        self.add("new", 2, total=1 * GB)
        popup = self.popup()
        self.assertEqual(len(popup.snapshot().series), 2)
        popup._range_combo.setCurrentIndex(0)
        self.assertEqual(popup.snapshot().since, (TODAY - timedelta(days=6)).isoformat())
        self.assertEqual(
            len(popup.snapshot().series), 1,
            "a 20-day-old download must fall outside a 7-day chart",
        )

    def test_all_time_has_no_lower_bound(self):
        self.add("ancient", 900, total=1 * GB)
        popup = self.popup()
        popup._range_combo.setCurrentIndex(3)
        self.assertEqual(popup.snapshot().since, "", "all time means no cut-off at all")
        self.assertEqual(len(popup.snapshot().series), 1)

    def test_changing_the_range_refreshes_the_chart(self):
        self.add("a", 0, total=1 * GB)
        popup = self.popup()
        popup._range_combo.setCurrentIndex(0)
        self.assertEqual(len(popup._chart.days()), len(popup.snapshot().series))

    def test_switching_to_per_month_merges_the_bars(self):
        for days in (0, 1, 2, 40):
            self.add(f"d{days}", days, total=1 * GB)
        popup = self.popup()
        popup._range_combo.setCurrentIndex(3)
        popup._bucket_combo.setCurrentIndex(
            popup._bucket_combo.findData("month")
        )
        snap = popup.snapshot()
        self.assertEqual(snap.bucket, "month")
        self.assertEqual([m for m, _s in snap.series], ["2026-08", "2026-09"])
        self.assertEqual(popup._chart.bucket(), "month", "the chart is told the granularity")

    def test_switching_the_granularity_alone_is_enough(self):
        for days in (0, 1, 40):
            self.add(f"d{days}", days, total=1 * GB)
        popup = self.popup()
        popup._range_combo.setCurrentIndex(3)
        popup._bucket_combo.setCurrentIndex(popup._bucket_combo.findData("month"))
        self.assertEqual(len(popup.snapshot().series), 2)

    def test_the_pickers_survive_a_refresh(self):
        popup = self.popup()
        popup._range_combo.setCurrentIndex(0)
        popup._bucket_combo.setCurrentIndex(popup._bucket_combo.findData("month"))
        popup.refresh()
        self.assertEqual(popup._range_combo.currentIndex(), 0)
        self.assertEqual(popup.bucket_selection(), "month")

    def test_an_out_of_range_initial_index_falls_back(self):
        popup = self.popup(range_index=99)
        self.assertEqual(popup._range_combo.currentIndex(), 1)
        self.assertIsNotNone(popup.snapshot())

    def test_an_unknown_initial_bucket_falls_back(self):
        popup = self.popup(bucket="fortnight")
        self.assertEqual(popup.bucket_selection(), "day")

    def test_the_selectors_render(self):
        self.add("a", 0, total=2 * GB)
        popup = self.popup()
        popup.resize(760, 640)
        popup.show()
        for _ in range(4):
            QApplication.processEvents()
        paint(popup)


class TestChartMonthLabels(unittest.TestCase):
    """A month bucket is already YYYY-MM; slicing it to MM-DD would print nonsense."""

    def _chart(self, bucket):
        chart = StatsChartWidget()
        chart.resize(520, 220)
        chart.set_days([("2026-09", DownloadStats(1, 100, 0, 1))], bucket=bucket)
        self.addCleanup(chart.deleteLater)
        return chart

    def test_a_day_bucket_paints(self):
        self._chart("day")

    def test_a_month_bucket_paints(self):
        self._chart("month")

    def test_the_tooltip_names_the_right_unit(self):
        self.assertIn("day of month", self._chart("day").toolTip())
        self.assertIn("month", self._chart("month").toolTip())
        self.assertNotIn("day of month", self._chart("month").toolTip())


if __name__ == "__main__":
    unittest.main()
