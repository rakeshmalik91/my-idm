"""The Statistics popup: totals grid, daily-volume chart, and a live speed sparkline.

Read-only by design. Nothing here writes to the database - see
``docs/architecture/statistics.md`` for the design and its known limits.

The two charts are drawn with ``QPainter`` rather than a plotting library.
``matplotlib`` and ``numpy`` happen to be importable in this environment but are absent
from ``requirements.txt`` - they arrive transitively, which is not a contract - and
everything else in this codebase draws its own widgets the same way.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional, Sequence

import humanize
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from my_idm.database import Database, DownloadStats, StatsSnapshot
from my_idm.styles import Colors

log = logging.getLogger(__name__)


def _fmt_bytes(value: int) -> str:
    """Short human size for the grid: '1.2 GB'. Exact counts live in the tooltip."""
    return humanize.naturalsize(max(0, int(value)), binary=True)


def _fmt_rate(bytes_per_second: int) -> str:
    if bytes_per_second <= 0:
        return "0 B/s"
    return humanize.naturalsize(bytes_per_second, binary=True) + "/s"


class StatsChartWidget(QWidget):
    """Stacked per-day downloaded/uploaded bars.

    Days with no activity are drawn at zero width rather than skipped: dropping them
    compresses the time axis and makes a quiet week look like a busy one.
    """

    AXIS_LEFT = 78
    AXIS_BOTTOM = 20
    AXIS_TOP = 8
    AXIS_RIGHT = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._days: tuple[tuple[str, DownloadStats], ...] = ()
        self._bucket = "day"
        self._cumulative = False
        self.setMinimumHeight(190)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._update_tooltip()

    def set_cumulative(self, cumulative: bool) -> None:
        cum = bool(cumulative)
        if self._cumulative != cum:
            self._cumulative = cum
            self._update_tooltip()
            self.update()

    def cumulative(self) -> bool:
        return self._cumulative

    def _update_tooltip(self) -> None:
        if self._bucket == "5min":
            unit = "5 minutes"
        elif self._bucket == "minute":
            unit = "minute"
        elif self._bucket == "hour":
            unit = "hour"
        elif self._bucket == "month":
            unit = "month"
        else:
            unit = "day of month"
        mode = "Cumulative volume" if self._cumulative else "Grouped"
        self.setToolTip(
            f"{mode} by {unit}. Tracks actual bytes transferred during each interval "
            "(instead of only when downloads were added)."
        )

    def set_days(self, days: Sequence[tuple[str, DownloadStats]], bucket: str = "day") -> None:
        self._days = tuple(days)
        self._bucket = bucket if bucket in ("5min", "minute", "hour", "day", "month") else "day"
        self._update_tooltip()
        self.update()

    def days(self):
        return self._days

    def display_days(self) -> tuple[tuple[str, DownloadStats], ...]:
        if not self._cumulative:
            return self._days
        result = []
        cum_down = 0
        cum_up = 0
        cum_count = 0
        cum_completed = 0
        for day, stats in self._days:
            cum_down += stats.downloaded
            cum_up += stats.uploaded
            cum_count += stats.count
            cum_completed += stats.completed
            result.append(
                (
                    day,
                    DownloadStats(
                        count=cum_count,
                        downloaded=cum_down,
                        uploaded=cum_up,
                        completed=cum_completed,
                    ),
                )
            )
        return tuple(result)

    def bucket(self) -> str:
        return self._bucket

    # -- painting -----------------------------------------------------------

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self.AXIS_LEFT,
            self.AXIS_TOP,
            max(1.0, self.width() - self.AXIS_LEFT - self.AXIS_RIGHT),
            max(1.0, self.height() - self.AXIS_TOP - self.AXIS_BOTTOM),
        )

    @staticmethod
    def _tick_step(peak: int) -> int:
        """A round number near 1/4 of the peak, so the axis has 3-4 readable labels."""
        if peak <= 0:
            return 1
        step = peak / 4.0
        magnitude = 10 ** max(0, len(str(int(step))) - 1)
        for factor in (1, 2, 5, 10):
            if magnitude * factor >= step:
                return int(magnitude * factor)
        return int(magnitude * 10)

    def paintEvent(self, event):  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(Colors.BG_MID))

        plot = self._plot_rect()
        painter.setFont(QFont(self.font().family(), 8))
        metrics = QFontMetrics(painter.font())

        if not self._days:
            painter.setPen(QColor(Colors.TEXT_MUTED))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "No downloads recorded yet"
            )
            painter.end()
            return

        display_days = self.display_days()
        peak = 0
        for _day, stats in display_days:
            peak = max(peak, stats.downloaded + stats.uploaded)
        if peak <= 0:
            peak = 1

        # Gridlines plus their labels.
        step = self._tick_step(peak)
        painter.setPen(QPen(QColor(Colors.BORDER), 1, Qt.PenStyle.DotLine))
        value = 0
        while value <= peak:
            y = plot.bottom() - (value / peak) * plot.height()
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QColor(Colors.TEXT_MUTED))
            painter.drawText(
                QRectF(0, y - 8, self.AXIS_LEFT - 6, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                _fmt_bytes(value),
            )
            painter.setPen(QPen(QColor(Colors.BORDER), 1, Qt.PenStyle.DotLine))
            value += step

        slot = plot.width() / len(display_days)
        bar_width = max(1.0, slot * 0.68)
        # Sparse ticks: label roughly six dates regardless of the range.
        label_every = max(1, len(display_days) // 6)
        same_day = len(display_days) > 0 and (display_days[0][0][:10] == display_days[-1][0][:10])

        downloaded_brush = QColor(Colors.ACCENT)
        uploaded_brush = QColor(Colors.GREEN)

        for index, (day, stats) in enumerate(display_days):
            x = plot.left() + index * slot + (slot - bar_width) / 2.0
            total = stats.downloaded + stats.uploaded
            if total > 0:
                height = (total / peak) * plot.height()
                up_height = (stats.uploaded / total) * height if total else 0.0
                if stats.downloaded > 0:
                    painter.fillRect(
                        QRectF(x, plot.bottom() - height, bar_width, height - up_height),
                        downloaded_brush,
                    )
                if stats.uploaded > 0:
                    painter.fillRect(
                        QRectF(x, plot.bottom() - up_height, bar_width, up_height),
                        uploaded_brush,
                    )
            if index % label_every == 0:
                painter.setPen(QColor(Colors.TEXT_MUTED))
                if self._bucket == "month":
                    label = day
                elif self._bucket == "hour":
                    hour_part = day[11:13] if len(day) >= 13 else day
                    date_part = day[5:10] if len(day) >= 10 else day
                    label = f"{hour_part}:00" if same_day else f"{date_part} {hour_part}h"
                elif self._bucket in ("5min", "minute"):
                    min_part = day[11:16] if len(day) >= 16 else day
                    date_part = day[5:10] if len(day) >= 10 else day
                    label = min_part if same_day else f"{date_part} {min_part}"
                else:
                    label = day[5:] if len(day) >= 5 else day
                tick_x = plot.left() + index * slot + slot / 2.0
                label_w = 64.0
                painter.drawText(
                    QRectF(tick_x - label_w / 2.0, plot.bottom() + 2, label_w, 16),
                    Qt.AlignmentFlag.AlignCenter, label,
                )

        painter.end()


class SparklineWidget(QWidget):
    """Live aggregate speed, sampled in memory while the popup is open.

    Nothing is persisted: closing the popup discards the history. That is deliberate -
    a sampled table would need a schema migration and a retention policy, which is out of
    scope for the first cut (see the design doc).
    """

    MAX_SAMPLES = 120

    def __init__(self, parent=None):
        super().__init__(parent)
        self._samples: list[int] = []
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def add_sample(self, bytes_per_second: int) -> None:
        self._samples.append(max(0, int(bytes_per_second)))
        if len(self._samples) > self.MAX_SAMPLES:
            del self._samples[: len(self._samples) - self.MAX_SAMPLES]
        self.update()

    def samples(self) -> list[int]:
        return list(self._samples)

    def peak(self) -> int:
        return max(self._samples) if self._samples else 0

    def clear(self) -> None:
        self._samples.clear()
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(Colors.BG_MID))

        peak = self.peak()
        if peak <= 0 or len(self._samples) < 2:
            painter.setPen(QColor(Colors.TEXT_MUTED))
            painter.setFont(QFont(self.font().family(), 9))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter,
                "Collecting speed samples…"
                if not self._samples else "0 B/s",
            )
            painter.end()
            return

        # Scale to a little above the peak, so a *constant* rate draws as a line across the
        # middle rather than a solid block pinned to the ceiling - which reads as a broken
        # widget rather than a steady transfer.
        ceiling = peak * 1.15
        points = [
            QPointF(
                self.width() * index / (len(self._samples) - 1),
                self.height() - (value / ceiling) * (self.height() - 6) - 3,
            )
            for index, value in enumerate(self._samples)
        ]
        painter.setPen(QPen(QColor(Colors.ACCENT), 1.6))
        painter.setBrush(QColor(Colors.ACCENT))
        polygon = points + [
            QPointF(points[-1].x(), self.height()),
            QPointF(points[0].x(), self.height()),
        ]
        painter.drawPolygon(polygon)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolyline(points)

        painter.setPen(QColor(Colors.TEXT_MUTED))
        painter.setFont(QFont(self.font().family(), 8))
        painter.drawText(
            QRectF(4, 2, self.width() - 8, 14),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            f"peak {_fmt_rate(peak)}",
        )
        painter.end()


class StatisticsPopup(QDialog):
    """Totals for today / week / month / year / all time, plus the two charts.

    The summary grid is fixed - those five rows are the headline numbers and are not
    redefined by a chart selection. The chart has its own range and granularity pickers,
    because "show me the last year" and "show me per month" are independent questions and
    forcing one range for both would make the chart useless at either extreme.

    The window itself is persisted through ``Database.set_ui_state`` so it reopens where
    the user left it, matching every other dialog in the app.
    """

    UI_STATE_KEY = "statistics_dialog_size"

    #: (label, days back or range key, or None for all time)
    RANGES = (
        ("Today", 0),
        ("This week", "this_week"),
        ("Last week", "last_week"),
        ("Last 7 days", 6),
        ("This month", "this_month"),
        ("Last month", "last_month"),
        ("Last 30 days", 29),
        ("This quarter", "this_quarter"),
        ("Last quarter", "last_quarter"),
        ("Last 90 days", 89),
        ("This year", "this_year"),
        ("Last year", "last_year"),
        ("Last 12 months", 364),
        ("All time", None),
    )
    DEFAULT_RANGE_INDEX = 6  # "Last 30 days"

    BUCKETS = (
        ("Per 5 minutes", "5min"),
        ("Per hour", "hour"),
        ("Per day", "day"),
        ("Per month", "month"),
    )

    def __init__(self, db: Database, parent=None, today: Optional[date] = None,
                 speed_provider=None, range_index: int = DEFAULT_RANGE_INDEX, bucket: str = "day",
                 cumulative: bool = False):
        super().__init__(parent)
        self._db = db
        self._today = today
        self._speed_provider = speed_provider
        self._snapshot: Optional[StatsSnapshot] = None
        self._timer: Optional[QTimer] = None
        self._range_index = range_index
        self._cumulative = bool(cumulative)
        valid_buckets = dict(self.BUCKETS).values()
        if bucket == "minute":
            self._bucket = "5min"
        elif bucket in valid_buckets:
            self._bucket = bucket
        else:
            self._bucket = "day"

        self.setWindowTitle("📊  Statistics")
        self.setMinimumWidth(680)
        self._build_ui()
        self._restore_size()
        self.refresh()

    # -- construction -------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(14, 14, 14, 12)

        grid_host = QWidget()
        self._grid = QGridLayout(grid_host)
        self._grid.setHorizontalSpacing(18)
        self._grid.setVerticalSpacing(6)
        self._grid.setColumnStretch(1, 1)
        header = QLabel("<b>Totals</b>")
        root.addWidget(header)
        root.addWidget(grid_host)

        # -- chart controls --------------------------------------------------
        controls = QHBoxLayout()
        self._volume_header = QLabel("<b>Volume</b>")
        controls.addWidget(self._volume_header)
        controls.addStretch()

        controls.addWidget(QLabel("Range:"))
        self._range_combo = QComboBox()
        for label, _spec in self.RANGES:
            self._range_combo.addItem(label)
        self._range_combo.setCurrentIndex(
            self._range_index if 0 <= self._range_index < len(self.RANGES) else self.DEFAULT_RANGE_INDEX
        )
        self._range_combo.currentIndexChanged.connect(self._on_range_changed)
        controls.addWidget(self._range_combo)

        controls.addWidget(QLabel("Group by:"))
        self._bucket_combo = QComboBox()
        for label, key in self.BUCKETS:
            self._bucket_combo.addItem(label, key)
        self._bucket_combo.setCurrentIndex(
            max(0, self._bucket_combo.findData(self._bucket))
        )
        self._bucket_combo.currentIndexChanged.connect(self._on_bucket_changed)
        controls.addWidget(self._bucket_combo)

        self._cumulative_btn = QPushButton("📈 Cumulative")
        self._cumulative_btn.setCheckable(True)
        self._cumulative_btn.setChecked(self._cumulative)
        self._cumulative_btn.setToolTip("Toggle cumulative volume (accumulated running totals)")
        self._cumulative_btn.toggled.connect(self._on_cumulative_toggled)
        controls.addWidget(self._cumulative_btn)
        root.addLayout(controls)

        self._chart = StatsChartWidget()
        self._chart.set_cumulative(self._cumulative)
        root.addWidget(self._chart, 1)

        legend = QLabel(
            f'<span style="color:{Colors.ACCENT}">■</span> downloaded&nbsp;&nbsp;'
            f'<span style="color:{Colors.GREEN}">■</span> uploaded'
        )
        root.addWidget(legend)

        speed_header = QLabel("<b>Current speed</b>")
        root.addWidget(speed_header)
        self._sparkline = SparklineWidget()
        root.addWidget(self._sparkline)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self._refresh_btn = QPushButton("🔄  Refresh")
        self._refresh_btn.clicked.connect(self.refresh)
        buttons.addWidget(self._refresh_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        buttons.addWidget(close_btn)
        root.addLayout(buttons)

    # -- chart selection ----------------------------------------------------

    def range_selection(self):
        """The chosen range as ``(label, spec)``."""
        return self.RANGES[max(0, self._range_combo.currentIndex())]

    def bucket_selection(self) -> str:
        return self._bucket_combo.currentData() or "day"

    def _date_bounds_for_range(self) -> tuple[Optional[date], Optional[date]]:
        """Compute the (since, until) dates for the selected range."""
        _label, spec = self.range_selection()
        today = self._resolve_today()
        if spec is None:
            return None, None
        if isinstance(spec, int):
            return today - timedelta(days=spec), today
        if spec == "this_week":
            return today - timedelta(days=today.weekday()), today
        if spec == "last_week":
            mon = today - timedelta(days=today.weekday() + 7)
            sun = today - timedelta(days=today.weekday() + 1)
            return mon, sun
        if spec == "this_month":
            return today.replace(day=1), today
        if spec == "last_month":
            first_of_this_month = today.replace(day=1)
            last_of_prev_month = first_of_this_month - timedelta(days=1)
            return last_of_prev_month.replace(day=1), last_of_prev_month
        if spec == "this_quarter":
            q_month = ((today.month - 1) // 3) * 3 + 1
            return today.replace(month=q_month, day=1), today
        if spec == "last_quarter":
            q_month = ((today.month - 1) // 3) * 3 + 1
            first_of_this_q = today.replace(month=q_month, day=1)
            last_of_prev_q = first_of_this_q - timedelta(days=1)
            prev_q_month = ((last_of_prev_q.month - 1) // 3) * 3 + 1
            return last_of_prev_q.replace(month=prev_q_month, day=1), last_of_prev_q
        if spec == "this_year":
            return date(today.year, 1, 1), today
        if spec == "last_year":
            return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
        return today - timedelta(days=29), today

    def _since_for_range(self) -> Optional[date]:
        since, _until = self._date_bounds_for_range()
        return since

    def _until_for_range(self) -> Optional[date]:
        _since, until = self._date_bounds_for_range()
        return until

    def _on_range_changed(self, _index: int) -> None:
        self._range_index = self._range_combo.currentIndex()
        self.refresh()

    def _on_bucket_changed(self, _index: int) -> None:
        self._bucket = self.bucket_selection()
        self.refresh()

    def is_cumulative(self) -> bool:
        return self._cumulative

    def set_cumulative(self, cumulative: bool) -> None:
        self._cumulative = bool(cumulative)
        if hasattr(self, "_cumulative_btn"):
            self._cumulative_btn.setChecked(self._cumulative)
        if hasattr(self, "_volume_header"):
            self._volume_header.setText(
                "<b>Volume (Cumulative)</b>" if self._cumulative else "<b>Volume</b>"
            )
        if hasattr(self, "_chart"):
            self._chart.set_cumulative(self._cumulative)

    def _on_cumulative_toggled(self, checked: bool) -> None:
        self.set_cumulative(checked)
        self._save_size()

    # -- data ---------------------------------------------------------------

    def _resolve_today(self) -> date:
        return self._today or datetime.now().date()

    def snapshot(self) -> Optional[StatsSnapshot]:
        return self._snapshot

    def refresh(self) -> None:
        """Re-read every bucket and repaint.

        The query is a handful of ``GROUP BY`` passes over the downloads table, which is
        sub-millisecond at the row counts a real library reaches, so it runs inline on the
        Qt thread. If that ever stops being true the fix is to move this onto a
        ``QThreadPool`` job - the API below would not change, because ``today`` is already
        injected.

        A failure renders an em-dash in every cell rather than propagating, but it is
        *logged*: swallowing a broken query silently leaves the user with an empty dialog
        and nothing to report, which is how a stats view becomes impossible to diagnose.
        """
        since = self._since_for_range()
        until = self._until_for_range()
        bucket = self.bucket_selection()
        try:
            snap = self._db.get_download_stats(
                self._resolve_today(), since=since, bucket=bucket, fill_gaps=True, until=until
            )
        except Exception:
            log.warning("Could not read download statistics", exc_info=True)
            snap = None
        self._snapshot = snap
        self._populate_grid()
        self._chart.set_cumulative(self._cumulative)
        self._chart.set_days(snap.series if snap else (), bucket=bucket)
        self._sample_speed()

    def _populate_grid(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        snap = self._snapshot
        rows = (
            ("Today", snap.today if snap else None),
            ("This week", snap.week if snap else None),
            ("This month", snap.month if snap else None),
            ("This year", snap.year if snap else None),
            ("All time", snap.lifetime if snap else None),
        )
        for index, (label, stats) in enumerate(rows):
            name = QLabel(label)
            name.setStyleSheet(f"color: {Colors.TEXT_MUTED};")
            self._grid.addWidget(name, index, 0)
            if stats is None:
                self._grid.addWidget(QLabel("—"), index, 1)
                continue
            value = QLabel(f"{_fmt_bytes(stats.downloaded)} down  ·  {_fmt_bytes(stats.uploaded)} up")
            value.setToolTip(
                f"downloaded: {stats.downloaded:,} bytes\n"
                f"uploaded: {stats.uploaded:,} bytes\n"
                f"files: {stats.count}  ·  completed: {stats.completed}"
            )
            self._grid.addWidget(value, index, 1)

        total = QLabel(
            f"<b>{snap.lifetime.completed:,} files completed</b>" if snap else "<b>—</b>"
        )
        total.setStyleSheet(f"color: {Colors.ACCENT};")
        self._grid.addWidget(total, len(rows), 0, 1, 1)

    def _sample_speed(self) -> None:
        if self._speed_provider is None:
            return
        try:
            self._sparkline.add_sample(int(self._speed_provider() or 0))
        except Exception:
            # One dropped sample is not worth a dialog or a stack trace; the next tick
            # samples again. A permanently broken provider shows as a flat line at zero.
            log.debug("Speed sample failed", exc_info=True)
            self._sparkline.add_sample(0)

    # -- live speed timer ----------------------------------------------------

    def start_speed_timer(self, interval_ms: int = 1000) -> None:
        if self._speed_provider is None or self._timer is not None:
            return
        timer = QTimer(self)
        timer.timeout.connect(self._sample_speed)
        timer.start(interval_ms)
        self._timer = timer

    def stop_speed_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    # -- window geometry -----------------------------------------------------

    def _restore_size(self) -> None:
        state = self._db.get_ui_state(self.UI_STATE_KEY, None) if self._db else None
        if isinstance(state, dict):
            try:
                self.resize(int(state.get("width", 680)), int(state.get("height", 560)))
            except (TypeError, ValueError):
                self.resize(680, 560)
            if "cumulative" in state:
                self.set_cumulative(bool(state.get("cumulative")))
            return
        self.resize(680, 560)

    def _save_size(self) -> None:
        if not self._db:
            return
        try:
            self._db.set_ui_state(
                self.UI_STATE_KEY,
                {
                    "width": self.width(),
                    "height": self.height(),
                    "cumulative": self._cumulative,
                },
            )
        except Exception:
            pass

    # -- lifecycle -----------------------------------------------------------

    def showEvent(self, event):  # noqa: N802 - Qt naming
        super().showEvent(event)
        self.start_speed_timer()

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        # Stop the timer before the dialog is destroyed: a QTimer that outlives its
        # receiver is a use-after-free waiting to happen.
        self.stop_speed_timer()
        self._save_size()
        super().closeEvent(event)

    def reject(self) -> None:
        self.stop_speed_timer()
        self._save_size()
        super().reject()
