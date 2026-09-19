"""Custom delegates for the download table view."""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRect, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionProgressBar,
    QStyleOptionViewItem,
)

from my_idm.styles import Colors


class ProgressBarDelegate(QStyledItemDelegate):
    """Renders a styled progress bar directly in a QTableView cell.

    Expects the model to return a dict from data():
      {"progress": 0-100, "status": "downloading"|"paused"|...}
    """

    _STATUS_COLORS = {
        "downloading":       QColor(Colors.PROGRESS_DOWNLOADING),
        "completed":         QColor(Colors.PROGRESS_COMPLETE),
        "seeding":           QColor(Colors.PROGRESS_SEEDING),
        "paused":            QColor(Colors.PROGRESS_PAUSED),
        "error":             QColor(Colors.PROGRESS_ERROR),
        "queued":            QColor(Colors.TEXT_DIM),
        "fetching_metadata": QColor(Colors.CYAN),
        "file_not_found":    QColor(Colors.RED),
        "stalled":           QColor(Colors.ORANGE),
    }

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex):
        raw = index.data(Qt.ItemDataRole.DisplayRole)
        if not isinstance(raw, dict):
            super().paint(painter, option, index)
            return

        progress = raw.get("progress", 0)
        status = raw.get("status", "queued")

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect: QRect = option.rect.adjusted(4, 4, -4, -4)
        radius = rect.height() // 2

        # Background track
        bg_color = QColor(Colors.PROGRESS_BG)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg_color)
        painter.drawRoundedRect(rect, radius, radius)

        # Filled portion
        if progress > 0:
            fill_width = max(
                rect.height(),  # minimum visible width
                int(rect.width() * progress / 100.0),
            )
            fill_rect = QRect(
                rect.x(), rect.y(), fill_width, rect.height()
            )

            color = self._STATUS_COLORS.get(status, QColor(Colors.ACCENT))

            # Subtle gradient
            gradient = QLinearGradient(
                fill_rect.topLeft(), fill_rect.bottomLeft()
            )
            gradient.setColorAt(0.0, color.lighter(120))
            gradient.setColorAt(1.0, color)

            painter.setBrush(gradient)
            painter.drawRoundedRect(fill_rect, radius, radius)

        # Percentage text
        if status == "file_not_found":
            text = "Not Found"
        elif status == "fetching_metadata":
            text = "Metadata..."
        elif status == "stalled":
            text = f"{progress:.1f}% (Stalled)"
        else:
            text = f"{progress:.1f}%"
        painter.setPen(QPen(QColor(Colors.TEXT)))
        font = painter.font()
        font_size = min(11, max(9, rect.height() - 6))
        font.setPixelSize(font_size)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem,
                 index: QModelIndex):
        size = super().sizeHint(option, index)
        size.setHeight(max(size.height(), 28))
        return size
