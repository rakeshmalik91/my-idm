"""Tests for bandwidth statistics: the aggregation, the charts, and the toolbar action.

Everything is hermetic and clock-free: ``get_download_stats`` takes ``today`` as a
parameter precisely so the date arithmetic can be asserted exactly rather than slept
through, and the charts are painted onto real pixmaps so a broken ``paintEvent`` fails a
test instead of showing a blank panel to the user.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication, QToolButton, QWidget

IS_HEADLESS_WIN_CI = sys.platform == "win32" and os.environ.get("CI") == "true"

from my_idm.database import (
    COMPLETE_STATUSES,
    Database,
    DownloadEntry,
    DownloadStats,
    StatsSnapshot,
)
from my_idm.settings_dialog import TAB_GENERAL
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


def local_day(days_back: int) -> date:
    """The *local* calendar day ``days_back`` days before :data:`TODAY`."""
    return TODAY - timedelta(days=days_back)


def iso(days_back: int) -> str:
    local_noon = datetime.combine(local_day(days_back), time(12, 0))
    return local_noon.astimezone(timezone.utc).isoformat()


def iso_at(days_back: int, hour: int = 12, minute: int = 0) -> str:
    local_dt = datetime.combine(local_day(days_back), time(hour, minute))
    return local_dt.astimezone(timezone.utc).isoformat()


# ===========================================================================
# The data layer
# ===========================================================================

class StatsTestCase(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)

    def add(self, eid, added_days_back=0, total=0, uploaded=0, status="completed",
            filename=None, hour=12, minute=0):
        self.db.add_download(DownloadEntry(
            id=eid,
            url=f"https://example.com/{eid}",
            filename=filename or f"{eid}.bin",
            save_path="C:/t",
            total_size=total,
            downloaded_size=total,
            uploaded_size=uploaded,
            status=status,
            added_at=iso_at(added_days_back, hour=hour, minute=minute),
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

    def test_an_hour_bucket_groups_by_hour(self):
        self.add("h1", 0, total=1 * GB, hour=10, minute=15)
        self.add("h2", 0, total=2 * GB, hour=10, minute=45)
        self.add("h3", 0, total=3 * GB, hour=14, minute=0)
        per_hour = self.stats(since=TODAY, bucket="hour")
        self.assertEqual(per_hour.bucket, "hour")
        self.assertEqual([h for h, _s in per_hour.series], ["2026-09-30 10", "2026-09-30 14"])
        self.assertEqual(per_hour.series[0][1].count, 2)
        self.assertEqual(per_hour.series[0][1].downloaded, 3 * GB)
        self.assertEqual(per_hour.series[1][1].count, 1)
        self.assertEqual(per_hour.series[1][1].downloaded, 3 * GB)

    def test_a_minute_bucket_groups_by_minute(self):
        self.add("m1", 0, total=1 * GB, hour=10, minute=15)
        self.add("m2", 0, total=2 * GB, hour=10, minute=15)
        self.add("m3", 0, total=3 * GB, hour=10, minute=30)
        per_minute = self.stats(since=TODAY, bucket="minute")
        self.assertEqual(per_minute.bucket, "minute")
        self.assertEqual([m for m, _s in per_minute.series], ["2026-09-30 10:15", "2026-09-30 10:30"])
        self.assertEqual(per_minute.series[0][1].count, 2)
        self.assertEqual(per_minute.series[0][1].downloaded, 3 * GB)
        self.assertEqual(per_minute.series[1][1].count, 1)

    def test_a_5min_bucket_groups_by_5min_quantized(self):
        self.add("m1", 0, total=1 * GB, hour=10, minute=11)
        self.add("m2", 0, total=2 * GB, hour=10, minute=14)
        self.add("m3", 0, total=3 * GB, hour=10, minute=17)
        self.add("m4", 0, total=4 * GB, hour=10, minute=20)
        per_5m = self.stats(since=TODAY, bucket="5min")
        self.assertEqual(per_5m.bucket, "5min")
        self.assertEqual([m for m, _s in per_5m.series], ["2026-09-30 10:10", "2026-09-30 10:15", "2026-09-30 10:20"])
        self.assertEqual(per_5m.series[0][1].count, 2)
        self.assertEqual(per_5m.series[0][1].downloaded, 3 * GB)
        self.assertEqual(per_5m.series[1][1].count, 1)
        self.assertEqual(per_5m.series[1][1].downloaded, 3 * GB)
        self.assertEqual(per_5m.series[2][1].count, 1)
        self.assertEqual(per_5m.series[2][1].downloaded, 4 * GB)

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


class TestStatsBucketByLocalDay(StatsTestCase):
    """The buckets are labelled in the user's calendar; the rows are stamped in UTC.

    ``added_at`` comes from ``_now_iso()``, which is UTC, while the cut-offs are built
    from a local ``today``. Slicing the stored string therefore compared a **UTC** date
    against a **local** one, so "Today" was a day out for part of every day east of UTC:
    at 02:00 in UTC+05:30 a file added two minutes earlier is stamped ``2026-09-29T20:30``
    and landed in *Yesterday*. The queries now re-base each row with SQLite's
    ``localtime`` - the same conversion ``download_model.get_entry_date_category`` does
    with ``astimezone()`` - before bucketing.

    Because the conversion reads the host's timezone, expectations here are computed in
    Python from the same stored value rather than hard-coded. That keeps every test valid
    on any machine while still failing if the conversion is removed.
    """

    def _local_day_of(self, stored: str) -> str:
        return datetime.fromisoformat(stored).astimezone().date().isoformat()

    def _sql_day_of(self, db, stored: str) -> str:
        return db._conn.execute(
            f"SELECT {Database._STATS_LOCAL_DAY} FROM downloads WHERE added_at = ?",
            (stored,),
        ).fetchone()[0]

    def _sql_month_of(self, db, stored: str) -> str:
        return db._conn.execute(
            f"SELECT {Database._STATS_LOCAL_MONTH} FROM downloads WHERE added_at = ?",
            (stored,),
        ).fetchone()[0]

    def add_at(self, eid, local_day, hour=12, minute=0, total=1 * GB):
        stored = datetime.combine(local_day, time(hour, minute)).astimezone(
            timezone.utc
        ).isoformat()
        self.add(eid, (TODAY - local_day).days, total=total)
        self.db._conn.execute(
            "UPDATE downloads SET added_at = ? WHERE id = ?", (stored, eid)
        )
        return stored

    def test_the_day_expression_converts_to_local_time(self):
        """Guards the regression on hosts where a UTC clock would hide it.

        A bare ``substr(added_at, 1, 10)`` comparison *is* the defect; requiring the
        conversion in the expression makes the test fail even at UTC+00:00.
        """
        expr = Database._STATS_LOCAL_DAY
        self.assertIn(
            "localtime", expr,
            "the day bucket must re-base the stored UTC stamp to local time",
        )
        self.assertNotIn(
            "added_at, 1, 10", expr,
            "a bare slice of the stored string compares a UTC date to a local cut-off",
        )

    def test_the_month_expression_converts_too(self):
        self.assertIn("localtime", Database._STATS_LOCAL_MONTH)

    def test_the_hour_expression_converts_too(self):
        self.assertIn("localtime", Database._STATS_LOCAL_HOUR)

    def test_the_minute_expression_converts_too(self):
        self.assertIn("localtime", Database._STATS_LOCAL_MINUTE)

    def test_the_5min_expression_converts_too(self):
        self.assertIn("localtime", Database._STATS_LOCAL_5MIN)

    def test_sqlite_and_python_agree_on_the_converted_day(self):
        """Pins the conversion contract whatever the host offset is."""
        for day in (TODAY, TODAY - timedelta(days=1), TODAY - timedelta(days=40)):
            for hour in (0, 6, 12, 18, 23):
                with self.subTest(day=day, hour=hour):
                    stored = self.add_at(f"agree-{day}-{hour}", day, hour, total=1)
                    self.assertEqual(
                        self._sql_day_of(self.db, stored),
                        self._local_day_of(stored),
                        "SQLite's localtime and Python's astimezone must agree",
                    )

    def test_the_day_and_month_expressions_share_a_prefix(self):
        stored = self.add_at("prefix", TODAY, 12, total=1)
        self.assertEqual(
            self._sql_day_of(self.db, stored)[:7], self._sql_month_of(self.db, stored)
        )

    def test_moments_across_today_all_count_in_today(self):
        stamps = [
            self.add_at(f"t{hour}", TODAY, hour, total=1 * GB)
            for hour in (0, 6, 12, 18, 23)
        ]
        snap = self.stats()
        expected = sum(
            1 * GB for stamp in stamps
            if self._local_day_of(stamp) == TODAY.isoformat()
        )
        self.assertEqual(snap.today.downloaded, expected)
        self.assertEqual(snap.today.count, expected // (1 * GB))

    def test_a_file_added_at_local_midnight_is_today(self):
        """The boundary case the bug was about: local 00:05, stored as the previous UTC day."""
        stored = self.add_at("midnight", TODAY, 0, 5, total=4096)
        snap = self.stats()
        self.assertEqual(
            snap.today.count, 1,
            "a file added at 00:05 local is Today whatever its UTC stamp says "
            f"(stored {stored}, local day {self._local_day_of(stored)})",
        )

    def test_a_file_added_at_2355_the_previous_local_day_is_not_today(self):
        stored = self.add_at("late", TODAY - timedelta(days=1), 23, 55, total=2048)
        snap = self.stats()
        self.assertEqual(
            snap.today.count, 0,
            f"23:55 local on the previous day is not Today (stored {stored})",
        )
        self.assertEqual(snap.week.count, 1, "it is still inside the rolling week")

    def test_the_series_buckets_by_the_local_day(self):
        morning = self.add_at("morning", TODAY, 6, total=1 * GB)
        evening = self.add_at(
            "evening", TODAY - timedelta(days=1), 23, total=1 * GB
        )
        snap = self.stats(since=TODAY - timedelta(days=5))
        self.assertEqual(
            {label for label, _ in snap.series},
            {self._local_day_of(morning), self._local_day_of(evening)},
        )

    def test_the_month_series_buckets_by_the_local_month(self):
        self.add_at("sep", TODAY, 12, total=1 * GB)
        self.add_at("aug", date(2026, 8, 31), 12, total=1 * GB)
        snap = self.stats(since=date(2026, 7, 1), bucket="month")
        labels = [label for label, _ in snap.series]
        self.assertTrue(all(len(label) == 7 for label in labels), labels)
        self.assertEqual(len(labels), 2, labels)

    def test_the_month_series_is_chronological_and_complete(self):
        for day in (date(2026, 7, 1), date(2026, 8, 15), date(2026, 9, 15)):
            self.add_at(f"m{day}", day, 12, total=1 * GB)
        snap = self.stats(since=date(2026, 1, 1), bucket="month")
        labels = [label for label, _ in snap.series]
        self.assertEqual(labels, sorted(labels), "the chart series must be chronological")
        self.assertEqual(sum(s.count for _l, s in snap.series), 3)

    def test_the_today_headline_agrees_with_the_table_today_section(self):
        """End to end: two features answer "what is today?" and must agree.

        ``DownloadTableModel`` in date mode converts through ``astimezone()``; the
        statistics queries convert through SQLite's ``localtime``. Nothing else in the
        suite would notice if only one of them stopped converting.
        """
        from my_idm.download_model import SECTION_DATE_TODAY, DownloadTableModel

        now = datetime.now().astimezone()
        day = now.date()
        for index, hour in enumerate((0, 8, 13, 22)):
            stored = datetime.combine(day, time(hour)).astimezone(
                timezone.utc
            ).isoformat()
            self.db.add_download(DownloadEntry(
                id=f"real{index}", url=f"https://example.com/{index}",
                filename=f"{index}.bin", save_path="C:/t",
                total_size=1024, downloaded_size=1024, status="completed",
                added_at=stored,
            ))
        yesterday = datetime.combine(
            day - timedelta(days=1), time(12)
        ).astimezone(timezone.utc).isoformat()
        self.db.add_download(DownloadEntry(
            id="old", url="https://example.com/old", filename="old.bin",
            save_path="C:/t", total_size=999, downloaded_size=999,
            status="completed", added_at=yesterday,
        ))

        model = DownloadTableModel()
        try:
            model.load_entries(self.db.get_all_downloads())
            model.set_segregated_view(True, "date")
            in_today, current = 0, None
            for row in range(model.rowCount()):
                header = model.get_section_header(row)
                if header is not None:
                    current = header.section_id
                elif current == SECTION_DATE_TODAY:
                    in_today += 1
            self.assertGreater(
                in_today, 0, "precondition: rows landed in the table's Today section"
            )
        finally:
            model.deleteLater()

        snap = self.db.get_download_stats(now.date())
        self.assertEqual(
            snap.today.count, in_today,
            "the statistics 'Today' count and the table's Today section must match",
        )
        self.assertEqual(snap.today.downloaded, in_today * 1024)


class TestStatsRejectsUnbucketableTimestamps(StatsTestCase):
    """A row that cannot be placed on a calendar must not fabricate a bucket.

    Values are written straight through SQL on purpose: ``Database.add_download`` backfills
    a blank ``added_at`` with the current time - correct for a new download - so a corrupt
    stamp can only reach the table from an older build or by hand, which is the case here.
    """

    BAD = ("", "not-a-date", "2026-13-45T00:00:00+00:00", "0000-00-00", "x" * 40)

    def fresh_db_with(self, eid, added_at):
        db = Database(":memory:")
        self.addCleanup(db.close)
        db.open()
        db.add_download(DownloadEntry(
            id=eid, url=f"https://example.com/{eid}", filename=f"{eid}.bin",
            save_path="C:/t", total_size=1024,
        ))
        db._conn.execute(
            "UPDATE downloads SET added_at = ? WHERE id = ?", (added_at, eid)
        )
        return db

    def test_a_corrupt_timestamp_never_lands_in_a_dated_bucket(self):
        for index, stamp in enumerate(self.BAD):
            with self.subTest(stamp=stamp):
                snap = self.fresh_db_with(f"bad{index}", stamp).get_download_stats(TODAY)
                for name in ("today", "week", "month", "year"):
                    self.assertEqual(
                        getattr(snap, name).count, 0,
                        f"{stamp!r} was bucketed into {name!r}",
                    )

    def test_a_corrupt_timestamp_still_counts_towards_lifetime(self):
        for index, stamp in enumerate(self.BAD):
            with self.subTest(stamp=stamp):
                snap = self.fresh_db_with(f"badl{index}", stamp).get_download_stats(TODAY)
                self.assertEqual(
                    snap.lifetime.count, 1,
                    "a hand-written row is still a download the user has",
                )
                self.assertEqual(snap.lifetime.downloaded, 1024)

    def test_a_corrupt_timestamp_never_produces_a_none_bucket_on_the_chart(self):
        """A ``GROUP BY`` on a NULL expression still forms a group, labelled "None".

        ``since=None`` - the **All time** range - is the only case that regresses. The
        bounded ranges carry a ``day >= ?`` comparison and ``NULL >= x`` is NULL, which is
        false, so those rows drop out on their own; All time has no comparison to exclude
        them, so only the explicit parse check keeps them out.
        """
        for index, stamp in enumerate(self.BAD):
            with self.subTest(stamp=stamp):
                db = self.fresh_db_with(f"bads{index}", stamp)
                for bucket in ("day", "month", "hour", "minute"):
                    snap = db.get_download_stats(TODAY, since=None, bucket=bucket)
                    labels = [label for label, _ in snap.series]
                    self.assertNotIn(
                        "None", labels,
                        f"{stamp!r} produced a literal 'None' bucket on the "
                        f"{bucket} chart: {labels}",
                    )
                    self.assertNotIn("", labels)

    def test_a_corrupt_timestamp_is_excluded_from_a_bounded_chart_too(self):
        for index, stamp in enumerate(self.BAD):
            with self.subTest(stamp=stamp):
                db = self.fresh_db_with(f"badsb{index}", stamp)
                for bucket in ("day", "month", "hour", "minute"):
                    snap = db.get_download_stats(
                        TODAY, since=date(2020, 1, 1), bucket=bucket
                    )
                    labels = [label for label, _ in snap.series]
                    self.assertNotIn("None", labels)
                    self.assertNotIn("", labels)

    def test_a_corrupt_timestamp_does_not_hide_a_valid_one(self):
        db = self.fresh_db_with("mixed-bad", "not-a-date")
        stored = datetime.combine(TODAY, time(12)).astimezone(
            timezone.utc
        ).isoformat()
        db.add_download(DownloadEntry(
            id="mixed-good", url="https://example.com/y", filename="y.bin",
            save_path="C:/t", total_size=2048, added_at=stored,
        ))
        snap = db.get_download_stats(TODAY)
        self.assertEqual(snap.today.count, 1)
        self.assertEqual(snap.today.downloaded, 2048)
        self.assertEqual(snap.lifetime.count, 2)
        self.assertEqual(
            [label for label, _ in snap.series],
            [datetime.fromisoformat(stored).astimezone().date().isoformat()],
        )

    def test_the_lifetime_total_counts_every_row(self):
        db = self.fresh_db_with("bad-only", "not-a-date")
        db.add_download(DownloadEntry(
            id="good", url="https://example.com/g", filename="g.bin",
            save_path="C:/t", total_size=1024, added_at=iso(0),
        ))
        snap = db.get_download_stats(TODAY)
        self.assertEqual(snap.lifetime.count, 2)
        self.assertEqual(snap.lifetime.downloaded, 2048)


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

    def add(self, eid, days_back=0, total=1 * GB, uploaded=0, status="completed",
            hour=12, minute=0):
        self.db.add_download(DownloadEntry(
            id=eid, url=f"https://e.com/{eid}", filename=f"{eid}.bin", save_path="C:/t",
            total_size=total, downloaded_size=total, uploaded_size=uploaded,
            status=status, added_at=iso_at(days_back, hour=hour, minute=minute),
        ))

    def popup(self, speed_provider=None, **kw):
        popup = StatisticsPopup(self.db, today=TODAY, speed_provider=speed_provider, **kw)
        self.popups.append(popup)
        return popup


@pytest.mark.ui
@unittest.skipIf(IS_HEADLESS_WIN_CI, "headless Windows CI cannot create real popup widgets")
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
        # Default range is 30 days; gaps are filled so days are continuous
        self.assertEqual(len(popup._chart.days()), 30)
        active = [s for _d, s in popup._chart.days() if s.count > 0]
        self.assertEqual(len(active), 2)

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


@pytest.mark.ui
@unittest.skipIf(IS_HEADLESS_WIN_CI, "headless Windows CI cannot create real popup widgets")
class TestStatisticsPopupFailureHandling(PopupTestCase):
    """A stats view must never take the app down - but it must stay diagnosable.

    ``refresh()`` catches everything so a broken query cannot crash the app. That is only
    safe if the failure is *logged*: swallowing it silently leaves the user with a dialog
    full of em-dashes and nothing to report, which is how a stats view becomes impossible
    to diagnose after the fact.
    """

    @staticmethod
    def _broken_db():
        def boom(*_a, **_kw):
            raise RuntimeError("no such column: downloads.gone")

        return type("Broken", (), {"get_download_stats": staticmethod(boom)})()

    def test_a_broken_query_is_logged_at_warning(self):
        popup = self.popup()
        popup._db = self._broken_db()
        with self.assertLogs("my_idm.stats_dialog", level="WARNING"):
            popup.refresh()
        self.assertIsNone(popup.snapshot())

    def test_a_broken_query_renders_dashes_rather_than_raising(self):
        popup = self.popup()
        popup._db = self._broken_db()
        with self.assertLogs("my_idm.stats_dialog", level="WARNING"):
            popup.refresh()
        self.assertEqual(popup._chart.days(), ())
        self.assertEqual(
            popup._grid.itemAtPosition(0, 1).widget().text(), "—",
            "an unreadable bucket must be visibly empty, not silently wrong",
        )

    def test_recovery_after_a_transient_failure(self):
        """The next refresh must repopulate once the database is readable again."""
        popup = self.popup()
        broken = self._broken_db()
        popup._db = broken
        with self.assertLogs("my_idm.stats_dialog", level="WARNING"):
            popup.refresh()
        self.assertIsNone(popup.snapshot())
        popup._db = self.db
        popup.refresh()
        self.assertIsInstance(popup.snapshot(), StatsSnapshot)

    def test_a_broken_speed_provider_logs_and_samples_zero(self):
        def _boom():
            raise RuntimeError("engine gone")

        popup = self.popup(speed_provider=_boom)
        before = len(popup._sparkline.samples())
        with self.assertLogs("my_idm.stats_dialog", level="DEBUG"):
            popup._sample_speed()
        after = popup._sparkline.samples()
        self.assertEqual(len(after), before + 1, "one sample per call, always")
        self.assertEqual(after[-1], 0, "a failed sample reads as zero, not a crash")

    def test_a_speed_provider_returning_junk_samples_zero(self):
        popup = self.popup(speed_provider=lambda: "not a number")
        before = len(popup._sparkline.samples())
        with self.assertLogs("my_idm.stats_dialog", level="DEBUG"):
            popup._sample_speed()
        self.assertEqual(popup._sparkline.samples()[-1], 0)
        self.assertGreater(len(popup._sparkline.samples()), before)

    def test_a_speed_provider_returning_none_samples_zero(self):
        popup = self.popup(speed_provider=lambda: None)
        popup._sample_speed()
        self.assertEqual(popup._sparkline.samples()[-1], 0)

    def test_a_working_speed_provider_records_the_real_value(self):
        popup = self.popup(speed_provider=lambda: 4096)
        popup._sample_speed()
        self.assertIn(4096, popup._sparkline.samples())

    def test_a_degenerate_geometry_does_not_raise(self):
        """Width/height of zero would divide by zero in the paint code."""
        popup = self.popup()
        popup.resize(0, 0)
        popup.refresh()
        QApplication.processEvents()
        self.assertEqual(popup.snapshot(), popup.snapshot())

    def test_the_grid_is_defined_exactly_once(self):
        """A duplicated method shadows the first silently; see the module docstring."""
        import inspect

        from my_idm.stats_dialog import StatisticsPopup

        source = inspect.getsource(StatisticsPopup)
        self.assertEqual(
            source.count("def _populate_grid"), 1,
            "_populate_grid was defined twice; the first copy was dead code",
        )


@pytest.mark.ui
@unittest.skipIf(IS_HEADLESS_WIN_CI, "headless Windows CI cannot create real MainWindow")
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
        self.assertTrue(hasattr(self.window, "_act_tools_stats"))
        self.assertIn("Statistics", self.window._act_tools_stats.text())

    def test_the_menu_spells_the_label_out_in_full(self):
        """Statistics was abbreviated to "Stats…" for the toolbar strip.

        The toolbar button is gone, so the Tools menu carries the only label and spells it out -
        and it must stay distinct from the Preferences entry beside it rather than reading as
        the same feature.
        """
        self.assertEqual(self.window._act_tools_stats.text(), "Statistics…")
        self.assertEqual(self.window._act_preferences.text(), "Preferences…")

    def test_the_tools_menu_keeps_the_long_label(self):
        tools = next(
            top.menu() for top in self.window.menuBar().actions()
            if top.menu() is not None and top.text().replace("&", "") == "Tools"
        )
        texts = [a.text() for a in tools.actions()]
        self.assertIn("Statistics…", texts)
        self.assertIn("Preferences…", texts)
        self.assertIsNot(
            tools.actions()[0], self.window._act_preferences,
            "the Tools menu must use its own action, not the toolbar's, so that Ctrl,+ "
            "is registered exactly once",
        )

    def test_both_preferences_actions_open_the_same_page(self):
        """Two actions, one feature: they must not drift apart."""
        recorded = []
        original = self.window._on_open_preferences
        self.window._on_open_preferences = lambda tab: recorded.append(tab)
        try:
            self.window._act_preferences.trigger()
            self.window._act_tools_preferences.trigger()
        finally:
            self.window._on_open_preferences = original
        self.assertEqual(recorded, [TAB_GENERAL, TAB_GENERAL])

    def test_the_label_still_carries_the_full_name_in_its_tooltip(self):
        for action in (self.window._act_tools_stats, self.window._act_preferences):
            with self.subTest(action=action.text()):
                self.assertTrue(
                    action.toolTip().strip(),
                    f"{action.text()!r} needs a tooltip",
                )
        self.assertIn("Statistics", self.window._act_tools_stats.toolTip())
        self.assertIn("Configure", self.window._act_preferences.toolTip())

    def test_stats_button_on_toolbar_precedes_preferences_and_is_icon_only(self):
        """The stats button sits on the left side of Preferences on the toolbar and is icon-only."""
        actions = self.window._toolbar.actions()
        self.assertIn(self.window._act_toolbar_stats, actions)
        idx = actions.index(self.window._act_toolbar_stats)
        self.assertTrue(actions[idx + 1].isSeparator())
        self.assertEqual(actions[idx + 2], self.window._act_preferences)

        btn = self.window._toolbar.widgetForAction(self.window._act_toolbar_stats)
        self.assertIsInstance(btn, QToolButton)
        self.assertEqual(btn.toolButtonStyle(), Qt.ToolButtonStyle.ToolButtonIconOnly)

    def test_it_lives_in_the_tools_menu(self):
        tools = next(
            top.menu() for top in self.window.menuBar().actions()
            if top.menu() is not None and top.text().replace("&", "") == "Tools"
        )
        self.assertIn(self.window._act_tools_stats, tools.actions())

    def test_it_has_no_shortcut(self):
        """Ctrl+, is Preferences; a read-only view must not take a shortcut slot."""
        self.assertTrue(self.window._act_tools_stats.shortcut().isEmpty())

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
        popup = self._open()
        self.assertGreaterEqual(popup.snapshot().lifetime.count, 1)
        popup.close()

    def _open(self):
        """Open the popup and register a cleanup that tolerates its own deletion.

        ``_on_show_statistics`` sets ``WA_DeleteOnClose``, so closing the popup destroys
        the C++ object - a plain ``addCleanup(popup.deleteLater)`` would then raise
        "Internal C++ object already deleted" and mask the real assertion.
        """
        popup = self.window._on_show_statistics()
        self.addCleanup(self._safely_close, popup)
        return popup

    @staticmethod
    def _safely_close(popup):
        try:
            popup.close()
            popup.deleteLater()
        except RuntimeError:
            pass  # already destroyed by WA_DeleteOnClose

    def test_the_window_holds_a_reference_to_the_open_popup(self):
        """A popup whose only reference is a local is collectable, timer and all."""
        popup = self._open()
        self.assertIs(self.window._stats_dialog, popup)
        popup.close()

    def test_a_second_click_reuses_the_open_popup(self):
        """It is modeless and nothing deleted it, so clicks used to stack dialogs."""
        first = self._open()
        second = self.window._on_show_statistics()
        self.assertIs(
            second, first,
            "a second click built another dialog instead of raising the first",
        )
        first.close()

    def test_clicking_many_times_never_stacks_dialogs(self):
        seen = [self._open() for _ in range(5)]
        self.assertEqual(
            len({id(p) for p in seen}), 1,
            "five clicks produced more than one popup",
        )
        seen[0].close()

    def test_the_popup_is_marked_for_deletion_on_close(self):
        popup = self._open()
        self.assertTrue(
            popup.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose),
            "without this the C++ dialog outlives every click",
        )
        popup.close()

    def test_closing_the_popup_releases_the_reference(self):
        popup = self._open()
        popup.close()
        QApplication.processEvents()
        self.assertIsNone(self.window._stats_dialog)

    def test_a_fresh_popup_is_built_after_the_first_was_closed(self):
        first = self._open()
        first.close()
        QApplication.processEvents()
        second = self._open()
        self.assertIsNot(second, first)
        self.assertIs(self.window._stats_dialog, second)
        second.close()

    def test_a_destroyed_popup_does_not_wedge_the_toolbar(self):
        """A dangling C++ object must be replaced, not raised into."""
        first = self._open()
        first.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        first.close()
        # Deliberately keep the reference: `finished` cleared the window's, so this walks
        # the path where a stale wrapper is still reachable.
        self.window._stats_dialog = first
        first.deleteLater()
        QApplication.processEvents()
        fresh = self._open()
        self.assertIsNot(fresh, first)
        self.assertIs(self.window._stats_dialog, fresh)
        fresh.close()

    def test_shutting_the_window_closes_the_popup(self):
        """Its 1 Hz timer must not outlive the manager it samples."""
        popup = self._open()
        popup.start_speed_timer()
        self.assertIsNotNone(popup._timer)
        self.window._force_exit = True
        self.window.close()
        QApplication.processEvents()
        self.assertIsNone(self.window._stats_dialog)
        self.assertIsNone(popup._timer, "the popup timer survived the shutdown")
        self.window._force_exit = False


@pytest.mark.ui
@unittest.skipIf(IS_HEADLESS_WIN_CI, "headless Windows CI cannot create real popup widgets")
class TestChartSelectors(PopupTestCase):
    """Range and granularity are independent questions and get independent pickers."""

    def test_both_pickers_exist(self):
        popup = self.popup()
        self.assertEqual(popup._range_combo.count(), len(StatisticsPopup.RANGES))
        self.assertEqual(popup._bucket_combo.count(), len(StatisticsPopup.BUCKETS))

    def test_the_offered_ranges(self):
        self.assertEqual(
            [label for label, _days in StatisticsPopup.RANGES],
            ["Today", "Last 7 days", "Last 30 days", "Last 12 months", "All time"],
        )

    def test_the_offered_granularities(self):
        self.assertEqual(
            [key for _label, key in StatisticsPopup.BUCKETS],
            ["5min", "hour", "day", "month"],
        )

    def test_the_default_is_thirty_days_by_day(self):
        popup = self.popup()
        self.assertEqual(popup._range_combo.currentIndex(), 2)
        self.assertEqual(popup._range_combo.currentText(), "Last 30 days")
        self.assertEqual(popup.bucket_selection(), "day")
        self.assertEqual(popup.snapshot().since, (TODAY - timedelta(days=29)).isoformat())

    def test_picking_today_narrows_the_series(self):
        self.add("old", 1, total=1 * GB)
        self.add("new", 0, total=1 * GB)
        popup = self.popup()
        self.assertEqual(len(popup.snapshot().series), 30)
        popup._range_combo.setCurrentIndex(0)
        self.assertEqual(popup.snapshot().since, TODAY.isoformat())
        self.assertEqual(len(popup.snapshot().series), 1)
        self.assertEqual(sum(s.count for _d, s in popup.snapshot().series), 1)

    def test_picking_last_seven_days_narrows_the_series(self):
        self.add("old", 20, total=1 * GB)
        self.add("new", 2, total=1 * GB)
        popup = self.popup()
        self.assertEqual(len(popup.snapshot().series), 30)
        popup._range_combo.setCurrentIndex(1)
        self.assertEqual(popup.snapshot().since, (TODAY - timedelta(days=6)).isoformat())
        self.assertEqual(
            len(popup.snapshot().series), 7,
            "a 20-day-old download must fall outside a 7-day chart",
        )
        self.assertEqual(sum(s.count for _d, s in popup.snapshot().series), 1)

    def test_all_time_has_no_lower_bound(self):
        self.add("ancient", 900, total=1 * GB)
        popup = self.popup()
        popup._range_combo.setCurrentIndex(4)
        self.assertEqual(popup.snapshot().since, "", "all time means no cut-off at all")
        self.assertEqual(len(popup.snapshot().series), 901)
        self.assertEqual(sum(s.count for _d, s in popup.snapshot().series), 1)

    def test_changing_the_range_refreshes_the_chart(self):
        self.add("a", 0, total=1 * GB)
        popup = self.popup()
        popup._range_combo.setCurrentIndex(1)
        self.assertEqual(len(popup._chart.days()), len(popup.snapshot().series))

    def test_switching_to_per_month_merges_the_bars(self):
        for days in (0, 1, 2, 40):
            self.add(f"d{days}", days, total=1 * GB)
        popup = self.popup()
        popup._range_combo.setCurrentIndex(4)
        popup._bucket_combo.setCurrentIndex(
            popup._bucket_combo.findData("month")
        )
        snap = popup.snapshot()
        self.assertEqual(snap.bucket, "month")
        self.assertEqual([m for m, _s in snap.series], ["2026-08", "2026-09"])
        self.assertEqual(popup._chart.bucket(), "month", "the chart is told the granularity")

    def test_switching_to_per_hour_groups_by_hour(self):
        self.add("h1", 0, total=1 * GB, hour=9)
        self.add("h2", 0, total=1 * GB, hour=14)
        popup = self.popup()
        popup._range_combo.setCurrentIndex(0)
        popup._bucket_combo.setCurrentIndex(popup._bucket_combo.findData("hour"))
        snap = popup.snapshot()
        self.assertEqual(snap.bucket, "hour")
        self.assertEqual(len(snap.series), 24)
        self.assertEqual(popup._chart.bucket(), "hour")
        self.assertEqual(sum(s.count for _h, s in snap.series), 2)

    def test_switching_to_per_5min_groups_by_5min(self):
        self.add("m1", 0, total=1 * GB, hour=9, minute=10)
        self.add("m2", 0, total=1 * GB, hour=9, minute=20)
        popup = self.popup()
        popup._range_combo.setCurrentIndex(0)
        popup._bucket_combo.setCurrentIndex(popup._bucket_combo.findData("5min"))
        snap = popup.snapshot()
        self.assertEqual(snap.bucket, "5min")
        self.assertEqual(len(snap.series), 288)
        self.assertEqual(popup._chart.bucket(), "5min")
        self.assertEqual(sum(s.count for _m, s in snap.series), 2)

    def test_switching_the_granularity_alone_is_enough(self):
        for days in (0, 1, 40):
            self.add(f"d{days}", days, total=1 * GB)
        popup = self.popup()
        popup._range_combo.setCurrentIndex(4)
        popup._bucket_combo.setCurrentIndex(popup._bucket_combo.findData("month"))
        self.assertEqual(len(popup.snapshot().series), 2)

    def test_the_pickers_survive_a_refresh(self):
        popup = self.popup()
        popup._range_combo.setCurrentIndex(1)
        popup._bucket_combo.setCurrentIndex(popup._bucket_combo.findData("month"))
        popup.refresh()
        self.assertEqual(popup._range_combo.currentIndex(), 1)
        self.assertEqual(popup.bucket_selection(), "month")

    def test_an_out_of_range_initial_index_falls_back(self):
        popup = self.popup(range_index=99)
        self.assertEqual(popup._range_combo.currentIndex(), 2)
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

    def test_an_hour_bucket_paints(self):
        chart = StatsChartWidget()
        chart.resize(520, 220)
        chart.set_days([("2026-09-30 14", DownloadStats(1, 100, 0, 1))], bucket="hour")
        self.addCleanup(chart.deleteLater)

    def test_a_minute_bucket_paints(self):
        chart = StatsChartWidget()
        chart.resize(520, 220)
        chart.set_days([("2026-09-30 14:35", DownloadStats(1, 100, 0, 1))], bucket="minute")
        self.addCleanup(chart.deleteLater)

    def test_a_5min_bucket_paints(self):
        chart = StatsChartWidget()
        chart.resize(520, 220)
        chart.set_days([("2026-09-30 14:35", DownloadStats(1, 100, 0, 1))], bucket="5min")
        self.addCleanup(chart.deleteLater)
        paint(chart)

    def test_a_dense_5min_seven_days_paints_without_error(self):
        from my_idm.database import fill_series_gaps
        chart = StatsChartWidget()
        chart.resize(520, 220)
        since = TODAY - timedelta(days=6)
        filled = fill_series_gaps([("2026-09-30 14:35", DownloadStats(1, 100, 0, 1))], "5min", start=since, end=TODAY)
        chart.set_days(filled, bucket="5min")
        self.addCleanup(chart.deleteLater)
        paint(chart)

    def test_the_tooltip_names_the_right_unit(self):
        self.assertIn("day of month", self._chart("day").toolTip())
        self.assertIn("month", self._chart("month").toolTip())
        self.assertNotIn("day of month", self._chart("month").toolTip())
        chart_hr = StatsChartWidget()
        chart_hr.set_days([], bucket="hour")
        self.assertIn("hour", chart_hr.toolTip())
        chart_hr.deleteLater()
        chart_min = StatsChartWidget()
        chart_min.set_days([], bucket="minute")
        self.assertIn("minute", chart_min.toolTip())
        chart_min.deleteLater()
        chart_5min = StatsChartWidget()
        chart_5min.set_days([], bucket="5min")
        self.assertIn("5 minutes", chart_5min.toolTip())
        chart_5min.deleteLater()


class TestStatsGapFilling(StatsTestCase):
    """Gaps in the time series must be filled with zeroes rather than skipped."""

    def test_day_series_fills_seven_days(self):
        self.add("d1", 6, total=1 * GB)
        self.add("d2", 0, total=2 * GB)
        since = TODAY - timedelta(days=6)
        snap = self.db.get_download_stats(TODAY, since=since, bucket="day", fill_gaps=True)
        self.assertEqual(len(snap.series), 7)
        self.assertEqual(snap.series[0][0], since.strftime("%Y-%m-%d"))
        self.assertEqual(snap.series[-1][0], TODAY.strftime("%Y-%m-%d"))
        self.assertEqual(snap.series[0][1].count, 1)
        self.assertEqual(snap.series[-1][1].count, 1)
        self.assertEqual(sum(s.count for _, s in snap.series), 2)

    def test_hour_series_fills_seven_days(self):
        self.add("h1", 6, total=1 * GB, hour=10)
        self.add("h2", 0, total=2 * GB, hour=14)
        since = TODAY - timedelta(days=6)
        snap = self.db.get_download_stats(TODAY, since=since, bucket="hour", fill_gaps=True)
        self.assertEqual(len(snap.series), 7 * 24)
        active = [s for _, s in snap.series if s.count > 0]
        self.assertEqual(len(active), 2)

    def test_5min_series_fills_seven_days(self):
        self.add("m1", 6, total=1 * GB, hour=10, minute=15)
        self.add("m2", 0, total=2 * GB, hour=14, minute=20)
        since = TODAY - timedelta(days=6)
        snap = self.db.get_download_stats(TODAY, since=since, bucket="5min", fill_gaps=True)
        self.assertEqual(len(snap.series), 7 * 288)
        active = [s for _, s in snap.series if s.count > 0]
        self.assertEqual(len(active), 2)

    def test_5min_series_fills_today(self):
        self.add("m1", 0, total=1 * GB, hour=10, minute=15)
        snap = self.db.get_download_stats(TODAY, since=TODAY, bucket="5min", fill_gaps=True)
        self.assertEqual(len(snap.series), 288)
        active = [s for _, s in snap.series if s.count > 0]
        self.assertEqual(len(active), 1)

    def test_hour_series_fills_today(self):
        self.add("h1", 0, total=1 * GB, hour=10)
        snap = self.db.get_download_stats(TODAY, since=TODAY, bucket="hour", fill_gaps=True)
        self.assertEqual(len(snap.series), 24)
        active = [s for _, s in snap.series if s.count > 0]
        self.assertEqual(len(active), 1)

    def test_empty_database_with_range_fills_all_zeroes(self):
        since = TODAY - timedelta(days=6)
        snap = self.db.get_download_stats(TODAY, since=since, bucket="day", fill_gaps=True)
        self.assertEqual(len(snap.series), 7)
        self.assertTrue(all(s.count == 0 for _, s in snap.series))



class TestTransferredBytesStatistics(StatsTestCase):
    def test_download_queued_in_past_tracks_bytes_transferred_today(self):
        """A download added 5 days ago, but transferred today, counts its bytes under today."""
        # Added 5 days ago with 0 downloaded size initially
        self.db.add_download(DownloadEntry(
            id="old_item",
            url="https://example.com/big.iso",
            filename="big.iso",
            save_path="C:/t",
            total_size=10 * GB,
            downloaded_size=0,
            status="downloading",
            added_at=iso(5),
        ))

        # Bytes actually transferred today
        now_today = datetime.combine(TODAY, time(14, 0))
        self.db.record_bandwidth("default", downloaded_bytes=500 * MB, uploaded_bytes=50 * MB, now=now_today)

        snap = self.db.get_download_stats(TODAY, since=TODAY - timedelta(days=6), bucket="day")

        # Today's bucket should show the 500 MB downloaded today
        self.assertEqual(snap.today.downloaded, 500 * MB)
        self.assertEqual(snap.today.uploaded, 50 * MB)
        # Count of downloads added today is 0 (it was added 5 days ago)
        self.assertEqual(snap.today.count, 0)
        # Lifetime has 1 download and 500 MB downloaded
        self.assertEqual(snap.lifetime.count, 1)
        self.assertEqual(snap.lifetime.downloaded, 500 * MB)

        # In daily series, day 5 ago has count 1 but 0 bytes; today has count 0 but 500 MB bytes
        series_map = {day: s for day, s in snap.series}
        day_5_ago = (TODAY - timedelta(days=5)).isoformat()
        day_today = TODAY.isoformat()

        self.assertIn(day_5_ago, series_map)
        self.assertEqual(series_map[day_5_ago].count, 1)
        self.assertEqual(series_map[day_5_ago].downloaded, 0)

        self.assertIn(day_today, series_map)
        self.assertEqual(series_map[day_today].count, 0)
        self.assertEqual(series_map[day_today].downloaded, 500 * MB)
        self.assertEqual(series_map[day_today].uploaded, 50 * MB)

    def test_bandwidth_history_5min_and_hourly_bucketing(self):
        """Transfers at distinct times land in their respective 5min and hour buckets."""
        dt1 = datetime.combine(TODAY, time(10, 12))  # 5-min bucket 10:10, hour 10
        dt2 = datetime.combine(TODAY, time(10, 48))  # 5-min bucket 10:45, hour 10
        dt3 = datetime.combine(TODAY, time(14, 5))   # 5-min bucket 14:05, hour 14

        self.db.record_bandwidth("default", downloaded_bytes=100 * MB, now=dt1)
        self.db.record_bandwidth("default", downloaded_bytes=200 * MB, now=dt2)
        self.db.record_bandwidth("default", downloaded_bytes=300 * MB, now=dt3)

        # Check 5min series
        snap_5m = self.db.get_download_stats(TODAY, since=TODAY, bucket="5min")
        series_5m = {b: s.downloaded for b, s in snap_5m.series if s.downloaded > 0}
        b1 = f"{TODAY.isoformat()} 10:10"
        b2 = f"{TODAY.isoformat()} 10:45"
        b3 = f"{TODAY.isoformat()} 14:05"
        self.assertEqual(series_5m.get(b1), 100 * MB)
        self.assertEqual(series_5m.get(b2), 200 * MB)
        self.assertEqual(series_5m.get(b3), 300 * MB)

        # Check hour series
        snap_hr = self.db.get_download_stats(TODAY, since=TODAY, bucket="hour")
        series_hr = {b: s.downloaded for b, s in snap_hr.series if s.downloaded > 0}
        h1 = f"{TODAY.isoformat()} 10"
        h2 = f"{TODAY.isoformat()} 14"
        self.assertEqual(series_hr.get(h1), 300 * MB)  # 100 + 200 MB in hour 10
        self.assertEqual(series_hr.get(h2), 300 * MB)  # 300 MB in hour 14

    def test_backfill_bandwidth_history_from_legacy_downloads(self):
        """When opening a database that has existing downloads, bandwidth_history is populated."""
        db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_file.close()
        try:
            # 1. Create a DB and add downloads without bandwidth_history
            db1 = Database(db_file.name)
            db1.open()
            db1.add_download(DownloadEntry(
                id="leg1",
                url="https://example.com/leg1.bin",
                filename="leg1.bin",
                save_path="C:/t",
                total_size=1 * GB,
                downloaded_size=1 * GB,
                status="completed",
                added_at=iso(2),
            ))
            # Delete any bandwidth_history rows to simulate legacy pre-migration DB
            db1._conn.execute("DELETE FROM bandwidth_history")
            db1._conn.commit()
            db1.close()

            # 2. Re-open DB — _init_db will trigger _backfill_bandwidth_history_if_empty
            db2 = Database(db_file.name)
            db2.open()
            try:
                count = db2._conn.execute("SELECT COUNT(*) FROM bandwidth_history").fetchone()[0]
                self.assertGreater(count, 0, "bandwidth_history should be backfilled from existing downloads")
                snap = db2.get_download_stats(TODAY)
                self.assertEqual(snap.lifetime.downloaded, 1 * GB)
            finally:
                db2.close()
        finally:
            os.unlink(db_file.name)


if __name__ == "__main__":
    unittest.main()
