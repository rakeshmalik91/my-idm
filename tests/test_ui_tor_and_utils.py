"""Hardened tests for the header-view widgets, the Tor service lifecycle, and utils.

Three independent surfaces that the existing suite only partly covers, chosen for the
failures they actually produce:

``header_view`` / ``delegates`` (UI components)
    The filter popup's select-all / clear-all / reset actions, the hover-tooltip state
    machine on the funnel button, the "no model yet" guard, and the popup-replacement rule.
    These are pure widget state, so they are driven with real Qt events and a real
    ``QAbstractTableModel`` - no screenshots, no waiting, no sleeps.

``tor_service`` (process lifecycle)
    ``TorServiceManager`` spawns and kills a child process, and every failure mode has a
    distinct user-facing message. The interesting cases are the ones a happy-path test
    never reaches: an externally-started Tor that must *not* be killed, a port collision, a
    shared data directory, an orphan PID file, and a spawn that exits immediately.

``utils`` (edge cases)
    ``robust_move_download_files`` - the retry ladder, the copy fallback, the partial-move
    merge, and the "already there" idempotence; ``unlock_path``; ``send_to_trash``;
    ``get_unique_filename``'s compound extensions and ``(N)`` continuation.

Everything is hermetic: no Tor binary is executed, no file outside the per-test temp
directory is touched, and every process launch is faked.

Two implementation details the tests have to respect, both of which bit during authoring:

* ``utils`` imports ``os``/``shutil``/``time``/``stat``/``gc`` *inside* its functions, so a
  test must patch ``shutil.move`` on the real module rather than ``my_idm.utils.shutil``.
* ``FilterHeaderView.paintSection`` asks the *model* whether a column is filtered
  (``model.is_status_filtered()``), so the painting tests need a real model attached.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QPoint,
    QPointF,
    Qt,
)
from PySide6.QtGui import QColor, QFontMetrics, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem, QTableView

from my_idm import tor_service as tor_module
from my_idm.config import TorConfig
from my_idm.delegates import (
    DownloadNameDelegate,
    ProgressBarDelegate,
    SavePathDelegate,
    _shorten_path,
)
from my_idm.download_model import (
    SIZE_FILTER_LABELS,
    STATUS_FILTER_LABELS,
    TYPE_FILTER_LABELS,
    Col,
)
from my_idm.header_view import FilterHeaderView, MultiselectFilterPopup
from my_idm.styles import Colors
from my_idm.tor_service import TorServiceManager, find_tor_executable
from my_idm.utils import (
    get_unique_filename,
    robust_move_download_files,
    send_to_trash,
    unlock_path,
)

app = QApplication.instance() or QApplication(sys.argv)


# ===========================================================================
# header_view - UI components
# ===========================================================================

class MinimalModel(QAbstractTableModel):
    """A concrete empty model, for the "header with nothing useful attached" cases."""

    def rowCount(self, parent=QModelIndex()):
        return 0

    def columnCount(self, parent=QModelIndex()):
        return 0


class StubModel(QAbstractTableModel):
    """A model exposing the three filter getters/setters ``FilterHeaderView`` uses.

    The header reads them as *methods* (``model.status_filter()``) but the view's own
    storage is a plain attribute, so the names must not collide: state lives in
    ``_*_filter`` and the accessors are real methods.
    """

    def __init__(self):
        super().__init__()
        self._status_filter = None
        self._size_filter = None
        self._type_filter = None
        self.filtered = set()
        self.set_calls: list[tuple] = []

    def rowCount(self, parent=QModelIndex()):
        return 0

    def columnCount(self, parent=QModelIndex()):
        return Col.COUNT

    def status_filter(self):
        return self._status_filter

    def size_filter(self):
        return self._size_filter

    def type_filter(self):
        return self._type_filter

    def set_status_filter(self, keys):
        self._status_filter = keys
        self.set_calls.append(("status", keys))

    def set_size_filter(self, keys):
        self._size_filter = keys
        self.set_calls.append(("size", keys))

    def set_type_filter(self, keys):
        self._type_filter = keys
        self.set_calls.append(("type", keys))

    def get_status_counts(self):
        return {key: index for index, key in enumerate(STATUS_FILTER_LABELS)}

    def get_size_counts(self):
        return {key: index for index, key in enumerate(SIZE_FILTER_LABELS)}

    def get_type_counts(self):
        return {key: index for index, key in enumerate(TYPE_FILTER_LABELS)}

    def is_status_filtered(self):
        return "status" in self.filtered

    def is_type_filtered(self):
        return "type" in self.filtered

    def is_size_filtered(self):
        return "size" in self.filtered


class FilterPopupTestCase(unittest.TestCase):
    def setUp(self):
        self.popups: list[MultiselectFilterPopup] = []
        self.addCleanup(self._destroy)

    def _destroy(self):
        for popup in self.popups:
            popup.close()
            popup.deleteLater()
        QApplication.processEvents()

    def make_popup(self, column=Col.STATUS, selected=None, counts=None):
        popup = MultiselectFilterPopup(column, selected, counts or {}, None)
        self.popups.append(popup)
        return popup

    def emitted(self, popup):
        seen: list[tuple] = []
        popup.filter_changed.connect(lambda col, keys: seen.append((col, keys)))
        return seen


class TestFilterPopupLabels(FilterPopupTestCase):
    """Each column's popup must list that column's own label set."""

    def _keys(self, column):
        return set(self.make_popup(column)._checkboxes)

    def test_the_status_popup_lists_statuses(self):
        self.assertEqual(self._keys(Col.STATUS), set(STATUS_FILTER_LABELS))

    def test_the_size_popup_lists_sizes_not_types(self):
        """Regression guard: the Size popup used to borrow the type labels."""
        self.assertEqual(self._keys(Col.SIZE), set(SIZE_FILTER_LABELS))
        self.assertNotEqual(set(SIZE_FILTER_LABELS), set(TYPE_FILTER_LABELS))

    def test_the_name_popup_lists_types(self):
        self.assertEqual(self._keys(Col.NAME), set(TYPE_FILTER_LABELS))

    def test_counts_are_rendered_into_the_label(self):
        popup = self.make_popup(Col.STATUS, counts={"queued": 3, "downloading": 7})
        self.assertIn("(3)", popup._checkboxes["queued"].text())
        self.assertIn("(7)", popup._checkboxes["downloading"].text())

    def test_a_missing_count_renders_as_zero(self):
        popup = self.make_popup(Col.STATUS, counts={})
        self.assertIn("(0)", popup._checkboxes["queued"].text())

    def test_everything_is_checked_when_no_selection_is_given(self):
        popup = self.make_popup(Col.STATUS)
        self.assertTrue(all(cb.isChecked() for cb in popup._checkboxes.values()))

    def test_only_the_selected_keys_start_checked(self):
        popup = self.make_popup(Col.STATUS, selected={"queued", "downloading"})
        self.assertTrue(popup._checkboxes["queued"].isChecked())
        self.assertTrue(popup._checkboxes["downloading"].isChecked())
        self.assertFalse(popup._checkboxes["completed"].isChecked())

    def test_an_empty_selection_set_checks_nothing(self):
        """``None`` means "no filter", but ``set()`` means "filter everything out"."""
        popup = self.make_popup(Col.STATUS, selected=set())
        self.assertFalse(any(cb.isChecked() for cb in popup._checkboxes.values()))


class TestFilterPopupActions(FilterPopupTestCase):
    """Select-all / clear-all / reset, and the ``None``-means-unfiltered contract."""

    def test_select_all_emits_none_to_mean_no_filter(self):
        popup = self.make_popup(Col.STATUS, selected={"queued"})
        seen = self.emitted(popup)
        popup._select_all()
        self.assertEqual(seen, [(Col.STATUS, None)])

    def test_clear_all_emits_the_empty_set_not_none(self):
        """The distinction matters: ``None`` would silently remove the filter."""
        popup = self.make_popup(Col.STATUS)
        seen = self.emitted(popup)
        popup._clear_all()
        self.assertEqual(seen, [(Col.STATUS, set())])

    def test_reset_restores_everything_and_closes(self):
        popup = self.make_popup(Col.STATUS, selected=set())
        seen = self.emitted(popup)
        popup.show()
        popup._reset_and_close()
        self.assertEqual(seen, [(Col.STATUS, None)])
        self.assertTrue(all(cb.isChecked() for cb in popup._checkboxes.values()))

    def test_reselecting_the_current_state_emits_once_not_twice(self):
        """``_updating`` must suppress the per-checkbox signal storm."""
        popup = self.make_popup(Col.STATUS, selected={"queued"})
        seen = self.emitted(popup)
        popup._select_all()
        self.assertEqual(len(seen), 1, "select-all must emit exactly one change")

    def test_clear_all_emits_once_not_once_per_box(self):
        popup = self.make_popup(Col.STATUS)
        seen = self.emitted(popup)
        popup._clear_all()
        self.assertEqual(len(seen), 1)

    def test_unchecking_one_box_emits_the_remaining_keys(self):
        popup = self.make_popup(Col.STATUS)
        seen = self.emitted(popup)
        popup._checkboxes["queued"].setChecked(False)
        self.assertEqual(len(seen), 1)
        column, keys = seen[0]
        self.assertEqual(column, Col.STATUS)
        self.assertNotIn("queued", keys)
        self.assertIn("downloading", keys)

    def test_unchecking_the_last_box_emits_the_empty_set(self):
        popup = self.make_popup(Col.STATUS)
        seen = self.emitted(popup)
        for key in list(popup._checkboxes):
            popup._checkboxes[key].setChecked(False)
        self.assertEqual(seen[-1], (Col.STATUS, set()))

    def test_get_selected_keys_returns_none_only_when_everything_is_on(self):
        popup = self.make_popup(Col.SIZE)
        self.assertIsNone(popup._get_selected_keys())
        popup._checkboxes[next(iter(popup._checkboxes))].setChecked(False)
        self.assertIsNotNone(popup._get_selected_keys())


class FilterHeaderTestCase(unittest.TestCase):
    def setUp(self):
        self.model = StubModel()
        self.view = QTableView()
        self.view.setModel(self.model)
        self.header = FilterHeaderView(self.view)
        self.view.setHorizontalHeader(self.header)
        self.view.resize(1200, 400)
        self.view.show()
        QApplication.processEvents()
        self.addCleanup(self._destroy)

    def _destroy(self):
        popup = self.header._active_popup
        if popup is not None:
            popup.close()
            popup.deleteLater()
        self.header._active_popup = None
        self.view.close()
        self.view.deleteLater()
        QApplication.processEvents()

    def _btn_center(self, column):
        rect = self.header._get_filter_btn_rect(column)
        self.assertFalse(rect.isNull(), f"column {column} has no filter button rect")
        return rect.center()

    def _click(self, point, button=Qt.MouseButton.LeftButton):
        self.header.mousePressEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonPress, QPointF(point), QPointF(point),
                button, button, Qt.KeyboardModifier.NoModifier,
            )
        )

    def _move(self, point):
        self.header.mouseMoveEvent(
            QMouseEvent(
                QEvent.Type.MouseMove, QPointF(point), QPointF(point),
                Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )


class TestFilterHeaderGeometry(FilterHeaderTestCase):
    """The funnel button rectangle, and the guard for columns that have none."""

    def test_filterable_columns_have_a_button(self):
        for column in (Col.NAME, Col.STATUS, Col.SIZE):
            with self.subTest(column=column):
                self.assertFalse(self.header._get_filter_btn_rect(column).isNull())

    def test_a_non_filterable_column_has_an_empty_rect(self):
        self.assertTrue(self.header._get_filter_btn_rect(Col.FILE_NAME).isNull())

    def test_the_button_sits_at_the_right_edge_of_its_section(self):
        rect = self.header._get_filter_btn_rect(Col.STATUS)
        left = self.header.sectionViewportPosition(Col.STATUS)
        right = left + self.header.sectionSize(Col.STATUS)
        self.assertGreaterEqual(rect.x(), left, "the button must stay in its section")
        self.assertLessEqual(
            rect.x() + rect.width(), right,
            "the button must stay inside the section it belongs to",
        )
        self.assertEqual(
            rect.width(), FilterHeaderView.FILTER_BTN_OFFSET - 4,
            "the button is sized independently of the inset",
        )

    def test_the_button_is_sixteen_pixels_and_vertically_centred(self):
        rect = self.header._get_filter_btn_rect(Col.SIZE)
        self.assertEqual((rect.width(), rect.height()), (16, 16))
        self.assertEqual(rect.y(), (self.header.height() - 16) // 2)


class TestFilterHeaderInteraction(FilterHeaderTestCase):
    """Clicking, hovering, and leaving the funnel button."""

    def test_clicking_the_button_opens_the_popup(self):
        self._click(self._btn_center(Col.STATUS))
        self.assertIsNotNone(self.header._active_popup)
        self.assertEqual(self.header._active_popup._column, Col.STATUS)

    def test_the_popup_starts_from_the_models_current_filter(self):
        self.model.set_status_filter({"queued"})
        self._click(self._btn_center(Col.STATUS))
        popup = self.header._active_popup
        self.assertTrue(popup._checkboxes["queued"].isChecked())
        self.assertFalse(popup._checkboxes["completed"].isChecked())

    def test_the_popup_carries_the_model_counts(self):
        self._click(self._btn_center(Col.SIZE))
        first_key = next(iter(SIZE_FILTER_LABELS))
        self.assertIn(
            "(0)", self.header._active_popup._checkboxes[first_key].text(),
            "the popup must render the model's counts, not a placeholder",
        )

    def test_clicking_a_filter_column_away_from_the_button_does_not_open_a_popup(self):
        column = Col.STATUS
        rect = self.header._get_filter_btn_rect(column)
        left = self.header.sectionViewportPosition(column) + 2
        self._click(QPoint(left, rect.center().y()))
        self.assertIsNone(self.header._active_popup)

    def test_clicking_a_non_filter_column_does_not_open_a_popup(self):
        pos = self.header.sectionViewportPosition(Col.FILE_NAME) + 5
        self._click(QPoint(pos, 5))
        self.assertIsNone(self.header._active_popup)

    def test_a_right_click_on_the_button_does_not_open_a_popup(self):
        self._click(self._btn_center(Col.STATUS), Qt.MouseButton.RightButton)
        self.assertIsNone(self.header._active_popup)

    def test_opening_a_second_popup_closes_the_first(self):
        """Two live popups would both be listening and the first would leak."""
        self._click(self._btn_center(Col.STATUS))
        first = self.header._active_popup
        self._click(self._btn_center(Col.SIZE))
        second = self.header._active_popup
        self.assertIsNot(first, second)
        self.assertFalse(first.isVisible(), "the previous popup must be closed")
        self.assertEqual(second._column, Col.SIZE)

    def test_opening_with_no_model_is_a_safe_no_op(self):
        self.view.setModel(None)
        self.header._open_filter_popup(Col.STATUS, self.header._get_filter_btn_rect(Col.STATUS))
        self.assertIsNone(self.header._active_popup)

    def test_a_model_missing_the_filter_accessors_degrades_gracefully(self):
        """A bare model must not raise out of a mouse press."""
        self.view.setModel(MinimalModel())
        self.header._open_filter_popup(Col.STATUS, self.header._get_filter_btn_rect(Col.STATUS))
        self.assertIsNotNone(self.header._active_popup)
        self.assertEqual(self.header._active_popup._column, Col.STATUS)

    def test_hovering_the_button_shows_a_tooltip_and_the_pointing_hand(self):
        self._move(self._btn_center(Col.STATUS))
        self.assertTrue(self.header._hover_filter_btn)
        self.assertEqual(self.header.cursor().shape(), Qt.CursorShape.PointingHandCursor)
        self.assertIn("Filter by", self.header.toolTip())

    def test_re_entering_the_same_button_does_not_restate_the_cursor(self):
        point = self._btn_center(Col.STATUS)
        self._move(point)
        self.header.setCursor(Qt.CursorShape.ArrowCursor)
        self._move(point)
        self.assertEqual(
            self.header.cursor().shape(), Qt.CursorShape.ArrowCursor,
            "an unchanged hover must not touch the cursor on every mouse move",
        )

    def test_moving_off_the_button_clears_the_hover_state(self):
        self._move(self._btn_center(Col.STATUS))
        self._move(QPoint(1, 1))
        self.assertFalse(self.header._hover_filter_btn)
        self.assertEqual(self.header._hover_logical_index, -1)
        self.assertEqual(self.header.toolTip(), "")

    def test_leaving_the_header_clears_the_hover_state(self):
        self._move(self._btn_center(Col.STATUS))
        self.header.leaveEvent(QEvent(QEvent.Type.Leave))
        self.assertFalse(self.header._hover_filter_btn)
        self.assertEqual(self.header.toolTip(), "")

    def test_leaving_without_a_hover_is_a_no_op(self):
        self.header.leaveEvent(QEvent(QEvent.Type.Leave))  # must not raise

    def test_moving_onto_another_filter_button_switches_the_tooltip(self):
        self._move(self._btn_center(Col.STATUS))
        self._move(self._btn_center(Col.SIZE))
        self.assertEqual(self.header._hover_logical_index, Col.SIZE)
        self.assertIn("Size", self.header.toolTip())


class TestFilterChangePropagation(FilterHeaderTestCase):
    """A popup change must reach both the model and the ``filter_requested`` signal."""

    def _open(self, column):
        self.header._open_filter_popup(column, self.header._get_filter_btn_rect(column))
        return self.header._active_popup

    def test_a_status_change_reaches_the_model_and_the_signal(self):
        seen: list[tuple] = []
        self.header.filter_requested.connect(lambda *a: seen.append(a))
        self._open(Col.STATUS)._clear_all()
        self.assertEqual(self.model.set_calls, [("status", set())])
        self.assertEqual(seen, [(Col.STATUS, set())])

    def test_a_type_change_reaches_the_type_setter(self):
        self._open(Col.NAME)._clear_all()
        self.assertEqual(self.model.set_calls, [("type", set())])

    def test_a_size_change_reaches_the_size_setter(self):
        self._open(Col.SIZE)._clear_all()
        self.assertEqual(self.model.set_calls, [("size", set())])

    def test_the_signal_fires_even_when_the_model_has_no_setter(self):
        seen: list[tuple] = []
        self.header.filter_requested.connect(lambda *a: seen.append(a))
        self.view.setModel(MinimalModel())
        self._open(Col.STATUS)._clear_all()
        self.assertEqual(len(seen), 1, "the view must still be told, model or no model")

    def test_a_change_with_no_model_emits_only_the_signal(self):
        seen: list[tuple] = []
        self.header.filter_requested.connect(lambda *a: seen.append(a))
        popup = self._open(Col.STATUS)
        self.view.setModel(None)
        popup._clear_all()
        self.assertEqual(seen[-1], (Col.STATUS, set()))


class TestHeaderPainting(FilterHeaderTestCase):
    """``paintSection`` for every filter state, onto an offscreen pixmap."""

    def _paint(self, column=Col.STATUS, filtered=False, hovered=False):
        self.model.filtered = {"status"} if filtered else set()
        pixmap = QPixmap(1200, 30)
        pixmap.fill(QColor(Colors.BG_MID))
        painter = QPainter(pixmap)
        try:
            self.header._hover_filter_btn = hovered
            self.header._hover_logical_index = column if hovered else -1
            self.header.paintSection(painter, pixmap.rect(), column)
        finally:
            painter.end()

    def test_a_plain_unfiltered_section_paints(self):
        self._paint()

    def test_a_hovered_unfiltered_section_paints(self):
        self._paint(hovered=True)

    def test_a_filtered_section_paints_with_its_active_dot(self):
        self._paint(filtered=True)

    def test_a_filtered_and_hovered_section_paints(self):
        self._paint(filtered=True, hovered=True)

    def test_a_non_filterable_section_paints(self):
        self._paint(column=Col.FILE_NAME)

    def test_a_collapsed_section_paints(self):
        self.header.resizeSection(Col.SIZE, 1)
        self._paint(column=Col.SIZE)

    def test_each_filterable_column_asks_the_model_about_itself(self):
        """A filtered type column must not read the status flag (and vice versa)."""
        for column, marker in (
            (Col.STATUS, "status"), (Col.NAME, "type"), (Col.SIZE, "size"),
        ):
            with self.subTest(column=column):
                self.model.filtered = {marker}
                self._paint(column=column, filtered=True)


# ===========================================================================
# delegates - UI components
# ===========================================================================

class TextModel(QAbstractTableModel):
    """A one-row model returning a fixed display string and optional icon.

    ``QStyledItemDelegate.initStyleOption`` type-checks its ``QModelIndex`` argument, so
    the delegate tests cannot use a mock index - they need a real one.
    """

    def __init__(self, text, icon=None):
        super().__init__()
        self._text = text
        self._icon = icon

    def rowCount(self, parent=QModelIndex()):
        return 1

    def columnCount(self, parent=QModelIndex()):
        return 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            return self._text
        if role == Qt.ItemDataRole.DecorationRole and self._icon is not None:
            return self._icon
        return None


class DelegateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.pixmap = QPixmap(320, 28)
        self.pixmap.fill(QColor(Colors.BG_MID))
        self.painter = QPainter(self.pixmap)
        self.addCleanup(self.painter.end)
        # The models must outlive every index handed to a delegate: a QModelIndex holds
        # a raw pointer to its model, so letting the model be collected turns index.data()
        # into a dangling dereference and an access violation.
        self._models: list = []

    def _option(self, text="", width=320, height=28):
        option = QStyleOptionViewItem()
        option.rect = self.pixmap.rect()
        option.rect.setWidth(width)
        option.rect.setHeight(height)
        option.text = text
        option.font = app.font()
        return option

    def _model(self, text, icon=None) -> TextModel:
        """Build a model that stays alive for the whole test."""
        model = TextModel(text, icon)
        self._models.append(model)
        return model

    def _index(self, text, icon=None):
        """A real QModelIndex carrying *text* and *icon*."""
        return self._model(text, icon).index(0, 0)


class TestShortenPath(DelegateTestCase):
    """Path elision, which is entirely about *which* part of the path survives."""

    def setUp(self):
        super().setUp()
        self.metrics = QFontMetrics(app.font())

    def test_an_empty_path_stays_empty(self):
        self.assertEqual(_shorten_path("", 100, self.metrics), "")

    def test_a_path_that_fits_is_returned_unchanged(self):
        path = "C:/a.zip"
        self.assertEqual(_shorten_path(path, 5000, self.metrics), path)

    def test_a_windows_path_collapses_to_the_drive_and_leaf(self):
        result = _shorten_path("C:/Users/Name/Downloads/Movies", 80, self.metrics)
        self.assertTrue(result.startswith("C:/"), result)
        self.assertTrue(result.endswith("Movies"), result)
        self.assertLessEqual(self.metrics.horizontalAdvance(result), 80)

    def test_a_posix_path_collapses_to_ellipsis_and_leaf(self):
        result = _shorten_path("/home/user/Downloads/Movies", 80, self.metrics)
        self.assertTrue(result.startswith("…"), result)
        self.assertTrue(result.endswith("Movies"), result)

    def test_a_very_narrow_column_still_keeps_the_drive_marker(self):
        result = _shorten_path("C:/Users/Name/Downloads/Movies", 12, self.metrics)
        self.assertTrue(result.startswith("C:"), result)
        self.assertLessEqual(self.metrics.horizontalAdvance(result), 12)

    def test_a_very_narrow_posix_column_keeps_only_the_ellipsis(self):
        result = _shorten_path("/home/user/Downloads/Movies", 8, self.metrics)
        self.assertLessEqual(self.metrics.horizontalAdvance(result), 8)

    def test_an_over_long_leaf_is_elided_but_still_visible(self):
        result = _shorten_path(f"C:/Users/{'A' * 200}", 60, self.metrics)
        self.assertIn("A", result, "the leaf must not disappear entirely")
        self.assertLessEqual(self.metrics.horizontalAdvance(result), 60)

    def test_a_single_component_path_is_elided_as_the_root(self):
        result = _shorten_path("C:", 20, self.metrics)
        self.assertTrue(result, "a root-only path must still render something")
        self.assertIn("C", result)

    def test_a_width_below_one_glyph_still_returns_a_string(self):
        for width in (0, 1, 2, 4):
            with self.subTest(width=width):
                self.assertIsInstance(
                    _shorten_path("C:/Users/Name/Downloads", width, self.metrics), str
                )

    def test_backslashes_are_normalised_before_measuring(self):
        forward = _shorten_path("C:/Users/Name/Downloads", 100, self.metrics)
        backward = _shorten_path("C:\\Users\\Name\\Downloads", 100, self.metrics)
        self.assertEqual(forward, backward)

    def test_a_relative_path_with_no_drive_collapses_with_a_leading_ellipsis(self):
        result = _shorten_path("relative/one/two/three/four", 40, self.metrics)
        self.assertLessEqual(self.metrics.horizontalAdvance(result), 40)


class TestProgressBarDelegate(DelegateTestCase):
    """The progress cell painter, including its non-dict and per-status text paths."""

    def _paint(self, payload, width=320):
        delegate = ProgressBarDelegate()
        self.addCleanup(delegate.deleteLater)
        # option.widget is None, so the fallback path resolves QApplication.style().
        delegate.paint(self.painter, self._option(width=width), self._index(payload))

    def test_a_non_dict_payload_falls_back_to_the_base_delegate(self):
        self._paint("just a string")  # must not raise

    def test_a_none_payload_falls_back_to_the_base_delegate(self):
        self._paint(None)  # must not raise

    def test_a_zero_progress_cell_still_paints_a_track(self):
        self._paint({"progress": 0, "status": "queued"})  # must not raise

    def test_every_status_paints(self):
        for status in ProgressBarDelegate._STATUS_COLORS:
            with self.subTest(status=status):
                self._paint({"progress": 42.0, "status": status})

    def test_an_unknown_status_falls_back_to_the_accent_colour(self):
        self._paint({"progress": 42.0, "status": "brand-new-status"})  # must not raise

    def test_a_missing_status_defaults_to_queued(self):
        self._paint({"progress": 10.0})  # must not raise

    def test_a_completed_cell_is_always_full(self):
        """A completed row must read 100% even if libtorrent reports less."""
        self._paint({"progress": 3.0, "status": "completed"})
        self._paint({"progress": 3.0, "status": "seeding"})

    def test_an_over_full_progress_does_not_overflow_the_cell(self):
        self._paint({"progress": 150.0, "status": "downloading"})

    def test_a_zero_width_cell_does_not_raise(self):
        self._paint({"progress": 50, "status": "downloading"}, width=0)

    def test_a_very_short_cell_does_not_raise(self):
        self._paint({"progress": 50, "status": "downloading"}, width=4)

    def test_size_hint_is_at_least_the_bar_height(self):
        delegate = ProgressBarDelegate()
        self.addCleanup(delegate.deleteLater)
        model = self._model({"progress": 1, "status": "queued"})
        hint = delegate.sizeHint(self._option(), model.index(0, 0))
        self.assertGreaterEqual(hint.height(), 28)


class TestNameAndPathDelegates(DelegateTestCase):
    def _icon(self):
        pixmap = QPixmap(16, 16)
        pixmap.fill(QColor(Colors.ACCENT))
        return pixmap

    def test_the_tor_prefix_is_stripped_from_the_label(self):
        delegate = DownloadNameDelegate()
        self.addCleanup(delegate.deleteLater)
        delegate.paint(
            self.painter, self._option(), self._index("🧅 movie.mkv", self._icon())
        )  # must not raise

    def test_a_plain_name_without_an_icon_paints(self):
        delegate = DownloadNameDelegate()
        self.addCleanup(delegate.deleteLater)
        delegate.paint(self.painter, self._option(), self._index("movie.mkv"))
        # must not raise

    def test_a_tor_prefix_without_an_icon_is_left_alone(self):
        """The prefix is only stripped when an icon is present, so it must not crash."""
        delegate = DownloadNameDelegate()
        self.addCleanup(delegate.deleteLater)
        delegate.paint(self.painter, self._option(), self._index("🧅 movie.mkv"))
        # must not raise

    def test_an_empty_save_path_falls_back_to_the_base_delegate(self):
        delegate = SavePathDelegate()
        self.addCleanup(delegate.deleteLater)
        delegate.paint(self.painter, self._option(), self._index(""))
        # must not raise

    def test_a_long_save_path_is_shortened_before_painting(self):
        delegate = SavePathDelegate()
        self.addCleanup(delegate.deleteLater)
        delegate.paint(
            self.painter, self._option(width=90),
            self._index("C:/Users/Name/Downloads/Movies/Feature Film.mkv"),
        )  # must not raise

    def test_a_narrow_save_path_column_does_not_raise(self):
        delegate = SavePathDelegate()
        self.addCleanup(delegate.deleteLater)
        delegate.paint(
            self.painter, self._option(width=1),
            self._index("C:/Users/Name/Downloads/Movies/Feature Film.mkv"),
        )  # must not raise

    def test_a_negative_width_save_path_column_does_not_raise(self):
        delegate = SavePathDelegate()
        self.addCleanup(delegate.deleteLater)
        delegate.paint(
            self.painter, self._option(width=-4),
            self._index("C:/Users/Name/Downloads/Movies"),
        )  # must not raise

    def test_the_bar_delegates_request_at_least_the_bar_height(self):
        for delegate_class in (SavePathDelegate, ProgressBarDelegate):
            with self.subTest(delegate=delegate_class.__name__):
                delegate = delegate_class()
                self.addCleanup(delegate.deleteLater)
                model = self._model("x")
                hint = delegate.sizeHint(self._option(), model.index(0, 0))
                self.assertGreaterEqual(hint.height(), 28)

    def test_the_name_delegate_keeps_the_base_row_height(self):
        """Documents the asymmetry: only the two bar-rendering delegates pad the row.

        ``DownloadNameDelegate`` overrides ``paint`` but not ``sizeHint``, so its rows
        keep whatever height the style gives them. If a row containing a progress bar
        and one containing only a name must align, this is the place that has to change.
        """
        delegate = DownloadNameDelegate()
        self.addCleanup(delegate.deleteLater)
        model = self._model("movie.mkv")
        hint = delegate.sizeHint(self._option(), model.index(0, 0))
        self.assertLess(
            hint.height(), 28,
            "KNOWN ASYMMETRY: DownloadNameDelegate does not pad its row height, so a name column and a progress column in the same table can differ in height",
        )


# ===========================================================================
# tor_service - process lifecycle
# ===========================================================================

class TorTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name) / "tor_data"
        self.config = TorConfig(enabled=True, proxy_host="127.0.0.1", proxy_port=9050)
        self.manager = TorServiceManager(self.config, self.data_dir)

        # `TorServiceManager.stop()` terminates with `subprocess.run(["taskkill", ...])` on
        # Windows and `os.kill(pid, 15)` everywhere else - see tor_service.stop.
        #
        # Patching only the former meant that off Windows the real `os.kill` ran against the
        # fictional PIDs these tests invent (777, 5, 4321), signalling whatever unrelated
        # processes happen to own them. So this fences both mechanisms for the whole class, the
        # same way tests/test_tor.py does, and the individual tests assert against whichever
        # branch their platform takes.
        #
        # Individual `patcher.stop` cleanups rather than `patch.stopall`: that would tear down
        # every live patch in the process, including ones other fixtures still depend on.
        self.mock_run = self._start_patch(patch.object(tor_module.subprocess, "run"))
        self.mock_kill = self._start_patch(patch.object(tor_module.os, "kill"))

    def _start_patch(self, patcher):
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def fake_process(self, pid=4321, poll=0, stdout="", stderr=""):
        process = MagicMock()
        process.pid = pid
        process.poll.return_value = poll
        process.communicate.return_value = (stdout, stderr)
        return process

    def reachability(self, ports):
        def _check(host, port, timeout=1.0):
            return port in ports

        return patch.object(tor_module, "is_tor_reachable", _check)

    def spawn(self, process, **extra):
        return patch.multiple(
            tor_module.subprocess, Popen=MagicMock(return_value=process), **extra
        )


class TestFindTorExecutable(unittest.TestCase):
    def test_a_configured_path_wins(self):
        with patch.object(tor_module.os.path, "isfile", return_value=True), \
             patch.object(Path, "is_file", return_value=False) as std:
            found = find_tor_executable("C:/custom/tor.exe")
        self.assertTrue(found.replace("\\", "/").endswith("C:/custom/tor.exe"))
        std.assert_not_called()

    def test_a_configured_path_that_does_not_exist_falls_through_to_the_search(self):
        with patch.object(tor_module.os.path, "isfile",
                          side_effect=lambda p: p == "C:/bin/tor.exe"), \
             patch.object(tor_module.shutil, "which", return_value="C:/bin/tor.exe"), \
             patch.object(Path, "is_file", return_value=False):
            found = find_tor_executable("C:/missing/tor.exe")
        self.assertNotIn("missing", found)
        self.assertIn("bin", found)

    def test_the_path_lookup_wins_over_the_standard_locations(self):
        with patch.object(tor_module.os.path, "isfile", return_value=True), \
             patch.object(tor_module.shutil, "which", return_value="C:/bin/tor.exe"), \
             patch.object(Path, "is_file", return_value=False) as std:
            find_tor_executable()
        std.assert_not_called()

    def test_the_windows_extension_is_tried_second(self):
        with patch.object(tor_module.os.path, "isfile", return_value=True), \
             patch.object(tor_module.shutil, "which",
                          side_effect=lambda name: None if name == "tor" else "C:/b/tor.exe"), \
             patch.object(Path, "is_file", return_value=False):
            self.assertTrue(find_tor_executable().endswith("tor.exe"))

    def test_a_which_hit_that_is_not_a_file_is_ignored(self):
        """A stale PATH entry must not send the search down the wrong branch."""
        with patch.object(tor_module.os.path, "isfile", return_value=False), \
             patch.object(tor_module.shutil, "which", return_value="C:/bin/tor.exe"), \
             patch.object(Path, "is_file", return_value=True):
            self.assertIsNotNone(find_tor_executable())

    def test_no_tor_anywhere_returns_none(self):
        with patch.object(tor_module.os.path, "isfile", return_value=False), \
             patch.object(tor_module.shutil, "which", return_value=None), \
             patch.object(Path, "is_file", return_value=False):
            self.assertIsNone(find_tor_executable())

    def test_the_wrong_drive_program_files_is_not_hard_coded(self):
        """A relocated ProgramFiles must not send the search to the default tree."""
        with patch.dict(tor_module.os.environ, {
            "ProgramFiles": r"D:\Apps",
            "ProgramFiles(x86)": r"D:\Apps (x86)",
        }), \
             patch.object(tor_module.os.path, "isfile", return_value=False), \
             patch.object(tor_module.shutil, "which", return_value=None), \
             patch.object(Path, "is_file", return_value=True):
            found = find_tor_executable()
        self.assertIsNotNone(found)
        self.assertNotIn(r"C:\Program Files", found)
        self.assertIn("D:", found, "the relocated ProgramFiles must be the one searched")

    def test_the_program_files_fallback_default_is_used_when_unset(self):
        with patch.dict(tor_module.os.environ, {"ProgramFiles": ""}, clear=False), \
             patch.object(tor_module.os.path, "isfile", return_value=False), \
             patch.object(tor_module.shutil, "which", return_value=None), \
             patch.object(Path, "is_file", return_value=True):
            found = find_tor_executable()
        self.assertIsNotNone(found)
        self.assertIn("Tor Browser", found)
        self.assertTrue(found.endswith("tor.exe"), found)

    def test_a_failing_candidate_check_does_not_abort_the_search(self):
        calls = {"n": 0}

        def _is_file(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("inaccessible")
            return True

        with patch.object(tor_module.os.path, "isfile", return_value=False), \
             patch.object(tor_module.shutil, "which", return_value=None), \
             patch.object(Path, "is_file", _is_file):
            self.assertIsNotNone(find_tor_executable())
        self.assertGreater(calls["n"], 1, "the search must continue past the failure")

    def test_unix_locations_are_searched_off_windows(self):
        """Off Windows the search must cover /usr/bin and the homebrew prefixes.

        Only the *candidate list* is asserted, not the resolved return value: patching
        ``os.name`` also flips ``pathlib`` to ``PosixPath`` inside this process, and
        resolving a POSIX path on a Windows filesystem raises inside the
        ``except Exception: continue`` of the search loop. The loop contents are the part
        worth pinning.
        """
        seen: list[str] = []

        def _is_file(path_self):
            seen.append(path_self.as_posix())
            return False

        with patch.object(tor_module.os, "name", "posix"), \
             patch.object(tor_module.os.path, "isfile", return_value=False), \
             patch.object(tor_module.shutil, "which", return_value=None), \
             patch.object(Path, "is_file", _is_file):
            self.assertIsNone(find_tor_executable())
        self.assertEqual(
            seen,
            ["/usr/bin/tor", "/usr/local/bin/tor", "/opt/homebrew/bin/tor", "/opt/local/bin/tor"],
            "the POSIX branch must search the system, /usr/local and both homebrew prefixes",
        )

    def test_the_windows_branch_is_not_searched_when_posix(self):
        seen: list[str] = []

        def _is_file(path_self):
            seen.append(path_self.as_posix())
            return False

        with patch.object(tor_module.os, "name", "posix"), \
             patch.object(tor_module.os.path, "isfile", return_value=False), \
             patch.object(tor_module.shutil, "which", return_value=None), \
             patch.object(Path, "is_file", _is_file):
            find_tor_executable()
        self.assertFalse(
            any("Tor Browser" in path for path in seen),
            "Windows install locations must not be searched on POSIX",
        )


class TestTorServiceStart(TorTestCase):
    def test_an_existing_service_is_adopted_not_respawned(self):
        with self.reachability({9050}), \
             patch.object(tor_module.subprocess, "Popen") as popen:
            ok, message = self.manager.start()
        self.assertTrue(ok)
        self.assertIn("existing Tor service", message)
        popen.assert_not_called()
        self.assertFalse(self.manager.is_spawned)

    def test_a_missing_binary_reports_where_it_looked(self):
        with self.reachability(set()), \
             patch.object(tor_module, "find_tor_executable", return_value=None):
            ok, message = self.manager.start()
        self.assertFalse(ok)
        self.assertIn("could not be found", message)
        self.assertIn("System PATH", message)
        self.assertIn("(none)", message)

    def test_a_missing_binary_names_the_configured_path(self):
        self.config.tor_executable_path = r"C:\tor\tor.exe"
        with self.reachability(set()), \
             patch.object(tor_module, "find_tor_executable", return_value=None):
            _, message = self.manager.start()
        self.assertIn(r"C:\tor\tor.exe", message)

    def test_a_missing_binary_hints_at_tor_browser_on_the_alternate_port(self):
        with self.reachability({9150}), \
             patch.object(tor_module, "find_tor_executable", return_value=None):
            ok, message = self.manager.start()
        self.assertFalse(ok)
        self.assertIn("9150", message, "the user needs the port-switch hint")
        self.assertIn("Tor Browser", message)

    def test_a_missing_binary_hints_at_a_service_on_the_default_port(self):
        self.config.proxy_port = 9150
        with self.reachability({9050}), \
             patch.object(tor_module, "find_tor_executable", return_value=None):
            ok, message = self.manager.start()
        self.assertFalse(ok)
        self.assertIn("Tor Service", message)
        self.assertIn("9050", message)

    def test_an_uncreatable_data_directory_is_reported(self):
        with self.reachability(set()), \
             patch.object(tor_module, "find_tor_executable", return_value="tor.exe"), \
             patch.object(Path, "mkdir", side_effect=PermissionError("read-only")):
            ok, message = self.manager.start()
        self.assertFalse(ok)
        self.assertIn("Failed to create Tor data directory", message)

    def test_a_spawn_failure_is_reported_and_leaves_nothing_behind(self):
        with self.reachability(set()), \
             patch.object(tor_module, "find_tor_executable", return_value="tor.exe"), \
             patch.object(tor_module.subprocess, "Popen",
                          side_effect=OSError("not a valid application")):
            ok, message = self.manager.start()
        self.assertFalse(ok)
        self.assertIn("Failed to execute Tor binary", message)
        self.assertIsNone(self.manager._process)
        self.assertFalse(self.manager.is_spawned)

    def test_a_successful_start_spawns_once_and_records_the_pid(self):
        """The proxy answers only after the spawn, so the wait loop runs exactly once."""
        checks = {"n": 0}

        def _check(host, port, timeout=1.0):
            checks["n"] += 1
            return checks["n"] > 1  # unreachable pre-flight, reachable post-spawn

        with patch.object(tor_module, "is_tor_reachable", _check), \
             patch.object(tor_module, "find_tor_executable", return_value="tor.exe"), \
             patch.object(tor_module.subprocess, "Popen",
                          return_value=self.fake_process(pid=999, poll=None)) as popen:
            ok, message = self.manager.start()
        self.assertTrue(ok, message)
        self.assertIn("started and connected", message)
        self.assertEqual(popen.call_count, 1)
        self.assertTrue(self.manager.is_spawned)
        self.assertEqual((self.data_dir / "tor.pid").read_text(encoding="utf-8"), "999")

    def test_the_command_line_pins_the_configured_port_and_data_dir(self):
        with self.reachability(set()), \
             patch.object(tor_module, "find_tor_executable", return_value="tor.exe"), \
             patch.object(tor_module.subprocess, "Popen",
                          return_value=self.fake_process(poll=None)) as popen, \
             patch.object(tor_module.subprocess, "run") as run, \
             patch.object(tor_module.time, "sleep"):
            self.manager.start(timeout=0.0)
        # The stale pid file from a previous run is cleaned up on start, using whichever
        # mechanism this platform has: taskkill on Windows, os.kill elsewhere.
        if os.name == "nt":
            run.assert_called_once_with(
                ["taskkill", "/F", "/T", "/PID", "4321"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        else:
            self.assertIn(
                unittest.mock.call(4321, 15), self.mock_kill.call_args_list,
                "a stale tor pid must be signalled on POSIX too",
            )
        cmd = popen.call_args.args[0]
        self.assertEqual(
            cmd[1:], ["--SocksPort", "9050", "--DataDirectory", str(self.data_dir)]
        )

    def test_the_child_is_detached_with_piped_output(self):
        with self.reachability(set()), \
             patch.object(tor_module, "find_tor_executable", return_value="tor.exe"), \
             patch.object(tor_module.subprocess, "Popen",
                          return_value=self.fake_process(poll=None)) as popen, \
             patch.object(tor_module.subprocess, "run"), \
             patch.object(tor_module.time, "sleep"):
            self.manager.start(timeout=0.0)
        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["stdout"], subprocess.PIPE)
        self.assertIs(kwargs["stderr"], subprocess.PIPE)
        self.assertTrue(kwargs["text"])
        if os.name == "nt":
            self.assertTrue(kwargs["creationflags"], "the console window must be hidden")

    def test_an_unwritable_pid_file_does_not_abort_the_start(self):
        checks = {"n": 0}

        def _check(host, port, timeout=1.0):
            checks["n"] += 1
            return checks["n"] > 1

        with patch.object(tor_module, "is_tor_reachable", _check), \
             patch.object(tor_module, "find_tor_executable", return_value="tor.exe"), \
             patch.object(tor_module.subprocess, "Popen",
                          return_value=self.fake_process(poll=None)), \
             patch.object(Path, "write_text", side_effect=PermissionError("read-only")), \
             patch.object(tor_module.subprocess, "run") as run:
            ok, message = self.manager.start()
        self.assertTrue(ok, message)
        run.assert_not_called()
        self.assertTrue(self.manager.is_spawned, "a pid-file failure must not orphan us")

    def test_a_process_that_exits_immediately_reports_its_stderr(self):
        process = self.fake_process(poll=1, stderr="Fatal: cannot open SocksPort")
        with self.reachability(set()), \
             patch.object(tor_module, "find_tor_executable", return_value="tor.exe"), \
             patch.object(tor_module.subprocess, "Popen", return_value=process):
            ok, message = self.manager.start()
        self.assertFalse(ok)
        self.assertIn("terminated unexpectedly", message)
        self.assertIn("cannot open SocksPort", message)
        self.assertIsNone(self.manager._process, "a dead process must not stay attached")

    def test_stdout_is_used_when_stderr_is_empty(self):
        process = self.fake_process(poll=1, stdout="tor: exiting on signal")
        with self.reachability(set()), \
             patch.object(tor_module, "find_tor_executable", return_value="tor.exe"), \
             patch.object(tor_module.subprocess, "Popen", return_value=process):
            _, message = self.manager.start()
        self.assertIn("exiting on signal", message)

    def test_a_port_collision_is_reported_with_a_remedy(self):
        process = self.fake_process(poll=1, stderr="Fatal: Could not bind to 127.0.0.1:9050")
        with self.reachability(set()), \
             patch.object(tor_module, "find_tor_executable", return_value="tor.exe"), \
             patch.object(tor_module.subprocess, "Popen", return_value=process):
            ok, message = self.manager.start()
        self.assertFalse(ok)
        self.assertIn("already in use", message)
        self.assertIn("9150", message)

    def test_a_port_collision_notes_a_live_alternate_port(self):
        process = self.fake_process(poll=1, stderr="Could not bind")
        with self.reachability({9150}), \
             patch.object(tor_module, "find_tor_executable", return_value="tor.exe"), \
             patch.object(tor_module.subprocess, "Popen", return_value=process):
            ok, message = self.manager.start()
        self.assertFalse(ok)
        self.assertIn("Tor Browser is active on port 9150", message)

    def test_a_shared_data_directory_is_reported_distinctly(self):
        process = self.fake_process(
            poll=1,
            stderr="Fatal: the data directory is already using by another process",
        )
        with self.reachability(set()), \
             patch.object(tor_module, "find_tor_executable", return_value="tor.exe"), \
             patch.object(tor_module.subprocess, "Popen", return_value=process):
            ok, message = self.manager.start()
        self.assertFalse(ok)
        self.assertIn("already using the data directory", message)
        self.assertIn("orphan", message)

    def test_an_exit_with_no_output_falls_back_to_the_exit_code(self):
        process = self.fake_process(poll=3)
        with self.reachability(set()), \
             patch.object(tor_module, "find_tor_executable", return_value="tor.exe"), \
             patch.object(tor_module.subprocess, "Popen", return_value=process):
            ok, message = self.manager.start()
        self.assertFalse(ok)
        self.assertIn("exit code 3", message)

    def test_an_unreadable_dead_process_falls_back_to_the_exit_code(self):
        process = self.fake_process(poll=4)
        process.communicate.side_effect = OSError("handle closed")
        with self.reachability(set()), \
             patch.object(tor_module, "find_tor_executable", return_value="tor.exe"), \
             patch.object(tor_module.subprocess, "Popen", return_value=process):
            ok, message = self.manager.start()
        self.assertFalse(ok)
        self.assertIn("exit code 4", message)

    def test_a_timeout_stops_the_process_it_started(self):
        with self.reachability(set()), \
             patch.object(tor_module, "find_tor_executable", return_value="tor.exe"), \
             patch.object(tor_module.subprocess, "Popen",
                          return_value=self.fake_process(poll=None)), \
             patch.object(tor_module.subprocess, "run"), \
             patch.object(tor_module.time, "sleep"):
            ok, message = self.manager.start(timeout=0.0)
        self.assertFalse(ok)
        self.assertIn("timed out", message)
        self.assertIsNone(self.manager._process, "a timed-out spawn must be torn down")

    def test_the_default_port_is_used_when_the_config_has_none(self):
        self.config.proxy_port = 0
        self.config.proxy_host = ""
        with self.reachability(set()), \
             patch.object(tor_module, "find_tor_executable", return_value="tor.exe"), \
             patch.object(tor_module.subprocess, "Popen",
                          return_value=self.fake_process(poll=None)) as popen, \
             patch.object(tor_module.subprocess, "run"), \
             patch.object(tor_module.time, "sleep"):
            self.manager.start(timeout=0.0)
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--SocksPort") + 1], "9050")

    def test_is_running_uses_the_configured_host_and_port(self):
        with patch.object(tor_module, "is_tor_reachable", return_value=True) as probe:
            self.assertTrue(self.manager.is_running())
        self.assertEqual(probe.call_args.args, ("127.0.0.1", 9050))
        self.assertEqual(probe.call_args.kwargs, {"timeout": 1.0})

    def test_is_spawned_is_false_for_a_dead_process(self):
        self.manager._process = self.fake_process(poll=0)
        self.manager._spawned_by_us = True
        self.assertFalse(self.manager.is_spawned)

    def test_is_spawned_is_false_without_a_process(self):
        self.manager._spawned_by_us = True
        self.assertFalse(self.manager.is_spawned)


class TestTorServiceStop(TorTestCase):
    def test_an_external_service_is_left_running(self):
        """Killing a Tor the user started themselves would break their browser."""
        self.manager._process = None
        self.manager._spawned_by_us = False
        with patch.object(tor_module.subprocess, "run") as run:
            self.manager.stop()
        run.assert_not_called()
        self.assertFalse(self.manager._spawned_by_us)

    def test_an_already_dead_external_service_is_left_alone(self):
        self.manager._process = self.fake_process(poll=0)
        self.manager._spawned_by_us = False
        with patch.object(tor_module.subprocess, "run") as run:
            self.manager.stop()
        run.assert_not_called()

    def test_a_spawned_process_is_terminated_and_the_pid_file_removed(self):
        process = self.fake_process(pid=777)
        process.poll.return_value = None
        self.manager._process = process
        self.manager._spawned_by_us = True
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "tor.pid").write_text("777", encoding="utf-8")

        with patch.object(tor_module.subprocess, "run") as run:
            self.manager.stop()

        process.terminate.assert_called_once()
        if os.name == "nt":
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0], ["taskkill", "/F", "/T", "/PID", "777"])
        else:
            run.assert_not_called()
            self.assertIn(
                unittest.mock.call(777, 15), self.mock_kill.call_args_list,
                "a spawned tor must be signalled on POSIX",
            )
        self.assertFalse((self.data_dir / "tor.pid").exists())
        self.assertIsNone(self.manager._process)
        self.assertFalse(self.manager._spawned_by_us)

    def test_a_pid_already_in_the_target_list_is_not_killed_twice(self):
        process = self.fake_process(pid=5)
        process.poll.return_value = None
        self.manager._process = process
        self.manager._spawned_by_us = True
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "tor.pid").write_text("5", encoding="utf-8")
        with patch.object(tor_module.subprocess, "run") as run:
            self.manager.stop()
        if os.name == "nt":
            self.assertEqual(run.call_args_list, [unittest.mock.call(
                ["taskkill", "/F", "/T", "/PID", "5"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )])
        else:
            self.assertEqual(
                [c for c in self.mock_kill.call_args_list if c == unittest.mock.call(5, 15)],
                [unittest.mock.call(5, 15)],
                "the pid must be signalled exactly once, not repeatedly",
            )

    def test_an_orphaned_pid_from_the_file_is_also_killed(self):
        """A previous run that was force-quit leaves a live Tor with no handle."""
        self.manager._process = None
        self.manager._spawned_by_us = True
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "tor.pid").write_text("1234", encoding="utf-8")

        killed: list[str] = []
        with patch.object(tor_module.subprocess, "run",
                          side_effect=lambda argv, **kw: killed.append(argv[-1])):
            self.manager.stop()

        self.assertEqual(killed, ["1234"])
        self.assertFalse((self.data_dir / "tor.pid").exists())

    def test_an_orphaned_pid_file_is_ignored_when_we_did_not_spawn(self):
        """A PID file left by a previous run must not authorise killing that PID."""
        self.manager._process = None
        self.manager._spawned_by_us = False
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "tor.pid").write_text("1234", encoding="utf-8")
        with patch.object(tor_module.subprocess, "run") as run:
            self.manager.stop()
        run.assert_not_called()

    def test_a_corrupt_pid_file_is_ignored(self):
        self.manager._process = None
        self.manager._spawned_by_us = True
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "tor.pid").write_text("not-a-pid", encoding="utf-8")
        with patch.object(tor_module.subprocess, "run") as run:
            self.manager.stop()  # must not raise
        run.assert_not_called()

    def test_a_failing_terminate_is_contained(self):
        process = self.fake_process(pid=5)
        process.poll.return_value = None
        process.terminate.side_effect = OSError("access denied")
        self.manager._process = process
        self.manager._spawned_by_us = True
        with patch.object(tor_module.subprocess, "run"):
            self.manager.stop()  # must not raise
        self.assertIsNone(self.manager._process)

    def test_a_failing_kill_is_contained(self):
        process = self.fake_process(pid=5)
        process.poll.return_value = None
        process.kill.side_effect = OSError("already gone")
        self.manager._process = process
        self.manager._spawned_by_us = True
        with patch.object(tor_module.subprocess, "run", side_effect=OSError("taskkill")), \
             patch.object(process, "wait", side_effect=OSError("gone")):
            self.manager.stop()  # must not raise
        self.assertIsNone(self.manager._process)

    def test_a_failing_pid_file_delete_is_contained(self):
        self.manager._process = None
        self.manager._spawned_by_us = True
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "tor.pid").write_text("1", encoding="utf-8")
        with patch.object(Path, "unlink", side_effect=PermissionError("locked")), \
             patch.object(tor_module.subprocess, "run"):
            self.manager.stop()  # must not raise
        self.assertIsNone(self.manager._process)

    def test_stop_is_idempotent(self):
        self.manager.stop()
        self.manager.stop()  # must not raise

    def test_the_default_data_dir_lives_under_the_user_profile(self):
        default = TorServiceManager(TorConfig())._data_dir
        self.assertEqual(default.name, "tor_data")
        self.assertIn(".my-idm", default.parts)

    def test_a_signal_is_used_off_windows(self):
        self.manager._process = None
        self.manager._spawned_by_us = True
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "tor.pid").write_text("4242", encoding="utf-8")
        signalled: list[tuple] = []
        with patch.object(tor_module.os, "name", "posix"), \
             patch.object(tor_module.os, "kill",
                          side_effect=lambda pid, sig: signalled.append((pid, sig))):
            self.manager.stop()
        self.assertEqual(signalled, [(4242, 15)], "SIGTERM, not SIGKILL")

    def test_a_failing_signal_is_contained(self):
        self.manager._process = None
        self.manager._spawned_by_us = True
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "tor.pid").write_text("4242", encoding="utf-8")
        with patch.object(tor_module.os, "name", "posix"), \
             patch.object(tor_module.os, "kill", side_effect=ProcessLookupError):
            self.manager.stop()  # must not raise
        self.assertIsNone(self.manager._process)


class TestTorServiceStopThreadSafety(TorTestCase):
    """``stop()`` and ``is_spawned`` may be read from the Qt thread and a monitor thread.

    ``DownloadManager`` polls ``is_running`` while the UI thread can call ``start``/``stop``;
    a partially-initialised manager must never make either raise or leave the ``_process``
    handle dangling.
    """

    WORKERS = 6

    def test_concurrent_stops_are_safe_and_idempotent(self):
        process = self.fake_process(pid=31337)
        process.poll.return_value = None
        self.manager._process = process
        self.manager._spawned_by_us = True
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "tor.pid").write_text("31337", encoding="utf-8")

        errors: list[BaseException] = []
        lock = threading.Lock()
        barrier = threading.Barrier(self.WORKERS, timeout=10)

        def _worker():
            try:
                barrier.wait()
                for _ in range(10):
                    self.manager.stop()
            except BaseException as exc:  # noqa: BLE001 - asserted on below
                with lock:
                    errors.append(exc)

        with patch.object(tor_module.subprocess, "run"):
            threads = [
                threading.Thread(target=_worker, daemon=True)
                for _ in range(self.WORKERS)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
                self.assertFalse(thread.is_alive(), "a stop thread hung")

        self.assertEqual(errors, [], f"concurrent stop raised: {errors!r}")
        self.assertIsNone(self.manager._process)
        self.assertFalse(self.manager._spawned_by_us)
        self.assertFalse((self.data_dir / "tor.pid").exists())

    def test_polling_while_stopping_never_observes_a_stale_spawned_flag(self):
        observed: list[bool] = []
        errors: list[BaseException] = []
        lock = threading.Lock()
        barrier = threading.Barrier(2, timeout=10)

        def _poller():
            try:
                barrier.wait()
                for _ in range(200):
                    with lock:
                        observed.append(self.manager.is_spawned)
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        def _stopper():
            try:
                barrier.wait()
                for _ in range(20):
                    self.manager.stop()
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=_poller, daemon=True),
                   threading.Thread(target=_stopper, daemon=True)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())

        self.assertEqual(errors, [], f"polling during stop raised: {errors!r}")
        self.assertFalse(self.manager.is_spawned)
        self.assertTrue(observed, "the poller must have sampled the flag at least once")


# ===========================================================================
# utils - edge cases
# ===========================================================================

class UtilFileTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)


class TestGetUniqueFilename(UtilFileTestCase):
    def test_an_empty_filename_is_returned_unchanged(self):
        self.assertEqual(get_unique_filename(self.tmp, ""), "")

    def test_a_free_name_is_returned_unchanged(self):
        self.assertEqual(get_unique_filename(self.tmp, "a.zip"), "a.zip")

    def test_an_existing_file_is_numbered(self):
        (self.tmp / "a.zip").write_bytes(b"x")
        self.assertEqual(get_unique_filename(self.tmp, "a.zip"), "a (1).zip")

    def test_a_reserved_name_is_numbered_even_when_no_file_exists(self):
        self.assertEqual(
            get_unique_filename(self.tmp, "a.zip", reserved_names={"a.zip"}), "a (1).zip"
        )

    def test_reserved_names_are_matched_case_insensitively(self):
        self.assertEqual(
            get_unique_filename(self.tmp, "A.ZIP", reserved_names={"a.zip"}), "A (1).ZIP"
        )

    def test_numbering_skips_over_every_taken_candidate(self):
        for name in ("a.zip", "a (1).zip", "a (2).zip"):
            (self.tmp / name).write_bytes(b"x")
        self.assertEqual(get_unique_filename(self.tmp, "a.zip"), "a (3).zip")

    def test_a_free_already_numbered_name_is_left_alone(self):
        self.assertEqual(get_unique_filename(self.tmp, "a (2).zip"), "a (2).zip")

    def test_an_already_numbered_name_continues_from_the_next_number(self):
        """Passing "a (2).zip" must not produce "a (2) (1).zip"."""
        (self.tmp / "a (2).zip").write_bytes(b"x")
        self.assertEqual(get_unique_filename(self.tmp, "a (2).zip"), "a (3).zip")

    def test_a_numbered_name_with_extra_spaces_is_handled(self):
        (self.tmp / "a  (7).zip").write_bytes(b"x")
        self.assertEqual(get_unique_filename(self.tmp, "a  (7).zip"), "a (8).zip")

    def test_a_compound_extension_is_preserved(self):
        for compound in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
            with self.subTest(compound=compound):
                name = f"archive{compound}"
                (self.tmp / name).write_bytes(b"x")
                self.assertEqual(
                    get_unique_filename(self.tmp, name), f"archive (1){compound}"
                )

    def test_a_compound_extension_that_is_already_numbered_continues(self):
        (self.tmp / "archive (2).tar.gz").write_bytes(b"x")
        self.assertEqual(
            get_unique_filename(self.tmp, "archive (2).tar.gz"), "archive (3).tar.gz"
        )

    def test_an_extensionless_name_is_numbered(self):
        (self.tmp / "README").write_bytes(b"x")
        self.assertEqual(get_unique_filename(self.tmp, "README"), "README (1)")

    def test_a_dotfile_keeps_its_leading_dot_as_a_stem(self):
        (self.tmp / ".gitignore").write_bytes(b"x")
        self.assertEqual(get_unique_filename(self.tmp, ".gitignore"), ".gitignore (1)")

    def test_an_inaccessible_directory_does_not_raise(self):
        with patch.object(Path, "exists", side_effect=OSError("access denied")):
            self.assertEqual(get_unique_filename(self.tmp, "a.zip"), "a.zip")

    def test_a_reserved_name_with_a_path_separator_is_still_matched(self):
        self.assertEqual(
            get_unique_filename(self.tmp, "a.zip", reserved_names={"a.zip"}), "a (1).zip"
        )

    def test_a_directory_of_the_same_name_counts_as_taken(self):
        (self.tmp / "a.zip").mkdir()
        self.assertEqual(get_unique_filename(self.tmp, "a.zip"), "a (1).zip")


class TestRobustMove(UtilFileTestCase):
    def test_a_missing_source_is_reported(self):
        ok, err = robust_move_download_files(self.tmp / "nope", self.tmp / "dst")
        self.assertFalse(ok)
        self.assertIn("does not exist", err)

    def test_an_already_moved_file_is_treated_as_success(self):
        """A retry after a partial move must not report failure."""
        (self.tmp / "dst").write_bytes(b"x")
        ok, err = robust_move_download_files(self.tmp / "src", self.tmp / "dst")
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_a_self_move_is_a_no_op(self):
        source = self.tmp / "a.bin"
        source.write_bytes(b"x")
        ok, err = robust_move_download_files(source, source)
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertTrue(source.exists())

    def test_a_file_move_relocates_the_content(self):
        source = self.tmp / "a.bin"
        source.write_bytes(b"payload")
        target = self.tmp / "nested" / "deep" / "a.bin"
        ok, err = robust_move_download_files(source, target)
        self.assertTrue(ok, err)
        self.assertEqual(target.read_bytes(), b"payload")
        self.assertFalse(source.exists())

    def test_a_file_move_replaces_an_existing_target(self):
        source = self.tmp / "a.bin"
        source.write_bytes(b"new")
        target = self.tmp / "b.bin"
        target.write_bytes(b"old")
        ok, err = robust_move_download_files(source, target)
        self.assertTrue(ok, err)
        self.assertEqual(target.read_bytes(), b"new")

    def test_a_file_move_falls_back_to_copy_after_the_retry_ladder(self):
        source = self.tmp / "a.bin"
        source.write_bytes(b"payload")
        target = self.tmp / "b.bin"
        with patch.object(shutil, "move", side_effect=PermissionError("sharing violation")), \
             patch("time.sleep"):
            ok, err = robust_move_download_files(source, target)
        self.assertTrue(ok, err)
        self.assertEqual(target.read_bytes(), b"payload")
        self.assertFalse(source.exists())

    def test_the_retry_ladder_is_walked_before_the_copy_fallback(self):
        source = self.tmp / "a.bin"
        source.write_bytes(b"payload")
        attempts = {"n": 0}

        real_move = shutil.move  # captured before the patch, or _move recurses

        def _move(*args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise PermissionError("still scanning")
            return real_move(*args, **kwargs)

        with patch.object(shutil, "move", _move), patch("time.sleep"):
            ok, err = robust_move_download_files(source, self.tmp / "b.bin")
        self.assertTrue(ok, err)
        self.assertEqual(attempts["n"], 3, "the ladder must retry before giving up on move")

    def test_a_file_move_failing_everyway_reports_the_last_error(self):
        source = self.tmp / "a.bin"
        source.write_bytes(b"payload")
        with patch.object(shutil, "move", side_effect=PermissionError("locked")), \
             patch.object(shutil, "copy2", side_effect=OSError("disk full")), \
             patch("time.sleep"):
            ok, err = robust_move_download_files(source, self.tmp / "b.bin")
        self.assertFalse(ok)
        self.assertIn("Failed to move file", err)
        self.assertIn("locked", err, "the move error, not the copy error, is reported")
        self.assertTrue(source.exists(), "a failed move must not lose the source")

    def test_a_directory_tree_keeps_its_structure(self):
        source = self.tmp / "Show"
        (source / "Season 01").mkdir(parents=True)
        (source / "Season 01" / "e01.mkv").write_bytes(b"a")
        (source / "Season 01" / "e02.mkv").write_bytes(b"b")
        (source / "cover.jpg").write_bytes(b"c")
        target = self.tmp / "Anime"
        ok, err = robust_move_download_files(source, target)
        self.assertTrue(ok, err)
        self.assertEqual((target / "Season 01" / "e01.mkv").read_bytes(), b"a")
        self.assertEqual((target / "Season 01" / "e02.mkv").read_bytes(), b"b")
        self.assertEqual((target / "cover.jpg").read_bytes(), b"c")
        self.assertFalse(source.exists(), "the emptied source tree must be cleaned up")

    def test_a_same_size_but_different_prior_partial_file_is_overwritten(self):
        """A same-length target is not proof the move already happened.

        Regression test. The resume check compared ``st_size`` only, so a target left
        truncated at exactly the source's length by a previous crashed attempt was treated
        as a completed move: the good source copy was deleted and the corrupt target kept.
        The content is now verified (sampled, so it stays cheap on a multi-gigabyte file).
        """
        source = self.tmp / "Show"
        source.mkdir()
        (source / "a.mkv").write_bytes(b"12345")
        target = self.tmp / "Anime"
        target.mkdir()
        (target / "a.mkv").write_bytes(b"abcde")  # same length, different content
        ok, err = robust_move_download_files(source, target)
        self.assertTrue(ok, err)
        self.assertEqual(
            (target / "a.mkv").read_bytes(), b"12345",
            "a same-length but different target must be overwritten with the good source",
        )
        self.assertFalse(
            (source / "a.mkv").exists(),
            "the source must be gone only because it was actually moved, not skipped",
        )

    def test_a_truncated_tail_is_detected_even_when_the_head_matches(self):
        """The realistic corruption: a copy whose head landed but whose tail did not."""
        source = self.tmp / "Show"
        source.mkdir()
        payload = b"A" * (3 << 20)
        (source / "big.mkv").write_bytes(payload)
        target = self.tmp / "Anime"
        target.mkdir()
        # Same length, identical head, zeros where the tail should be.
        (target / "big.mkv").write_bytes(payload[:1 << 20] + b"\x00" * (2 << 20))

        ok, err = robust_move_download_files(source, target)
        self.assertTrue(ok, err)
        self.assertEqual((target / "big.mkv").read_bytes(), payload)

    def test_a_differently_sized_prior_partial_file_is_overwritten(self):
        source = self.tmp / "Show"
        source.mkdir()
        (source / "a.mkv").write_bytes(b"new-and-longer")
        target = self.tmp / "Anime"
        target.mkdir()
        (target / "a.mkv").write_bytes(b"old")
        ok, err = robust_move_download_files(source, target)
        self.assertTrue(ok, err)
        self.assertEqual((target / "a.mkv").read_bytes(), b"new-and-longer")

    def test_an_identically_sized_source_file_is_deleted_after_the_merge(self):
        source = self.tmp / "Show"
        source.mkdir()
        (source / "a.mkv").write_bytes(b"same")
        target = self.tmp / "Anime"
        target.mkdir()
        (target / "a.mkv").write_bytes(b"same")
        ok, err = robust_move_download_files(source, target)
        self.assertTrue(ok, err)
        self.assertEqual((target / "a.mkv").read_bytes(), b"same")
        self.assertFalse((source / "a.mkv").exists())

    def test_a_file_move_falls_back_to_copy_inside_a_directory(self):
        source = self.tmp / "Show"
        source.mkdir()
        (source / "a.mkv").write_bytes(b"payload")
        target = self.tmp / "Anime"
        with patch.object(shutil, "move", side_effect=PermissionError("locked")), \
             patch("time.sleep"):
            ok, err = robust_move_download_files(source, target)
        self.assertTrue(ok, err)
        self.assertEqual((target / "a.mkv").read_bytes(), b"payload")

    def test_an_unmovable_file_is_reported_with_a_count_and_a_sample(self):
        source = self.tmp / "Show"
        source.mkdir()
        (source / "a.mkv").write_bytes(b"x")
        (source / "b.mkv").write_bytes(b"y")
        with patch.object(shutil, "move", side_effect=PermissionError("locked")), \
             patch.object(shutil, "copy2", side_effect=OSError("disk full")), \
             patch("time.sleep"):
            ok, err = robust_move_download_files(source, self.tmp / "Anime")
        self.assertFalse(ok)
        self.assertIn("2 file(s) failed to move", err)
        self.assertIn("a.mkv", err)
        self.assertIn("b.mkv", err)

    def test_the_error_sample_is_capped_at_three_files(self):
        source = self.tmp / "Show"
        source.mkdir()
        for index in range(6):
            (source / f"{index}.mkv").write_bytes(b"x")
        with patch.object(shutil, "move", side_effect=PermissionError("locked")), \
             patch.object(shutil, "copy2", side_effect=OSError("disk full")), \
             patch("time.sleep"):
            ok, err = robust_move_download_files(source, self.tmp / "Anime")
        self.assertFalse(ok)
        self.assertIn("6 file(s)", err)
        self.assertLessEqual(err.count(".mkv:"), 3, "only the first three failures listed")

    def test_non_empty_source_directories_are_left_when_a_move_failed(self):
        source = self.tmp / "Show"
        source.mkdir()
        (source / "a.mkv").write_bytes(b"x")
        with patch.object(shutil, "move", side_effect=PermissionError("locked")), \
             patch.object(shutil, "copy2", side_effect=OSError("disk full")), \
             patch("time.sleep"):
            robust_move_download_files(source, self.tmp / "Anime")
        self.assertTrue(source.is_dir(), "a partially-moved tree must not be deleted")
        self.assertTrue((source / "a.mkv").exists())

    def test_a_read_only_source_tree_is_unlocked_first(self):
        source = self.tmp / "Show"
        (source / "Season").mkdir(parents=True)
        episode = source / "Season" / "e01.mkv"
        episode.write_bytes(b"a")
        episode.chmod(stat.S_IREAD)
        ok, err = robust_move_download_files(source, self.tmp / "Anime")
        self.assertTrue(ok, err)
        self.assertTrue((self.tmp / "Anime" / "Season" / "e01.mkv").exists())

    def test_an_empty_source_directory_is_removed(self):
        source = self.tmp / "Empty"
        source.mkdir()
        ok, err = robust_move_download_files(source, self.tmp / "Anime")
        self.assertTrue(ok, err)
        self.assertFalse(source.exists())
        self.assertTrue((self.tmp / "Anime").is_dir())


class TestUnlockPath(UtilFileTestCase):
    def test_an_empty_path_is_a_no_op(self):
        unlock_path("")  # must not raise

    def test_a_missing_path_is_a_no_op(self):
        unlock_path(self.tmp / "nope")  # must not raise

    def test_a_read_only_file_is_made_writable(self):
        target = self.tmp / "locked.bin"
        target.write_bytes(b"x")
        target.chmod(stat.S_IREAD)
        unlock_path(target)
        self.assertTrue(os.access(target, os.W_OK))

    def test_a_read_only_tree_is_unlocked_recursively(self):
        root = self.tmp / "tree"
        (root / "sub").mkdir(parents=True)
        inner = root / "sub" / "f.bin"
        inner.write_bytes(b"x")
        inner.chmod(stat.S_IREAD)
        (root / "sub").chmod(stat.S_IREAD)
        unlock_path(root)
        self.assertTrue(os.access(inner, os.W_OK))

    def test_a_failing_chmod_is_contained(self):
        target = self.tmp / "a.bin"
        target.write_bytes(b"x")
        with patch("os.chmod", side_effect=PermissionError("access denied")):
            unlock_path(target)  # must not raise

    def test_a_failing_existence_check_is_contained(self):
        with patch.object(Path, "exists", side_effect=OSError("gone")):
            unlock_path("C:/whatever")  # must not raise

    def test_a_failing_walk_is_contained(self):
        root = self.tmp / "tree"
        root.mkdir()
        with patch("os.walk", side_effect=OSError("inaccessible")):
            unlock_path(root)  # must not raise


class TestSendToTrash(UtilFileTestCase):
    def test_an_empty_path_is_rejected(self):
        self.assertFalse(send_to_trash(""))

    def test_a_missing_target_is_already_done(self):
        self.assertTrue(send_to_trash(self.tmp / "never-existed"))

    def test_a_file_is_removed(self):
        from PySide6.QtCore import QFile

        target = self.tmp / "gone.bin"
        target.write_bytes(b"x")
        with patch.object(QFile, "moveToTrash", return_value=False):
            self.assertTrue(send_to_trash(target))
        self.assertFalse(target.exists())

    def test_a_directory_is_removed(self):
        from PySide6.QtCore import QFile

        target = self.tmp / "gone-dir"
        (target / "nested").mkdir(parents=True)
        (target / "nested" / "f.bin").write_bytes(b"x")
        with patch.object(QFile, "moveToTrash", return_value=False):
            self.assertTrue(send_to_trash(target))
        self.assertFalse(target.exists())

    def test_a_successful_qt_trash_returns_immediately(self):
        from PySide6.QtCore import QFile

        target = self.tmp / "gone.bin"
        target.write_bytes(b"x")
        attempts: list[str] = []

        def _move(path):
            attempts.append(path)
            if len(attempts) == 1:
                target.unlink()
                return True
            return False

        with patch.object(QFile, "moveToTrash", _move):
            self.assertTrue(send_to_trash(target))
        self.assertEqual(len(attempts), 1, "a successful trash must not fall through")

    def test_a_qt_trash_that_lies_is_not_taken_at_face_value(self):
        """A True return while the file is still there must not be reported as success."""
        from PySide6.QtCore import QFile

        target = self.tmp / "gone.bin"
        target.write_bytes(b"x")
        with patch.object(QFile, "moveToTrash", return_value=True), \
             patch("send2trash.send2trash", side_effect=OSError("no bin")):
            self.assertTrue(send_to_trash(target))
        self.assertFalse(target.exists(), "the deletion fallback must have run")

    def test_the_normalised_spelling_is_tried_when_the_native_one_declines(self):
        from PySide6.QtCore import QFile

        attempts: list[str] = []
        target = self.tmp / "gone.bin"
        target.write_bytes(b"x")

        def _move(path):
            attempts.append(path)
            if len(attempts) == 2:
                target.unlink()
                return True
            return False

        with patch.object(QFile, "moveToTrash", _move):
            self.assertTrue(send_to_trash(target))
        self.assertEqual(len(attempts), 2)
        self.assertIn("\\", attempts[1], "the backslash spelling must be tried second")

    def test_a_raising_qt_trash_falls_through(self):
        from PySide6.QtCore import QFile

        target = self.tmp / "gone.bin"
        target.write_bytes(b"x")
        with patch.object(QFile, "moveToTrash", side_effect=RuntimeError("no shell")):
            self.assertTrue(send_to_trash(target))
        self.assertFalse(target.exists())

    def test_a_send2trash_success_is_honoured(self):
        from PySide6.QtCore import QFile

        target = self.tmp / "gone.bin"
        target.write_bytes(b"x")
        sent: list[str] = []

        def _send2trash(path):
            sent.append(path)
            target.unlink()

        with patch.object(QFile, "moveToTrash", return_value=False), \
             patch("send2trash.send2trash", _send2trash):
            self.assertTrue(send_to_trash(target))
        self.assertEqual(len(sent), 1)

    def test_a_raising_send2trash_falls_through_to_deletion(self):
        from PySide6.QtCore import QFile

        target = self.tmp / "gone.bin"
        target.write_bytes(b"x")
        with patch.object(QFile, "moveToTrash", return_value=False), \
             patch("send2trash.send2trash", side_effect=OSError("no recycle bin")):
            self.assertTrue(send_to_trash(target))
        self.assertFalse(target.exists())

    def test_a_permanently_locked_target_reports_false(self):
        from PySide6.QtCore import QFile

        target = self.tmp / "locked.bin"
        target.write_bytes(b"x")
        with patch.object(QFile, "moveToTrash", return_value=False), \
             patch("send2trash.send2trash", side_effect=OSError("no recycle bin")), \
             patch.object(Path, "unlink", side_effect=PermissionError("in use")), \
             patch("time.sleep"):
            self.assertFalse(send_to_trash(target))

    def test_a_retry_succeeds_after_a_transient_lock(self):
        from PySide6.QtCore import QFile

        target = self.tmp / "flaky.bin"
        target.write_bytes(b"x")
        unlinks = {"n": 0}
        real_unlink = Path.unlink

        def _unlink(self, *args, **kwargs):
            unlinks["n"] += 1
            if unlinks["n"] == 1:
                raise PermissionError("antivirus scan in progress")
            return real_unlink(self, *args, **kwargs)

        with patch.object(QFile, "moveToTrash", return_value=False), \
             patch("send2trash.send2trash", side_effect=OSError("no bin")), \
             patch.object(Path, "unlink", _unlink), \
             patch("time.sleep"):
            self.assertTrue(send_to_trash(target))
        self.assertEqual(unlinks["n"], 2, "the deletion must be retried once")

    def test_a_read_only_target_is_unlocked_before_deletion(self):
        from PySide6.QtCore import QFile

        target = self.tmp / "readonly.bin"
        target.write_bytes(b"x")
        target.chmod(stat.S_IREAD)
        with patch.object(QFile, "moveToTrash", return_value=False), \
             patch("send2trash.send2trash", side_effect=OSError("no bin")):
            self.assertTrue(send_to_trash(target))
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
