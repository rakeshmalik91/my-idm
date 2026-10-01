"""Custom delegates for the download table view."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QModelIndex, QRect, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionProgressBar,
    QStyleOptionViewItem,
)

from my_idm.styles import Colors
from my_idm.utils import normalize_path


def _shorten_path(path: str, max_width: int, font_metrics: QFontMetrics) -> str:
    """Shorten a path to fit within max_width, prioritizing leaf nodes.

    The leaf folder (last component) is always kept visible; earlier parts are
    collapsed into an ellipsis marker. As the column narrows further the leaf
    itself is elided, then finally only the drive/root marker is shown.

    Examples:
        "C:/Users/Name/Downloads/Movies" -> "C:/…/Movies" (when narrow)
        "C:/Users/Name/Downloads/Movies" -> "C:/…/Down…" (when leaf too long)
        "C:/Users/Name/Downloads/Movies" -> "C:/…" (when very narrow)
        "/home/user/Downloads/Movies" -> ".../Movies" (when narrow)
    """
    if not path:
        return ""

    path = normalize_path(path)
    if font_metrics.horizontalAdvance(path) <= max_width:
        return path

    # Split into parts
    parts = [p for p in Path(path).parts if p]
    if not parts:
        return path

    # Check if first part is a drive letter (Windows: "C:", "C:\", "D:", etc.)
    has_drive = len(parts) > 0 and len(parts[0]) >= 2 and parts[0][1] == ':' and parts[0][0].isalpha()
    drive_letter = parts[0].rstrip('\\/') if has_drive else ""  # Normalize to "C:"
    path_parts = parts[1:] if has_drive else parts

    ellipsis = "…"
    ellipsis_width = font_metrics.horizontalAdvance(ellipsis)

    # When there are path components beyond the root, prioritize the leaf.
    if path_parts:
        leaf = path_parts[-1]

        # Try keeping the leaf plus an increasing number of parent parts,
        # collapsing the omitted prefix with an ellipsis marker.
        for n in (1, 2, 3):
            if n > len(path_parts):
                break
            tail = path_parts[-n:]
            if has_drive:
                candidate = f"{drive_letter}/{ellipsis}/{'/'.join(tail)}"
            else:
                candidate = f"{ellipsis}/{'/'.join(tail)}"
            if font_metrics.horizontalAdvance(candidate) <= max_width:
                return candidate

        # The leaf itself is too long: keep the drive/root marker and elide the
        # leaf so it still renders (e.g. "C:/…/Down…") instead of dropping to just
        # the drive letter (e.g. "C:").
        if has_drive:
            prefix = f"{drive_letter}/{ellipsis}/"
        else:
            prefix = f"{ellipsis}/"
        prefix_width = font_metrics.horizontalAdvance(prefix)
        leaf_space = max_width - prefix_width
        if leaf_space >= ellipsis_width:
            return prefix + font_metrics.elidedText(
                leaf, Qt.TextElideMode.ElideRight, leaf_space
            )

        # Very narrow: drop the leaf entirely, keep the drive/root marker.
        if has_drive:
            short = f"{drive_letter}/{ellipsis}"
        else:
            short = ellipsis
        if font_metrics.horizontalAdvance(short) <= max_width:
            return short

    # No path components beyond the root (root-only path): elide the root.
    root = drive_letter if has_drive else path
    return font_metrics.elidedText(root, Qt.TextElideMode.ElideRight, max_width)


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
        "suspended":         QColor(Colors.TEXT_DIM),
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
    """Renders the filename while preserving its decoration and Tor indicator."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        if not opt.icon.isNull() and opt.text.startswith("🧅 "):
            opt.text = opt.text[2:].lstrip()

        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)


