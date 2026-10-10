"""Custom delegates for the download table view."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QEvent, QModelIndex, QPoint, QRect, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QCursor,
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

from my_idm import fonts
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
        "deleting":          QColor(Colors.TEXT_DISABLED),
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

        is_disabled = not bool(option.state & QStyle.StateFlag.State_Enabled) or status == "deleting"
        if is_disabled:
            painter.setOpacity(0.35)

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
        elif status == "deleting":
            text = "Deleting..."
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

        is_disabled = not bool(opt.state & QStyle.StateFlag.State_Enabled)
        if is_disabled:
            painter.save()
            painter.setOpacity(0.4)

        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        if is_disabled:
            painter.restore()


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
        
        is_disabled = not bool(opt.state & QStyle.StateFlag.State_Enabled)
        if is_disabled:
            painter.save()
            painter.setOpacity(0.4)

        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        if is_disabled:
            painter.restore()

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
    SWATCH = 18
    GAP = 8
    PADDING = 5
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
        if not (option.state & QStyle.StateFlag.State_Enabled):
            return QColor(Colors.TEXT_DISABLED)
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
        is_disabled = not bool(option.state & QStyle.StateFlag.State_Enabled)
        if is_disabled:
            painter.setOpacity(0.35)

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
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setBrush(QColor(colour))
        # A translucent dark outline keeps a dark swatch visible against a dark row.
        painter.setPen(QPen(QColor(0, 0, 0, 140)))
        painter.drawRoundedRect(swatch, 4, 4)

        char = (name or "").strip()[:1].upper()
        if char:
            letter_font = QFont(option.font)
            letter_font.setBold(True)
            letter_font.setPixelSize(10)
            painter.setFont(letter_font)
            qc = QColor(colour)
            luminance = (0.299 * qc.red() + 0.587 * qc.green() + 0.114 * qc.blue()) / 255.0
            text_color = QColor(0, 0, 0, 220) if luminance > 0.65 else QColor(255, 255, 255, 240)
            painter.setPen(text_color)
            painter.drawText(swatch, int(Qt.AlignmentFlag.AlignCenter), char)
            painter.setFont(option.font)

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


def get_section_select_all_btn_rect(cell_rect: QRect, viewport_width: int = 0) -> QRect:
    """Calculate the bounding rectangle for the 'Select All' / 'Clear Selection' button at the right edge of a section header row."""
    btn_w = 96
    btn_h = 20
    right_limit = min(cell_rect.right(), viewport_width) if viewport_width > 0 else cell_rect.right()
    btn_x = right_limit - btn_w - 12
    # Ensure it doesn't overlap the left title area
    btn_x = max(cell_rect.left() + 180, btn_x)
    btn_y = cell_rect.top() + max(0, (cell_rect.height() - btn_h) // 2)
    return QRect(btn_x, btn_y, btn_w, btn_h)


class SectionHeaderDelegate(QStyledItemDelegate):
    """Renders section header rows in grouped view with a 'Select All' / 'Clear Selection' button at the right edge.

    For ordinary download rows in column 0 (Col.QUEUE), it delegates to default painting
    to show the queue order/row number.
    """

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        model = index.model()
        row = index.row()
        if not getattr(model, "is_section_header_row", lambda r: False)(row):
            super().paint(painter, option, index)
            return

        entry = getattr(model, "get_section_header", lambda r: None)(row)
        if entry is None:
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        # 1. Background spanning across the header row
        painter.fillRect(option.rect, QColor("#1e2330"))

        # Subtle bottom border line
        painter.setPen(QPen(QColor("#282f40"), 1))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())

        # 2. Text (arrow + section title + count)
        arrow = "▶" if entry.section_collapsed else "▼"
        title_text = f"  {arrow}   {entry.section_title.upper()}"
        active_count = getattr(entry, "section_active_count", 0)
        seeding_count = getattr(entry, "section_seeding_count", 0)
        total_count = getattr(entry, "section_count", 0)

        fg_color = index.data(Qt.ItemDataRole.ForegroundRole)
        painter.setPen(QColor(fg_color) if fg_color else QColor(Colors.ACCENT))
        hdr_font = index.data(Qt.ItemDataRole.FontRole) or fonts.ui_font(10, bold=True)
        painter.setFont(hdr_font)

        text_rect = option.rect.adjusted(12, 0, -120, 0)
        
        # Draw title
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            title_text,
        )
        
        # Draw count parts with different colors
        title_width = painter.fontMetrics().horizontalAdvance(title_text)
        x_pos = 12 + title_width + 2
        
        # Active count in green
        if active_count > 0:
            active_text = f" {active_count} Active"
            active_rect = option.rect.adjusted(x_pos, 0, -120, 0)
            active_width = painter.fontMetrics().horizontalAdvance(active_text)
            green_color = QColor(Colors.GREEN) if hasattr(Colors, 'GREEN') else QColor("#2ea043")
            painter.setPen(green_color)
            painter.drawText(
                active_rect,
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                active_text,
            )
            x_pos += active_width
        
        count_color = QColor(Colors.TEXT_MUTED) if hasattr(Colors, 'TEXT_MUTED') else QColor("#8fa0b5")

        # Seeding count in purple
        if seeding_count > 0:
            if active_count > 0:
                sep_text = " / "
                sep_rect = option.rect.adjusted(x_pos, 0, -120, 0)
                sep_width = painter.fontMetrics().horizontalAdvance(sep_text)
                painter.setPen(count_color)
                painter.drawText(
                    sep_rect,
                    int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    sep_text,
                )
                x_pos += sep_width
                seeding_text = f"{seeding_count} Seeding"
            else:
                seeding_text = f" {seeding_count} Seeding"

            seeding_rect = option.rect.adjusted(x_pos, 0, -120, 0)
            seeding_width = painter.fontMetrics().horizontalAdvance(seeding_text)
            purple_color = QColor(Colors.PURPLE) if hasattr(Colors, 'PURPLE') else QColor("#a371f7")
            painter.setPen(purple_color)
            painter.drawText(
                seeding_rect,
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                seeding_text,
            )
            x_pos += seeding_width
        
        # Total count in muted color
        total_text = f" / {total_count} Total" if (active_count > 0 or seeding_count > 0) else f" {total_count} Total"
        total_rect = option.rect.adjusted(x_pos, 0, -120, 0)
        count_color = QColor(Colors.TEXT_MUTED) if hasattr(Colors, 'TEXT_MUTED') else QColor("#8fa0b5")
        painter.setPen(count_color)
        painter.drawText(
            total_rect,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            total_text,
        )
        
        # 2b. Draw consolidated progress bar for active downloads
        active_progress = getattr(entry, "section_active_progress", 0.0)
        if active_count > 0 and active_progress > 0:
            # Progress bar positioned after the count text
            fm = painter.fontMetrics()
            total_width = fm.horizontalAdvance(total_text)
            progress_x = x_pos + total_width + 10

            # Vertically center with the text glyphs (cap height / baseline visual center)
            font_top = option.rect.top() + (option.rect.height() - fm.height()) // 2
            baseline = font_top + fm.ascent()
            cap_center = baseline - fm.capHeight() // 2
            bar_height = 12
            bar_y = cap_center - bar_height // 2

            max_bar_width = max(20, option.rect.right() - 120 - (option.rect.left() + progress_x))
            bar_width = min(150, max_bar_width)
            progress_rect = QRect(option.rect.left() + progress_x, bar_y, bar_width, bar_height)

            # Background
            painter.setBrush(QColor("#21262d"))
            painter.setPen(QPen(QColor("#30363d"), 1))
            painter.drawRoundedRect(progress_rect, 3, 3)

            # Progress fill
            fill_width = int(progress_rect.width() * active_progress / 100.0)
            if fill_width > 0:
                fill_rect = QRect(
                    progress_rect.x() + 1,
                    progress_rect.y() + 1,
                    min(fill_width, max(0, progress_rect.width() - 2)),
                    bar_height - 2,
                )
                # Use green for active progress
                painter.setBrush(QColor(Colors.GREEN) if hasattr(Colors, 'GREEN') else QColor("#2ea043"))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(fill_rect, 2, 2)
        
        # 3. 'Select All' / 'Clear Selection' button at the right edge
        if getattr(entry, "section_count", 0) > 0:
            vw = option.widget.width() if option.widget else 0
            btn_rect = get_section_select_all_btn_rect(option.rect, vw)

            all_selected = False
            if option.widget and hasattr(option.widget, "selectionModel"):
                sm = option.widget.selectionModel()
                if sm:
                    download_rows = getattr(model, "get_section_download_rows", lambda sid: [])(entry.section_id)
                    all_selected = bool(download_rows) and all(sm.isRowSelected(r, QModelIndex()) for r in download_rows)

            btn_label = "Clear Selection" if all_selected else "Select All"

            pos = option.widget.mapFromGlobal(QCursor.pos()) if option.widget else None
            is_hover = (pos is not None and btn_rect.contains(pos))

            if is_hover:
                painter.setBrush(QColor(88, 166, 255, 45) if all_selected else QColor(88, 166, 255, 35))
                painter.setPen(QPen(QColor(Colors.ACCENT), 1))
            elif all_selected:
                painter.setBrush(QColor(88, 166, 255, 20))
                painter.setPen(QPen(QColor(88, 166, 255, 70), 1))
            else:
                painter.setBrush(QColor(255, 255, 255, 12))
                painter.setPen(QPen(QColor(255, 255, 255, 35), 1))

            painter.drawRoundedRect(btn_rect, 4, 4)

            painter.setFont(fonts.ui_font(9, bold=True))
            btn_text_color = (
                QColor(Colors.TEXT)
                if is_hover
                else (QColor(Colors.ACCENT) if all_selected else QColor(Colors.TEXT_SECONDARY))
            )
            painter.setPen(btn_text_color)
            painter.drawText(btn_rect, int(Qt.AlignmentFlag.AlignCenter), btn_label)

        painter.restore()

    def editorEvent(self, event, model, option, index) -> bool:
        row = index.row()
        if not getattr(model, "is_section_header_row", lambda r: False)(row):
            return super().editorEvent(event, model, option, index)

        entry = getattr(model, "get_section_header", lambda r: None)(row)
        if entry is None or getattr(entry, "section_count", 0) <= 0:
            return super().editorEvent(event, model, option, index)

        vw = option.widget.width() if option.widget else 0
        btn_rect = get_section_select_all_btn_rect(option.rect, vw)

        if event.type() == QEvent.Type.MouseMove:
            pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
            if btn_rect.contains(pos):
                if option.widget:
                    option.widget.setCursor(Qt.CursorShape.PointingHandCursor)
                    option.widget.update(btn_rect)
            else:
                if option.widget and option.widget.cursor().shape() == Qt.CursorShape.PointingHandCursor:
                    option.widget.setCursor(Qt.CursorShape.ArrowCursor)
                    option.widget.update(btn_rect)
            return False

        return super().editorEvent(event, model, option, index)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        model = index.model()
        if getattr(model, "is_section_header_row", lambda r: False)(index.row()):
            return QSize(super().sizeHint(option, index).width(), 28)
        return super().sizeHint(option, index)


