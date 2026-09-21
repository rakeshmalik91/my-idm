"""Custom delegates for the download table view."""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPen
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
        if status in ("completed", "seeding"):
            progress = 100.0

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


class DownloadNameDelegate(QStyledItemDelegate):
    """Renders the download filename along with its source domain in a different color at the end."""

    _DOMAIN_COLOR = QColor(Colors.CYAN)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        name = opt.text
        domain = index.data(Qt.ItemDataRole.UserRole) or ""

        widget = opt.widget
        style = widget.style() if widget else QApplication.style()

        # Draw panel background (selection, hover) and decoration icon only (without text)
        # Note: Do not call super().paint as it re-invokes initStyleOption and draws text
        opt.text = ""
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        if not name:
            return

        if not opt.icon.isNull() and name.startswith("🧅 "):
            name = name[2:].lstrip()

        text_rect: QRect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, widget)
        if text_rect.width() <= 0:
            text_rect = option.rect.adjusted(24, 0, -4, 0)

        avail_w = text_rect.width()
        if avail_w <= 10:
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setFont(opt.font)
        fm = QFontMetrics(opt.font)
        gap = 8

        elided_name, elided_domain = self._layout_texts(name, domain, avail_w, fm, gap)
        text_flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

        # Draw filename in standard text color
        name_w = fm.horizontalAdvance(elided_name)
        name_rect = QRect(text_rect.x(), text_rect.y(), name_w + 4, text_rect.height())
        painter.setPen(QPen(QColor(Colors.TEXT)))
        painter.drawText(name_rect, text_flags, elided_name)

        # Draw source domain at end in cyan
        if elided_domain:
            dom_x = text_rect.x() + name_w + gap
            dom_w = fm.horizontalAdvance(elided_domain)
            dom_rect = QRect(dom_x, text_rect.y(), dom_w + 4, text_rect.height())
            painter.setPen(QPen(self._DOMAIN_COLOR))
            painter.drawText(dom_rect, text_flags, elided_domain)

        painter.restore()

    def _layout_texts(self, name: str, domain: str, avail_w: int, fm: QFontMetrics, gap: int) -> tuple[str, str]:
        if not domain:
            return fm.elidedText(name, Qt.TextElideMode.ElideRight, avail_w), ""

        name_w = fm.horizontalAdvance(name)
        dom_w = fm.horizontalAdvance(domain)
        if name_w + gap + dom_w <= avail_w:
            return name, domain

        # If full domain fits leaving reasonable space for filename (>= 80px), keep full domain
        if avail_w - dom_w - gap >= 80:
            elided_name = fm.elidedText(name, Qt.TextElideMode.ElideRight, avail_w - dom_w - gap)
            return elided_name, domain

        # Under tighter space, allocate proportionately
        dom_avail = min(dom_w, max(int(avail_w * 0.4), 45))
        avail_for_name = max(avail_w - gap - dom_avail, 35)
        elided_name = fm.elidedText(name, Qt.TextElideMode.ElideRight, avail_for_name)
        actual_dom_avail = avail_w - fm.horizontalAdvance(elided_name) - gap
        if actual_dom_avail >= 25:
            elided_domain = fm.elidedText(domain, Qt.TextElideMode.ElideRight, actual_dom_avail)
        else:
            elided_domain = ""
            elided_name = fm.elidedText(name, Qt.TextElideMode.ElideRight, avail_w)
        return elided_name, elided_domain