class SavePathDelegate(QStyledItemDelegate):
    """Renders the save path with intelligent shortening that prioritizes leaf nodes."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        
        # Get the full path from the model
        full_path = opt.text
        if not full_path:
            super().paint(painter, opt, index)
            return
        
        # Calculate available width (with some padding)
        padding = 8
        max_width = opt.rect.width() - padding
        
        # Shorten the path
        font_metrics = QFontMetrics(opt.font)
        shortened = _shorten_path(full_path, max_width, font_metrics)
        
        # Update the text to display
        opt.text = shortened
        
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

    def sizeHint(self, option: QStyleOptionViewItem,
                 index: QModelIndex):
        size = super().sizeHint(option, index)
        size.setHeight(max(size.height(), 28))
        return size


class QueueColumnDelegate(QStyledItemDelegate):
    """Renders a queue's colour swatch beside its name.

    Falls back to **swatch only** when the column is too narrow for both. That is the point of
    the fallback: a half-elided name next to a swatch is worse than the swatch alone, because
    the colour still identifies the queue whereas "Torr..." does not. The decision is made
    against the measured text width, so a short queue name keeps its label at the same column
    width at which a long one collapses to the swatch.

    **The tooltip is not optional.** A narrow column shows the swatch and nothing else, and a
    coloured square with no legend is not readable — the same reason the fallback exists applies
    to hover. Qt draws the tooltip for the whole cell, so it is the name plus the swatch hex.
    """

    #: Swatch edge length, the gap to the text, and the padding either side of the swatch.
    SWATCH = 12
    GAP = 6
    PADDING = 4
    #: Narrower than this and the name is dropped rather than elided into uselessness.
    MIN_TEXT_WIDTH = 28

    def _swatch_rect(self, option: QStyleOptionViewItem) -> QRect:
        return QRect(
            option.rect.left() + self.PADDING,
            option.rect.top() + (option.rect.height() - self.SWATCH) // 2,
            self.SWATCH,
            self.SWATCH,
        )

    def _text_colour(self, option: QStyleOptionViewItem) -> QColor:
        """Swatch-visible foreground: highlighted text on a selection, plain text otherwise."""
        if option.state & QStyle.StateFlag.State_Selected:
            return option.palette.color(QPalette.ColorRole.HighlightedText)
        return option.palette.color(QPalette.ColorRole.Text)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex):
        from my_idm.download_model import QUEUE_COLOR_ROLE

        colour = index.data(QUEUE_COLOR_ROLE)
        name = index.data(Qt.ItemDataRole.DisplayRole) or ""
        metrics = QFontMetrics(option.font)

        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        elif option.features & QStyleOptionViewItem.ViewItemFeature.HasDisplay:
            painter.fillRect(option.rect, option.palette.base())

        if not colour:
            # No swatch to draw (a queue with no colour): fall back to plain text so the row is
            # still identifiable rather than blank.
            painter.setPen(self._text_colour(option))
            painter.drawText(
                option.rect.adjusted(self.PADDING, 0, -self.PADDING, 0),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                metrics.elidedText(name, Qt.TextElideMode.ElideRight,
                                  max(0, option.rect.width() - 8)),
            )
            painter.restore()
            return

        swatch = self._swatch_rect(option)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(QColor(colour))
        # A translucent dark outline keeps a dark swatch visible against a dark row.
        painter.setPen(QPen(QColor(0, 0, 0, 140)))
        painter.drawRoundedRect(swatch, 3, 3)

        text_left = swatch.right() + self.GAP
        available = option.rect.right() - self.PADDING - text_left
        if name and available >= self.MIN_TEXT_WIDTH:
            painter.setPen(self._text_colour(option))
            painter.drawText(
                QRect(text_left, option.rect.top(), available, option.rect.height()),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                metrics.elidedText(name, Qt.TextElideMode.ElideRight, available),
            )
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex):
        """Wide enough for the swatch plus the name, but no wider than the name needs."""
        size = super().sizeHint(option, index)
        name = index.data(Qt.ItemDataRole.DisplayRole) or ""
        if name:
            metrics = QFontMetrics(option.font)
            needed = (
                self.PADDING * 2 + self.SWATCH + self.GAP
                + metrics.horizontalAdvance(name)
            )
            size.setWidth(min(size.width(), needed))
        return size

