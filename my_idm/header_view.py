"""Custom QHeaderView with ASC/DESC sort indicator and multiselect column filters."""

from __future__ import annotations

from typing import Optional, Set

from PySide6.QtCore import QPoint, QPointF, QRect, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from my_idm.download_model import (
    Col,
    STATUS_FILTER_GROUPS,
    STATUS_FILTER_LABELS,
    TYPE_FILTER_LABELS,
)
from my_idm.styles import Colors


class MultiselectFilterPopup(QFrame):
    """Modern dark-themed popup dialog for multiselect column filtering."""

    filter_changed = Signal(int, object)  # (column_index, set_of_selected_keys or None)

    def __init__(
        self,
        column: int,
        selected_keys: Optional[Set[str]],
        counts: dict[str, int],
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self._column = column
        self._checkboxes: dict[str, QCheckBox] = {}
        self._updating = False

        self.setStyleSheet(f"""
            QFrame {{
                background-color: {Colors.BG_DARK};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
            }}
            QLabel {{
                color: {Colors.TEXT};
                font-weight: 600;
                font-size: 12px;
                border: none;
            }}
            QPushButton {{
                background-color: {Colors.BG_MID};
                color: {Colors.TEXT_SECONDARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
                padding: 3px 8px;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background-color: {Colors.BG_LIGHT};
                color: {Colors.TEXT};
                border-color: {Colors.BORDER_LIGHT};
            }}
            QPushButton#resetBtn {{
                background-color: {Colors.BG_MID};
                color: {Colors.ACCENT};
                border-color: {Colors.ACCENT};
            }}
            QPushButton#resetBtn:hover {{
                background-color: {Colors.ACCENT};
                color: {Colors.BG_DARK};
            }}
            QCheckBox {{
                color: {Colors.TEXT};
                font-size: 12px;
                padding: 3px 0;
                spacing: 8px;
                border: none;
            }}
            QCheckBox:hover {{
                color: {Colors.ACCENT};
            }}
            QCheckBox::indicator {{
                width: 15px;
                height: 15px;
                border: 1px solid {Colors.BORDER_LIGHT};
                border-radius: 3px;
                background-color: {Colors.BG_MID};
            }}
            QCheckBox::indicator:checked {{
                background-color: {Colors.ACCENT};
                border-color: {Colors.ACCENT};
                image: none;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # Header Title
        title_text = "Filter by Status" if column == Col.STATUS else "Filter by Download Type"
        title_lbl = QLabel(title_text)
        layout.addWidget(title_lbl)

        # Quick actions row: Select All / Clear All
        action_layout = QHBoxLayout()
        action_layout.setSpacing(6)
        action_layout.setContentsMargins(0, 0, 0, 0)

        select_all_btn = QPushButton("Select All")
        select_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        select_all_btn.clicked.connect(self._select_all)
        action_layout.addWidget(select_all_btn)

        clear_all_btn = QPushButton("Clear All")
        clear_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_all_btn.clicked.connect(self._clear_all)
        action_layout.addWidget(clear_all_btn)

        action_layout.addStretch()
        layout.addLayout(action_layout)

        # Separator line
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background-color: {Colors.BORDER}; max-height: 1px; border: none;")
        layout.addWidget(sep)

        # Items list
        items_widget = QWidget()
        items_layout = QVBoxLayout(items_widget)
        items_layout.setContentsMargins(0, 2, 0, 2)
        items_layout.setSpacing(4)

        if column == Col.STATUS:
            items = STATUS_FILTER_LABELS.items()
        else:
            items = TYPE_FILTER_LABELS.items()

        for key, label in items:
            count = counts.get(key, 0)
            cb = QCheckBox(f"{label}  ({count})")
            cb.setCursor(Qt.CursorShape.PointingHandCursor)
            is_checked = (selected_keys is None) or (key in selected_keys)
            cb.setChecked(is_checked)
            cb.stateChanged.connect(self._on_item_toggled)
            self._checkboxes[key] = cb
            items_layout.addWidget(cb)

        layout.addWidget(items_widget)

        # Bottom footer row
        bottom_layout = QHBoxLayout()
        bottom_layout.setSpacing(6)
        bottom_layout.setContentsMargins(0, 4, 0, 0)

        reset_btn = QPushButton("Reset Filter")
        reset_btn.setObjectName("resetBtn")
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.clicked.connect(self._reset_and_close)
        bottom_layout.addWidget(reset_btn)

        bottom_layout.addStretch()

        close_btn = QPushButton("Close")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.close)
        bottom_layout.addWidget(close_btn)

        layout.addLayout(bottom_layout)

    def _select_all(self):
        self._updating = True
        for cb in self._checkboxes.values():
            cb.setChecked(True)
        self._updating = False
        self._emit_change()

    def _clear_all(self):
        self._updating = True
        for cb in self._checkboxes.values():
            cb.setChecked(False)
        self._updating = False
        self._emit_change()

    def _on_item_toggled(self):
        if not self._updating:
            self._emit_change()

    def _get_selected_keys(self) -> Optional[Set[str]]:
        selected = {k for k, cb in self._checkboxes.items() if cb.isChecked()}
        if len(selected) == len(self._checkboxes):
            return None  # All selected -> no filter
        return selected

    def _emit_change(self):
        selected = self._get_selected_keys()
        self.filter_changed.emit(self._column, selected)

    def _reset_and_close(self):
        self._select_all()
        self.close()


class FilterHeaderView(QHeaderView):
    """Horizontal header view featuring ASC/DESC sort icons and column filter popups."""

    filter_requested = Signal(int, object)  # (column, selected_keys)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setSectionsClickable(True)
        self.setSortIndicatorShown(True)
        self.setSectionsMovable(True)
        self.setFirstSectionMovable(True)
        self.setMouseTracking(True)
        self._hover_logical_index: int = -1
        self._hover_filter_btn: bool = False
        self._active_popup: Optional[MultiselectFilterPopup] = None

    def _get_filter_btn_rect(self, logical_index: int) -> QRect:
        if logical_index not in (Col.NAME, Col.STATUS):
            return QRect()
        pos = self.sectionViewportPosition(logical_index)
        width = self.sectionSize(logical_index)
        height = self.height()
        is_sorted = (logical_index == self.sortIndicatorSection())
        right_offset = 32 if is_sorted else 18
        btn_x = pos + width - right_offset
        btn_y = (height - 16) // 2
        return QRect(btn_x, btn_y, 16, 16)

    def paintSection(self, painter: QPainter, rect: QRect, logical_index: int):
        # 1. Base header section rendering
        super().paintSection(painter, rect, logical_index)

        # 2. Paint ASC/DESC sort indicator icon if this section is sorted
        is_sorted = (logical_index == self.sortIndicatorSection())
        if is_sorted and self.isSortIndicatorShown():
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            arrow_cx = rect.right() - 14
            arrow_cy = rect.top() + rect.height() / 2.0

            if self.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder:
                # Upward pointing triangle (ASC)
                poly = QPolygonF([
                    QPointF(arrow_cx, arrow_cy - 3.5),
                    QPointF(arrow_cx - 4.0, arrow_cy + 3.0),
                    QPointF(arrow_cx + 4.0, arrow_cy + 3.0),
                ])
            else:
                # Downward pointing triangle (DESC)
                poly = QPolygonF([
                    QPointF(arrow_cx, arrow_cy + 3.5),
                    QPointF(arrow_cx - 4.0, arrow_cy - 3.0),
                    QPointF(arrow_cx + 4.0, arrow_cy - 3.0),
                ])

            painter.setBrush(QColor(Colors.ACCENT))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPolygon(poly)
            painter.restore()

        # 3. Paint filter icon for Name (Type) and Status columns
        if logical_index in (Col.NAME, Col.STATUS):
            model = self.model()
            is_filtered = False
            if model is not None:
                if logical_index == Col.STATUS:
                    is_filtered = getattr(model, "is_status_filtered", lambda: False)()
                elif logical_index == Col.NAME:
                    is_filtered = getattr(model, "is_type_filtered", lambda: False)()

            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)

            fx = rect.right() - (30 if is_sorted else 16)
            fy = rect.top() + (rect.height() - 12) // 2

            # Funnel vector path
            path = QPainterPath()
            path.moveTo(fx, fy + 1)
            path.lineTo(fx + 10, fy + 1)
            path.lineTo(fx + 6.5, fy + 5.5)
            path.lineTo(fx + 6.5, fy + 10)
            path.lineTo(fx + 4, fy + 8.5)
            path.lineTo(fx + 4, fy + 5.5)
            path.closeSubpath()

            is_hovered = (
                self._hover_filter_btn and self._hover_logical_index == logical_index
            )

            if is_filtered:
                painter.setBrush(QColor(Colors.ACCENT))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawPath(path)
                # Small active indicator dot
                painter.setBrush(QColor(Colors.GREEN))
                painter.drawEllipse(QPointF(fx + 9.5, fy + 1.0), 2.2, 2.2)
            else:
                color = QColor(Colors.TEXT_SECONDARY if is_hovered else Colors.TEXT_DIM)
                painter.setPen(QPen(color, 1.2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(path)

            painter.restore()

    def mousePressEvent(self, event: QMouseEvent):
        pos = event.position().toPoint()
        if event.button() == Qt.MouseButton.LeftButton:
            logical_idx = self.logicalIndexAt(pos)
            if logical_idx in (Col.NAME, Col.STATUS):
                btn_rect = self._get_filter_btn_rect(logical_idx)
                if btn_rect.contains(pos):
                    self._open_filter_popup(logical_idx, btn_rect)
                    event.accept()
                    return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        pos = event.position().toPoint()
        logical_idx = self.logicalIndexAt(pos)
        if logical_idx in (Col.NAME, Col.STATUS):
            btn_rect = self._get_filter_btn_rect(logical_idx)
            if btn_rect.contains(pos):
                if not self._hover_filter_btn or self._hover_logical_index != logical_idx:
                    self._hover_filter_btn = True
                    self._hover_logical_index = logical_idx
                    self.setCursor(Qt.CursorShape.PointingHandCursor)
                    title = "Status" if logical_idx == Col.STATUS else "Download Type"
                    self.setToolTip(f"Filter by {title}")
                    self.viewport().update()
                return

        if self._hover_filter_btn:
            self._hover_filter_btn = False
            self._hover_logical_index = -1
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.setToolTip("")
            self.viewport().update()

        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self._hover_filter_btn:
            self._hover_filter_btn = False
            self._hover_logical_index = -1
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.setToolTip("")
            self.viewport().update()
        super().leaveEvent(event)

    def _open_filter_popup(self, logical_index: int, btn_rect: QRect):
        if self._active_popup is not None:
            self._active_popup.close()
            self._active_popup = None

        model = self.model()
        if model is None:
            return

        if logical_index == Col.STATUS:
            current_selection = getattr(model, "status_filter", lambda: None)()
            counts = getattr(model, "get_status_counts", lambda: {})()
        else:
            current_selection = getattr(model, "type_filter", lambda: None)()
            counts = getattr(model, "get_type_counts", lambda: {})()

        popup = MultiselectFilterPopup(logical_index, current_selection, counts, self)
        popup.filter_changed.connect(self._on_filter_changed)

        # Position right under the header section
        sec_pos = self.sectionViewportPosition(logical_index)
        global_pt = self.viewport().mapToGlobal(QPoint(sec_pos, self.height() + 2))
        popup.move(global_pt)
        popup.show()
        self._active_popup = popup

    def _on_filter_changed(self, column: int, selected_keys: Optional[Set[str]]):
        model = self.model()
        if model is not None:
            if column == Col.STATUS:
                if hasattr(model, "set_status_filter"):
                    model.set_status_filter(selected_keys)
            elif column == Col.NAME:
                if hasattr(model, "set_type_filter"):
                    model.set_type_filter(selected_keys)
        self.filter_requested.emit(column, selected_keys)
        self.viewport().update()
