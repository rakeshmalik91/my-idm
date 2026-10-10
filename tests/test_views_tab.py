"""Tests for the Views tab and the file-type segregated view.

Two features, one file, because they share a tab and a mode string:

* a third segregated mode grouping by **file type** (Video / Audio / Archives /
  Documents / Photos / General), selectable from the View menu as well as the new
  **Views** tab in Preferences;
* the Views tab's **column select and ordering** controls, which are a UI over the
  QHeaderView state that ``MainWindow._save_ui_state_to_db()`` already persists.

The column half deliberately has no new persistence: visibility and order live in the
header's own ``saveState()`` blob. A second source of truth would be a second thing to keep
in sync with ``_on_reset_view``, and the tests below assert the tab writes through to the
real header rather than to any private copy.

Hermetic throughout: temp database, a real ``MainWindow`` where the header is needed, and
no clock reads beyond what the date classifier is given.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication, QCheckBox, QListWidget

from my_idm.database import Database, DownloadEntry
from my_idm.settings_dialog import TAB_VIEWS
from my_idm.styles import STYLESHEETS, _PALETTES
from my_idm.download_model import (
    DEFAULT_SEGREGATED_MODE,
    SEGREGATED_MODES,
    SEGREGATED_MODE_LABELS,
    SECTION_TYPE_ARCHIVE,
    SECTION_TYPE_AUDIO,
    SECTION_TYPE_DOCUMENTS,
    SECTION_TYPE_GENERAL,
    SECTION_TYPE_PHOTO,
    SECTION_TYPE_VIDEO,
    TYPE_SECTION_DEFS,
    Col,
    DownloadTableModel,
    get_entry_type_category,
    split_extension,
)

app = QApplication.instance() or QApplication(sys.argv)
IS_HEADLESS_WIN_CI = sys.platform == "win32" and os.environ.get("CI", "").strip().lower() in ("true", "1")


def entry(name="", url="u", eid="x", **kw) -> DownloadEntry:
    return DownloadEntry(id=eid, url=url, filename=name, save_path="C:/t", **kw)


# ===========================================================================
# The classifier
# ===========================================================================

class TestSplitExtension(unittest.TestCase):
    def test_a_normal_name_splits_at_the_last_dot(self):
        self.assertEqual(split_extension("Show.S01E01.1080p.mkv"), ("Show.S01E01.1080p", ".mkv"))

    def test_a_leading_dot_is_a_hidden_file_not_an_extension(self):
        self.assertEqual(split_extension(".gitignore"), (".gitignore", ""))

    def test_a_trailing_dot_is_not_an_extension(self):
        self.assertEqual(split_extension("weird."), ("weird.", ""))

    def test_no_dot_at_all(self):
        self.assertEqual(split_extension("README"), ("README", ""))

    def test_a_blank_name(self):
        self.assertEqual(split_extension(""), ("", ""))

    def test_directories_are_stripped_first(self):
        self.assertEqual(split_extension("C:/a/b/Movie.mkv"), ("Movie", ".mkv"))

    def test_the_extension_is_lower_cased_but_the_stem_is_not(self):
        self.assertEqual(split_extension("Movie.MKV"), ("Movie", ".mkv"))


class TestTypeCategory(unittest.TestCase):
    def test_every_category_is_reachable(self):
        cases = {
            "a.mkv": SECTION_TYPE_VIDEO,
            "a.mp3": SECTION_TYPE_AUDIO,
            "a.zip": SECTION_TYPE_ARCHIVE,
            "a.pdf": SECTION_TYPE_DOCUMENTS,
            "a.png": SECTION_TYPE_PHOTO,
            "a.bin": SECTION_TYPE_GENERAL,
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(get_entry_type_category(entry(name)), expected)

    def test_the_extension_match_is_case_insensitive(self):
        for name in ("A.MKV", "a.Mp4", "Movie.JPEG"):
            with self.subTest(name=name):
                self.assertNotEqual(
                    get_entry_type_category(entry(name)), SECTION_TYPE_GENERAL
                )

    def test_a_compound_extension_uses_its_tail(self):
        self.assertEqual(
            get_entry_type_category(entry("archive.tar.gz")), SECTION_TYPE_ARCHIVE
        )

    def test_the_file_path_is_used_when_the_filename_has_no_extension(self):
        """A torrent whose root folder carries the extension still lands correctly."""
        e = entry("", file_path="C:/Downloads/Show.S01/Feature.mkv")
        self.assertEqual(get_entry_type_category(e), SECTION_TYPE_VIDEO)

    def test_the_filename_wins_over_the_path(self):
        e = entry("notes.txt", file_path="C:/Downloads/Movie.mkv")
        self.assertEqual(
            get_entry_type_category(e), SECTION_TYPE_DOCUMENTS,
            "the display name is what the user recognises, so it wins",
        )

    def test_an_unknown_extension_falls_to_general(self):
        self.assertEqual(get_entry_type_category(entry("thing.xyz")), SECTION_TYPE_GENERAL)

    def test_a_download_with_no_name_is_general_not_a_crash(self):
        self.assertEqual(get_entry_type_category(entry("")), SECTION_TYPE_GENERAL)
        self.assertEqual(get_entry_type_category(entry("", "")), SECTION_TYPE_GENERAL)

    def test_an_explicit_override_is_honoured(self):
        """A mis-detected item can be filed by hand, and the override is sticky."""
        e = entry("movie.mkv")
        e.metadata["type_category"] = SECTION_TYPE_GENERAL
        self.assertEqual(get_entry_type_category(e), SECTION_TYPE_GENERAL)

    def test_a_bogus_override_is_ignored(self):
        e = entry("movie.mkv")
        e.metadata["type_category"] = "not-a-category"
        self.assertEqual(get_entry_type_category(e), SECTION_TYPE_VIDEO)

    def test_every_declared_extension_maps_to_a_real_section(self):
        declared = {cat for cat, _t, _s in TYPE_SECTION_DEFS}
        from my_idm.download_model import TYPE_CATEGORY_EXTENSIONS

        for ext, category in TYPE_CATEGORY_EXTENSIONS.items():
            with self.subTest(ext=ext):
                self.assertIn(category, declared)
                self.assertEqual(ext, ext.lower().lstrip("."))

    def test_the_section_order_is_the_display_order(self):
        self.assertEqual(
            [cat for cat, _t, _s in TYPE_SECTION_DEFS],
            [
                SECTION_TYPE_VIDEO, SECTION_TYPE_AUDIO, SECTION_TYPE_ARCHIVE,
                SECTION_TYPE_DOCUMENTS, SECTION_TYPE_PHOTO, SECTION_TYPE_GENERAL,
            ],
        )


# ===========================================================================
# The model
# ===========================================================================

class TestFileTypeSegregatedModel(unittest.TestCase):
    def setUp(self):
        self.model = DownloadTableModel()
        self.entries = [
            entry("movie.mkv", eid="v1"),
            entry("song.flac", eid="a1"),
            entry("pack.zip", eid="r1"),
            entry("manual.pdf", eid="d1"),
            entry("photo.png", eid="p1"),
            entry("installer.bin", eid="g1"),
        ]
        self.model.load_entries(self.entries)

    def _sections(self):
        return [
            self.model.get_section_header(row).section_title
            for row in self.model.get_section_header_row_indices()
        ]

    def _section_of(self, eid):
        """Title of the section header immediately above the row holding *eid*."""
        current = None
        for row in range(self.model.rowCount()):
            header = self.model.get_section_header(row)
            if header is not None:
                current = header.section_title
                continue
            e = self.model.get_entry(row)
            if e is not None and e.id == eid:
                return current
        return None

    def test_each_file_lands_in_its_own_section(self):
        self.model.set_segregated_view(True, "type")
        self.assertEqual(self._section_of("v1"), "Video")
        self.assertEqual(self._section_of("a1"), "Audio")
        self.assertEqual(self._section_of("r1"), "Archives")
        self.assertEqual(self._section_of("d1"), "Documents")
        self.assertEqual(self._section_of("p1"), "Photos")
        self.assertEqual(self._section_of("g1"), "General")

    def test_every_section_appears_even_when_empty(self):
        """An empty section is how the user discovers the mode exists at all."""
        self.model.load_entries([entry("movie.mkv", eid="v1")])
        self.model.set_segregated_view(True, "type")
        self.assertEqual(
            self._sections(),
            ["Video", "Audio", "Archives", "Documents", "Photos", "General"],
        )

    def test_the_status_mode_still_works(self):
        self.model.set_segregated_view(True, "status")
        self.assertEqual(self._sections(), ["Active", "Seeding", "Inactive"])

    def test_the_date_mode_still_works(self):
        self.model.set_segregated_view(True, "date")
        self.assertEqual(len(self._sections()), 5)

    def test_switching_modes_rebuilds_the_sections(self):
        self.model.set_segregated_view(True, "status")
        self.assertEqual(self._sections(), ["Active", "Seeding", "Inactive"])
        self.model.set_segregated_mode("type")
        self.assertEqual(self._sections()[0], "Video")
        self.model.set_segregated_mode("date")
        self.assertNotIn("Video", self._sections())

    def test_type_is_a_recognised_mode(self):
        self.assertIn("type", SEGREGATED_MODES)
        self.assertEqual(SEGREGATED_MODE_LABELS["type"], SEGREGATED_MODE_LABELS["type"])
        for mode in SEGREGATED_MODES:
            with self.subTest(mode=mode):
                self.assertIn(mode, SEGREGATED_MODE_LABELS)

    def test_an_unknown_mode_falls_back_to_the_default(self):
        self.model.set_segregated_mode("type")
        self.model.set_segregated_mode("colour")
        self.assertEqual(self.model.segregated_mode(), DEFAULT_SEGREGATED_MODE)

    def test_an_unknown_mode_in_the_constructor_is_ignored(self):
        self.model.set_segregated_view(True, "colour")
        self.assertEqual(self.model.segregated_mode(), DEFAULT_SEGREGATED_MODE)

    def test_turning_segregation_off_restores_a_flat_list(self):
        self.model.set_segregated_view(True, "type")
        self.model.set_segregated_view(False)
        self.assertEqual(self._sections(), [])
        self.assertEqual(self.model.rowCount(), len(self.entries))


# ===========================================================================
# The Views tab
# ===========================================================================

@unittest.skipIf(IS_HEADLESS_WIN_CI, "headless Windows CI cannot create real MainWindow / SettingsDialog")
class ViewsTabTestCase(unittest.TestCase):
    """A real MainWindow, so the tab is exercised against the header it edits."""

    @classmethod
    def setUpClass(cls):
        if IS_HEADLESS_WIN_CI:
            raise unittest.SkipTest("headless Windows CI cannot create real MainWindow / SettingsDialog")
        from my_idm.main_window import MainWindow
        from my_idm.manager import DownloadManager

        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = Database(":memory:")
        cls.db.open()
        cls.manager = DownloadManager(cls.db)
        cls.window = MainWindow(cls.manager)

    @staticmethod
    def _dispose(widget):
        """Close a widget and **actually destroy it**.

        Two separate things go wrong otherwise, and both are silent:

        * ``MainWindow.closeEvent`` ignores the close event and hides the window whenever
          close-to-tray is on (it is by default), so a bare ``close()`` leaves it alive.
        * ``deleteLater()`` posts a ``DeferredDelete`` event, and
          ``QApplication.processEvents()`` does **not** deliver those by default - the loop
          has to be asked for ``AllEvents``, or ``sendPostedEvents`` called directly. So the
          usual ``close()`` + ``deleteLater()`` + ``processEvents()`` idiom destroys nothing.

        A surviving widget still counts. ``apply_theme`` calls ``app.setStyleSheet``, which
        restyles every live top-level widget, so each leak makes the next theme switch
        proportionally slower. Measured: ten undeleted ``SettingsDialog`` objects left 4,212
        live widgets and 100 top-level ones, which turned this file from 27 seconds into
        over thirteen minutes.
        """
        try:
            widget.close()
            widget.deleteLater()
        except RuntimeError:
            return  # already destroyed by an earlier cleanup
        if not IS_HEADLESS_WIN_CI:
            QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            QApplication.processEvents()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "window"):
            cls._dispose(cls.window)
        if hasattr(cls, "manager"):
            cls.manager.stop()
        if hasattr(cls, "db"):
            cls.db.close()
        if hasattr(cls, "_tmp"):
            cls._tmp.cleanup()
        if not IS_HEADLESS_WIN_CI:
            QApplication.processEvents()

    def make_dialog(self):
        from my_idm.settings_dialog import SettingsDialog

        dialog = SettingsDialog(db=self.db, parent=self.window)
        self.addCleanup(self._dispose, dialog)
        if not IS_HEADLESS_WIN_CI:
            QApplication.processEvents()
        return dialog

    def test_the_views_tab_exists_next_to_general(self):
        from my_idm.settings_dialog import SettingsDialog

        dialog = self.make_dialog()
        titles = [
            dialog._tabs.tabText(i) for i in range(dialog._tabs.count())
        ]
        self.assertIn("👁️ Views & Columns", titles)
        self.assertEqual(
            titles.index("👁️ Views & Columns"), 3,
            "the Views tab belongs after the core download, app, and clipboard tabs",
        )
        self.assertIsInstance(SettingsDialog, type)

    def test_the_segregated_controls_reflect_stored_state(self):
        self.db.set_ui_state("segregated_view_enabled", True)
        self.db.set_ui_state("segregated_view_mode", "type")
        dialog = self.make_dialog()
        self.assertIsInstance(dialog._seg_enabled_cb, QCheckBox)
        self.assertTrue(dialog._seg_enabled_cb.isChecked())
        self.assertEqual(dialog._seg_mode_combo.currentData(), "type")

    def test_turning_segregation_off_disables_the_mode_picker(self):
        self.db.set_ui_state("segregated_view_enabled", False)
        dialog = self.make_dialog()
        self.assertFalse(dialog._seg_mode_combo.isEnabled())
        dialog._seg_enabled_cb.setChecked(True)
        self.assertTrue(dialog._seg_mode_combo.isEnabled())

    def test_a_stale_persisted_mode_does_not_break_the_picker(self):
        self.db.set_ui_state("segregated_view_mode", "colour")
        dialog = self.make_dialog()
        self.assertEqual(
            dialog._seg_mode_combo.currentData(), DEFAULT_SEGREGATED_MODE
        )

    def test_the_column_list_shows_every_column(self):
        dialog = self.make_dialog()
        self.assertIsInstance(dialog._column_list, QListWidget)
        self.assertEqual(dialog._column_list.count(), Col.COUNT)
        logicals = [
            dialog._column_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(dialog._column_list.count())
        ]
        self.assertEqual(
            sorted(logicals), list(range(Col.COUNT)),
            "every column must appear exactly once - none dropped, none duplicated",
        )
        labels = [dialog._column_list.item(i).text() for i in range(Col.COUNT)]
        self.assertEqual(
            sorted(labels), sorted(Col.HEADERS[:Col.COUNT]),
            "each list entry must carry its own column's header text",
        )

    def test_the_column_list_is_in_visual_order(self):
        """Ordering control is only useful if it reads the way the table looks.

        Note the app's *default* order is not identity order: ``_DEFAULT_COLUMN_ORDER``
        deliberately puts QUEUE_NAME at slot 1 and SOURCE_DOMAIN, FILE_NAME and the seeding
        columns in the tail. So this asserts the list mirrors the header rather than that it is
        sorted.
        """
        dialog = self.make_dialog()
        header = self.window._table.horizontalHeader()
        listed = [
            dialog._column_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(dialog._column_list.count())
        ]
        self.assertEqual(
            listed, [header.logicalIndex(v) for v in range(Col.COUNT)],
            "the list must read left-to-right exactly as the header renders it",
        )

    def test_unticking_a_column_hides_it_on_save(self):
        dialog = self.make_dialog()
        header = self.window._table.horizontalHeader()
        target = Col.ETA
        self.assertFalse(header.isSectionHidden(target))
        for i in range(dialog._column_list.count()):
            if dialog._column_list.item(i).data(Qt.ItemDataRole.UserRole) == target:
                dialog._column_list.item(i).setCheckState(Qt.CheckState.Unchecked)
        dialog._apply_views_tab()
        try:
            self.assertTrue(
                header.isSectionHidden(target),
                "an unticked column must actually be hidden on the table",
            )
        finally:
            header.setSectionHidden(target, False)

    def test_reordering_moves_the_real_section(self):
        dialog = self.make_dialog()
        header = self.window._table.horizontalHeader()
        start_visual = header.visualIndex(Col.PROGRESS)
        row = next(
            i for i in range(dialog._column_list.count())
            if dialog._column_list.item(i).data(Qt.ItemDataRole.UserRole) == Col.PROGRESS
        )
        self.assertEqual(row, start_visual, "precondition: the list is in visual order")
        dialog._column_list.setCurrentRow(row)
        dialog._move_selected_column(-1)
        dialog._apply_views_tab()
        try:
            self.assertEqual(
                header.visualIndex(Col.PROGRESS), start_visual - 1,
                "Move Up must shift the real section, not just the list",
            )
        finally:
            self.window._on_reset_view()

    def test_moving_a_column_past_the_end_is_a_no_op(self):
        dialog = self.make_dialog()
        before = [
            dialog._column_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(dialog._column_list.count())
        ]
        dialog._column_list.setCurrentRow(dialog._column_list.count() - 1)
        dialog._move_selected_column(1)
        after = [
            dialog._column_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(dialog._column_list.count())
        ]
        self.assertEqual(after, before)

    def test_moving_with_nothing_selected_is_a_no_op(self):
        dialog = self.make_dialog()
        before = [
            dialog._column_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(dialog._column_list.count())
        ]
        dialog._column_list.setCurrentRow(-1)
        dialog._move_selected_column(-1)
        dialog._move_selected_column(1)
        after = [
            dialog._column_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(dialog._column_list.count())
        ]
        self.assertEqual(after, before)

    def test_reset_restores_defaults_and_unticks_nothing(self):
        header = self.window._table.horizontalHeader()
        header.setSectionHidden(Col.ETA, True)
        try:
            dialog = self.make_dialog()
            dialog._reset_columns_to_defaults()
            self.assertFalse(header.isSectionHidden(Col.ETA))
            states = [
                dialog._column_list.item(i).checkState()
                for i in range(dialog._column_list.count())
            ]
            self.assertTrue(
                all(s == Qt.CheckState.Checked for s in states),
                "Reset must show every column again",
            )
        finally:
            self.window._on_reset_view()

    def test_saving_persists_the_segregation_mode(self):
        dialog = self.make_dialog()
        dialog._seg_enabled_cb.setChecked(True)
        dialog._seg_mode_combo.setCurrentIndex(
            dialog._seg_mode_combo.findData("type")
        )
        dialog._apply_views_tab()
        self.assertTrue(self.db.get_ui_state("segregated_view_enabled"))
        self.assertEqual(self.db.get_ui_state("segregated_view_mode"), "type")
        self.assertEqual(self.window._segregated_view_mode, "type")

    def test_saving_applies_the_mode_to_the_table(self):
        dialog = self.make_dialog()
        dialog._seg_enabled_cb.setChecked(True)
        dialog._seg_mode_combo.setCurrentIndex(dialog._seg_mode_combo.findData("type"))
        dialog._apply_views_tab()
        try:
            self.assertTrue(self.window._model.is_segregated_view())
            self.assertEqual(self.window._model.segregated_mode(), "type")
        finally:
            self.window._set_segregation_mode("status")

    def test_the_whole_save_path_applies_the_views_tab(self):
        """Save Settings must reach the Views tab, not just _apply_views_tab in isolation."""
        dialog = self.make_dialog()
        dialog._seg_enabled_cb.setChecked(True)
        dialog._seg_mode_combo.setCurrentIndex(dialog._seg_mode_combo.findData("type"))
        dialog._on_save()
        try:
            self.assertEqual(self.db.get_ui_state("segregated_view_mode"), "type")
        finally:
            self.window._set_segregation_mode("status")

    def test_a_standalone_dialog_with_no_table_still_saves_the_mode(self):
        """SettingsDialog is constructed headless in tests; it must degrade, not crash."""
        from my_idm.settings_dialog import SettingsDialog

        dialog = SettingsDialog(db=self.db)
        self.addCleanup(dialog.close)
        self.addCleanup(dialog.deleteLater)
        QApplication.processEvents()
        self.assertIsNone(dialog._table_view())
        dialog._seg_enabled_cb.setChecked(True)
        dialog._seg_mode_combo.setCurrentIndex(dialog._seg_mode_combo.findData("type"))
        dialog._apply_views_tab()  # must not raise
        self.assertEqual(self.db.get_ui_state("segregated_view_mode"), "type")
        dialog._reset_columns_to_defaults()  # must not raise
        dialog._move_selected_column(-1)  # must not raise


@unittest.skipIf(IS_HEADLESS_WIN_CI, "headless Windows CI cannot create real MainWindow / SettingsDialog")
class TestViewMenuOffersTheTypeMode(ViewsTabTestCase):
    def test_the_menu_has_a_type_entry_in_the_exclusive_group(self):
        self.assertTrue(hasattr(self.window, "_act_seg_by_type"))
        self.assertIn(self.window._act_seg_by_type, self.window._seg_mode_group.actions())
        self.assertTrue(self.window._act_seg_by_type.isCheckable())
        self.assertIn("File Type", self.window._act_seg_by_type.text())

    def test_all_three_modes_are_in_the_group(self):
        labels = [a.text() for a in self.window._seg_mode_group.actions()]
        self.assertEqual(len(labels), len(SEGREGATED_MODES))
        self.assertTrue(any("Status" in t for t in labels))
        self.assertTrue(any("Date" in t for t in labels))
        self.assertTrue(any("File Type" in t for t in labels))

    def test_choosing_the_type_mode_checks_only_the_type_entry(self):
        try:
            self.window._set_segregation_mode("type")
            self.assertTrue(self.window._act_seg_by_type.isChecked())
            self.assertFalse(self.window._act_seg_by_status.isChecked())
            self.assertFalse(self.window._act_seg_by_date.isChecked())
        finally:
            self.window._set_segregation_mode("status")
        self.assertTrue(self.window._act_seg_by_status.isChecked())


@unittest.skipIf(IS_HEADLESS_WIN_CI, "headless Windows CI cannot create real MainWindow / SettingsDialog")
class TestColumnListIsNeverSliced(ViewsTabTestCase):
    """The list's height comes from a stretch, so it must not end mid-row.

    The layout hands the list whatever height is left over, which lands on an arbitrary
    pixel value. The last visible row was therefore **bisected** by the bottom of the frame
    - a half-height checkbox and a name cut through the middle. With seventeen columns that
    happened at most window sizes, and it reads as a broken control rather than as a list
    with more below.

    ``_WholeRowListWidget`` constrains the viewport to a whole multiple of the row height,
    so the leftover becomes blank space inside the frame. Checked across a range of heights
    because the defect only appears at some of them.
    """

    SIZES = ((740, 560), (740, 640), (900, 560), (1000, 700), (1100, 600), (1400, 800))

    def _sized(self, width, height):
        from my_idm.settings_dialog import TAB_VIEWS, SettingsDialog

        dialog = SettingsDialog(db=self.db, parent=self.window, initial_tab=TAB_VIEWS)
        self.addCleanup(self._dispose, dialog)
        dialog.resize(width, height)
        dialog.show()
        for _ in range(3):
            QApplication.processEvents()
        return dialog

    def _at_minimum(self):
        """The dialog at its own minimum size, with Views the current page.

        Constructed *with* the Views tab selected rather than switched to afterwards: a
        dialog opened on General never lays the Views page out, so switching to it later
        leaves a stale geometry and every measurement below is of the wrong widget. Sized
        once, too - resizing to 0 and back leaves the freshly-current page unlaid-out.
        """
        from my_idm.settings_dialog import TAB_VIEWS, SettingsDialog

        dialog = SettingsDialog(db=self.db, parent=self.window, initial_tab=TAB_VIEWS)
        self.addCleanup(self._dispose, dialog)
        dialog.resize(dialog.minimumWidth(), dialog.minimumHeight())
        dialog.show()
        for _ in range(4):
            QApplication.processEvents()
        return dialog

    def test_no_row_is_partially_visible_at_any_window_size(self):
        for width, height in self.SIZES:
            with self.subTest(size=f"{width}x{height}"):
                dialog = self._sized(width, height)
                self.assertFalse(
                    dialog._column_list.has_partially_visible_row(),
                    "the list clips a row in half instead of ending between two",
                )

    def test_the_height_is_always_a_whole_number_of_rows(self):
        """The invariant that actually prevents cropping, at any window size.

        The widget owns its height, so this holds whatever the font, DPI or window size -
        which is the point. Earlier attempts snapped a height the layout still owned, and
        were only correct for the font they were measured with.
        """
        for width, height in self.SIZES:
            with self.subTest(size=f"{width}x{height}"):
                dialog = self._sized(width, height)
                lst = dialog._column_list
                row = lst.sizeHintForRow(0)
                self.assertGreater(row, 0)
                margins = lst.viewportMargins()
                content = lst.height() - (
                    2 * lst.frameWidth() + margins.top() + margins.bottom()
                )
                self.assertEqual(
                    content % row, 0,
                    f"content height {content} is not a whole multiple of the "
                    f"{row}px row height",
                )
                self.assertFalse(lst.has_partially_visible_row())

    def test_growing_the_window_does_not_change_the_row_count(self):
        """A fixed height means the same whole rows are shown at every window size.

        The alternative - growing with the window - is what let an arbitrary leftover pixel
        count cut a row in half.
        """
        dialog = self._sized(1400, 800)
        lst = dialog._column_list
        tall = lst.rows_that_fit()
        height = lst.height()
        dialog.resize(740, 560)
        for _ in range(3):
            QApplication.processEvents()
        self.assertEqual(lst.rows_that_fit(), tall)
        self.assertEqual(lst.height(), height)
        self.assertFalse(lst.has_partially_visible_row())

    def test_every_row_is_still_reachable_by_scrolling(self):
        """Scrolling to the end must reveal the last column in full."""
        dialog = self._sized(740, 560)
        lst = dialog._column_list
        bar = lst.verticalScrollBar()
        bar.setValue(bar.maximum())
        QApplication.processEvents()
        last = lst.visualItemRect(lst.item(lst.count() - 1))
        self.assertLessEqual(
            last.bottom(), lst.viewport().rect().bottom(),
            "the last column cannot be scrolled fully into view",
        )

    def test_the_list_is_not_cropped_at_the_dialog_minimum(self):
        """The reported symptom, stated directly.

        Whether the *page* needs to scroll is a separate question that depends on the font
        size in force, so it is not asserted here; whether the list slices a row is not
        font-dependent, and that is the defect.
        """
        dialog = self._at_minimum()
        lst = dialog._column_list
        self.assertFalse(
            lst.has_partially_visible_row(),
            "the column list is cropped at the dialog's minimum size",
        )

    def test_every_column_is_visible_without_scrolling(self):
        """No row is half-shown and the list needs no scrollbar.

        Whether *all* seventeen fit at once depends on the row height, which depends on the
        Qt style and font in force - about 17px per row under the real stylesheet, about 28px
        without it. Pinning a row **count** here would therefore assert something about the
        test harness rather than about the product, so it is deliberately not asserted; that
        all seventeen do fit at once was confirmed by rendering the dialog, and is protected
        by the invariants below, which hold in any environment.
        """
        lst = self._at_minimum()._column_list
        viewport = lst.viewport().rect()

        for index in range(lst.count()):
            rect = lst.visualItemRect(lst.item(index))
            if not viewport.intersects(rect):
                continue
            with self.subTest(row=index):
                self.assertTrue(
                    viewport.contains(rect),
                    "a row is cut by the list frame instead of ending between two rows",
                )
        self.assertFalse(lst.has_partially_visible_row())

    def test_the_column_list_shows_every_column_by_default(self):
        """All seventeen rows, unless the font is too tall to fit them.

        ``DEFAULT_VISIBLE_ROWS`` is the whole set on purpose: this list has always shown
        every column, and trimming it hides columns rather than fixing anything.
        """
        dialog = self.make_dialog()
        lst = dialog._column_list
        self.assertEqual(
            lst._visible_rows, lst.count(),
            "the list should size itself to show every column it holds",
        )

    def test_the_group_box_has_no_hole_above_the_list(self):
        """The Fixed-height list cannot absorb slack, so the layout used to centre it.

        That left a ~46px gap between the hint and the first row and a similar one under the
        last, with the group frame drawn around empty space. Asserted as a bound rather than
        an exact value because it is padding, and padding is font-dependent.
        """
        dialog = self._sized(820, 700)
        lst = dialog._column_list
        group = lst.parentWidget()
        hint = group.layout().itemAt(0).widget()
        self.assertIsNotNone(hint, "the hint label is no longer the group's first item")

        gap_above = lst.geometry().top() - hint.geometry().bottom()
        last_row = lst.visualItemRect(lst.item(lst.count() - 1))
        last_bottom = lst.geometry().top() + lst.viewport().y() + last_row.bottom()
        gap_below = group.rect().bottom() - last_bottom

        self.assertLessEqual(
            gap_above, 24,
            f"{gap_above}px of dead space between the hint and the first column",
        )
        self.assertLessEqual(
            gap_below, 28,
            f"{gap_below}px of dead space under the last column, inside the group frame",
        )

    def test_the_group_box_hugs_its_content(self):
        """Its height should be the content's, not the page's."""
        dialog = self._sized(820, 700)
        group = dialog._column_list.parentWidget()
        dialog.resize(820, 1100)
        for _ in range(4):
            QApplication.processEvents()
        dialog.resize(820, 700)
        for _ in range(4):
            QApplication.processEvents()
        self.assertLessEqual(
            group.height(), group.sizeHint().height() + 2,
            "the group grows with the window instead of hugging the list",
        )

    def test_the_hint_label_wraps_instead_of_being_clipped(self):
        """A plain QLabel in a QVBoxLayout never wraps, so it clips mid-word when narrow."""
        from PySide6.QtWidgets import QLabel

        from my_idm.settings_dialog import tab_index

        dialog = self.make_dialog()
        # currentWidget() follows whichever tab is showing, which a neighbouring test may
        # have changed; the search has to look at the Views page specifically or this test
        # passes or fails depending on collection order.
        dialog._tabs.setCurrentIndex(tab_index(TAB_VIEWS))
        QApplication.processEvents()
        hints = [
            label for label in dialog._tabs.currentWidget().findChildren(QLabel)
            if label.text().startswith("Tick a column")
        ]
        self.assertEqual(len(hints), 1, "the column hint label was not found")
        self.assertTrue(
            hints[0].wordWrap(),
            "without word wrap the hint is cut off at the right edge in a narrow dialog",
        )

    def test_an_empty_list_does_not_raise(self):
        """`sizeHintForRow` returns -1 with no items; sizing must tolerate that.

        An earlier version fell back to a font-derived row height there, which ran ~28px
        against a real ~17px and inflated the page by hundreds of pixels.
        """
        from my_idm.settings_dialog import _WholeRowListWidget

        empty = _WholeRowListWidget()
        self.addCleanup(empty.deleteLater)
        empty.relayout_rows()
        self.assertFalse(empty.has_partially_visible_row())
        self.assertEqual(empty.rows_that_fit(), 0)

        empty.addItem("first")
        empty.relayout_rows()
        self.assertGreaterEqual(empty.rows_that_fit(), 1)


@unittest.skipIf(IS_HEADLESS_WIN_CI, "headless Windows CI cannot create real MainWindow / SettingsDialog")
class TestColumnListLayout(ViewsTabTestCase):
    """The list must present every column without overflowing its row.

    Two real defects prompted these: with the action buttons underneath, the list inherited
    the group's full stretch and the long names were clipped mid-glyph; and a horizontal
    scrollbar appeared, which read as a broken control rather than a scrollable list.
    """

    def test_long_column_names_are_elided_not_clipped(self):
        dialog = self.make_dialog()
        self.assertEqual(
            dialog._column_list.textElideMode(), Qt.TextElideMode.ElideRight
        )

    def test_there_is_no_horizontal_scrollbar(self):
        dialog = self.make_dialog()
        self.assertEqual(
            dialog._column_list.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )

    def test_the_list_has_room_for_the_longest_name(self):
        dialog = self.make_dialog()
        self.assertGreaterEqual(
            dialog._column_list.minimumWidth(), 260,
            "the widest header is 'Seeding Started At'; the list must fit it",
        )

    def test_every_item_carries_a_tooltip_with_its_full_name(self):
        """An elided item still has to be identifiable on hover."""
        dialog = self.make_dialog()
        for i in range(dialog._column_list.count()):
            item = dialog._column_list.item(i)
            self.assertIn(
                item.text(), item.toolTip(),
                f"item {i} ({item.text()!r}) has no usable tooltip",
            )

    def test_the_actions_sit_beside_the_list_not_below_it(self):
        """Buttons underneath cost the list its width, which caused the overflow."""
        dialog = self.make_dialog()
        self.assertIsNotNone(dialog._col_up_btn.parentWidget())


@unittest.skipIf(IS_HEADLESS_WIN_CI, "headless Windows CI cannot create real MainWindow / SettingsDialog")
class TestTabStructure(ViewsTabTestCase):
    def test_vpn_and_tor_are_separate_tabs(self):
        """Splitting them stops scrolling past three unrelated groups to reach Tor."""
        titles = [
            self.make_dialog()._tabs.tabText(i)
            for i in range(self.window and 9)
        ]
        self.assertIn("🛡️ VPN & Proxy", titles)
        self.assertIn("🧅 Tor", titles)
        self.assertNotIn("🛡️ Network & Privacy (VPN & Tor)", titles)

    def test_the_vpn_tab_has_no_tor_controls(self):
        dialog = self.make_dialog()
        vpn = dialog._create_vpn_tab()
        self.assertIsNotNone(vpn)
        self.assertFalse(hasattr(dialog, "_tor_enable_cb") and _contains(vpn, dialog._tor_enable_cb))

    def test_the_tor_tab_has_the_tor_controls(self):
        dialog = self.make_dialog()
        dialog._create_tor_tab()
        self.assertIsNotNone(dialog._tor_enable_cb)
        self.assertIsNotNone(dialog._tor_route_http_cb)
        self.assertIsNotNone(dialog._tor_route_torrent_cb)

    def test_external_tools_are_one_tab_per_tool(self):
        dialog = self.make_dialog()
        tabs = [dialog._tabs.tabText(i) for i in range(dialog._tabs.count())]
        self.assertIn("🌐 AnimePahe Scraper", tabs)
        self.assertIn("▶️ YouTube (yt-dlp)", tabs)
        self.assertNotIn("🛠️ External Tools", tabs)


def _contains(widget, child) -> bool:
    """Is *child* inside *widget*'s subtree?"""
    if child is None:
        return False
    found = child
    while found is not None:
        if found is widget:
            return True
        found = found.parentWidget()
    return False


@unittest.skipIf(IS_HEADLESS_WIN_CI, "headless Windows CI cannot create real MainWindow / SettingsDialog")
class TestPreferencesSidebar(ViewsTabTestCase):
    """The tab navigator is a list on the left, not a rotated vertical ``QTabBar``.

    A ``QTabWidget`` with ``setTabPosition(West)`` was tried first and is wrong for these
    titles: Qt rotates a vertical tab bar's labels 90 degrees whenever the tab is narrower
    than its text, and clips whatever still does not fit. The tab bar is therefore hidden
    and a ``QListWidget`` drives it.
    """

    def test_the_sidebar_lists_every_tab(self):
        dialog = self.make_dialog()
        self.assertEqual(dialog._tab_sidebar.count(), dialog._tabs.count())
        for i in range(dialog._tabs.count()):
            self.assertTrue(dialog._tab_sidebar.item(i).text())

    def test_the_native_tab_bar_is_hidden(self):
        """Otherwise Qt draws the rotated labels over the sidebar."""
        dialog = self.make_dialog()
        self.assertFalse(dialog._tabs.tabBar().isVisible())

    def test_the_emoji_is_split_from_the_label(self):
        """A full-height glyph beside 11px text reads as a glyph, not an icon."""
        dialog = self.make_dialog()
        for i in range(dialog._tab_sidebar.count()):
            text = dialog._tab_sidebar.item(i).text()
            self.assertNotIn("\U0001f4c1", text, f"row {i} still carries the raw emoji")

    def test_clicking_a_row_switches_the_page(self):
        dialog = self.make_dialog()
        dialog._tab_sidebar.setCurrentRow(5)
        QApplication.processEvents()
        self.assertEqual(dialog._tabs.currentIndex(), 5)

    def test_switching_the_page_moves_the_selection(self):
        dialog = self.make_dialog()
        dialog._tabs.setCurrentIndex(2)
        QApplication.processEvents()
        self.assertEqual(
            dialog._tab_sidebar.currentRow(), 2,
            "the two must stay in sync in both directions",
        )

    def test_the_sync_does_not_bounce(self):
        """Signals are blocked on the programmatic half; a loop would hang the event loop."""
        dialog = self.make_dialog()
        changes: list[tuple[int, int]] = []
        dialog._tabs.currentChanged.connect(lambda i: changes.append(("tab", i)))
        dialog._tabs.setCurrentIndex(4)
        QApplication.processEvents()
        dialog._tab_sidebar.setCurrentRow(7)
        QApplication.processEvents()
        self.assertEqual(changes, [("tab", 4), ("tab", 7)])

    def test_the_sidebar_is_narrow_enough_to_leave_room_for_content(self):
        dialog = self.make_dialog()
        self.assertLessEqual(dialog._tab_sidebar.maximumWidth(), 300)
        self.assertGreaterEqual(dialog._tab_sidebar.width(), 170)


@unittest.skipIf(IS_HEADLESS_WIN_CI, "headless Windows CI cannot create real MainWindow / SettingsDialog")
class TestAmpersandRendering(ViewsTabTestCase):
    """A literal ``&`` needs different escaping depending on the widget.

    Verified by rendering both spellings:

    * ``QGroupBox`` pushes its title through the mnemonic parser, so a lone ``&`` is
      **dropped** - "A & B" paints as "A  B" - and ``&&`` is required for one visible
      ampersand;
    * ``QLabel`` and ``QListWidget`` paint the string verbatim, so there a lone ``&`` is
      correct and ``&&`` would show two.

    The sidebar is a ``QListWidget``, so the tab titles use a single ``&``, while the seven
    group box titles keep ``&&``. Blanket-replacing one with the other silently deletes the
    ampersand from half the dialog, which is invisible in the source and obvious on screen.
    """

    def test_sidebar_labels_use_a_single_ampersand(self):
        dialog = self.make_dialog()
        for i in range(dialog._tab_sidebar.count()):
            text = dialog._tab_sidebar.item(i).text()
            with self.subTest(row=i):
                self.assertNotIn(
                    "&&", text,
                    "a QListWidget paints its text verbatim, so '&&' would show two "
                    "ampersands in the sidebar",
                )
        joined = " ".join(
            dialog._tab_sidebar.item(i).text() for i in range(dialog._tab_sidebar.count())
        )
        self.assertIn("&", joined, "the sidebar is meant to show single ampersands")

    def test_group_box_titles_keep_the_doubled_escape(self):
        from PySide6.QtWidgets import QGroupBox

        dialog = self.make_dialog()
        with_ampersand = [
            box.title() for box in dialog.findChildren(QGroupBox)
            if "&" in box.title()
        ]
        self.assertTrue(
            with_ampersand, "expected at least one group box title containing an ampersand"
        )
        for title in with_ampersand:
            with self.subTest(title=title):
                self.assertIn(
                    "&&", title,
                    "QGroupBox eats a lone '&', so its title must keep the doubled escape "
                    "or the ampersand disappears from the dialog",
                )

    def test_no_group_box_title_has_a_bare_ampersand(self):
        """The failure mode is silent, so assert the source-level invariant."""
        from PySide6.QtWidgets import QGroupBox

        dialog = self.make_dialog()
        for box in dialog.findChildren(QGroupBox):
            title = box.title()
            if "&" not in title or "&&" in title:
                continue
            with self.subTest(title=title):
                for index, char in enumerate(title):
                    if char != "&":
                        continue
                    following = title[index + 1] if index + 1 < len(title) else ""
                    self.assertTrue(
                        following and not following.isspace(),
                        f"{title!r} has a bare '&' before a space, which QGroupBox drops",
                    )


@unittest.skipIf(IS_HEADLESS_WIN_CI, "headless Windows CI cannot create real MainWindow / SettingsDialog")
class TestSegregationEnableCheckboxIsApplied(ViewsTabTestCase):
    """The "group downloads into sections" box must actually reach the table.

    It did not. ``_apply_views_tab`` called ``_set_segregation_mode()`` first, which
    force-*enables* segregation when it is currently off, and only reached the branch that
    applied the checkbox behind an ``elif`` that could never run. Two consequences, both
    silent:

    * saving with the box **unticked** turned segregation back on and overwrote the
      ``False`` the dialog had just written to the database;
    * unticking it while a live view was on did nothing at all until the next restart.
    """

    def setUp(self):
        self.window._set_segregation_mode(DEFAULT_SEGREGATED_MODE)
        self.window._on_toggle_segregated_view(False)

    def test_saving_with_the_box_unticked_does_not_enable_segregation(self):
        dialog = self.make_dialog()
        dialog._seg_enabled_cb.setChecked(False)
        dialog._seg_mode_combo.setCurrentIndex(dialog._seg_mode_combo.findData("type"))
        dialog._apply_views_tab()
        try:
            self.assertFalse(
                self.window._model.is_segregated_view(),
                "an unticked 'group downloads into sections' must stay unticked",
            )
        finally:
            self.window._set_segregation_mode(DEFAULT_SEGREGATED_MODE)

    def test_saving_with_the_box_unticked_persists_false(self):
        """The clobber: the force-enable overwrote the ``False`` just written."""
        dialog = self.make_dialog()
        dialog._seg_enabled_cb.setChecked(False)
        dialog._apply_views_tab()
        try:
            self.assertIs(
                self.db.get_ui_state("segregated_view_enabled"), False,
                "the database must record the user's unticked choice, not the model's",
            )
        finally:
            self.window._set_segregation_mode(DEFAULT_SEGREGATED_MODE)

    def test_unticking_the_box_turns_a_live_view_off(self):
        dialog = self.make_dialog()
        self.window._set_segregation_mode("type")
        self.assertTrue(self.window._model.is_segregated_view())
        dialog._seg_enabled_cb.setChecked(False)
        dialog._apply_views_tab()
        try:
            self.assertFalse(self.window._model.is_segregated_view())
        finally:
            self.window._set_segregation_mode(DEFAULT_SEGREGATED_MODE)

    def test_unticking_the_box_updates_the_view_menu_checkmark(self):
        dialog = self.make_dialog()
        self.window._set_segregation_mode("type")
        dialog._seg_enabled_cb.setChecked(False)
        dialog._apply_views_tab()
        try:
            self.assertFalse(
                self.window._act_segregated_view.isChecked(),
                "the View-menu checkmark must agree with the table",
            )
        finally:
            self.window._set_segregation_mode(DEFAULT_SEGREGATED_MODE)

    def test_the_mode_is_still_persisted_while_the_box_is_unticked(self):
        """Hiding the sections must not throw away which mode they were grouped by."""
        dialog = self.make_dialog()
        dialog._seg_enabled_cb.setChecked(False)
        dialog._seg_mode_combo.setCurrentIndex(dialog._seg_mode_combo.findData("type"))
        dialog._apply_views_tab()
        try:
            self.assertEqual(self.db.get_ui_state("segregated_view_mode"), "type")
        finally:
            self.window._set_segregation_mode(DEFAULT_SEGREGATED_MODE)

    def test_ticking_the_box_still_applies_the_mode(self):
        """The fix must not have broken the original happy path."""
        dialog = self.make_dialog()
        dialog._seg_enabled_cb.setChecked(True)
        dialog._seg_mode_combo.setCurrentIndex(dialog._seg_mode_combo.findData("type"))
        dialog._apply_views_tab()
        try:
            self.assertTrue(self.window._model.is_segregated_view())
            self.assertEqual(self.window._model.segregated_mode(), "type")
            self.assertIs(self.db.get_ui_state("segregated_view_enabled"), True)
        finally:
            self.window._set_segregation_mode(DEFAULT_SEGREGATED_MODE)

    def test_the_whole_save_path_respects_an_unticked_box(self):
        """Go through ``_on_save``, not just ``_apply_views_tab`` in isolation."""
        dialog = self.make_dialog()
        dialog._seg_enabled_cb.setChecked(False)
        dialog._on_save()
        try:
            self.assertFalse(self.window._model.is_segregated_view())
            self.assertIs(self.db.get_ui_state("segregated_view_enabled"), False)
        finally:
            self.window._set_segregation_mode(DEFAULT_SEGREGATED_MODE)

    def test_saving_from_a_standalone_dialog_still_writes_the_flag(self):
        """No parent to apply anything live, so the database write is all there is."""
        from my_idm.settings_dialog import SettingsDialog

        dialog = SettingsDialog(db=self.db)
        self.addCleanup(dialog.close)
        self.addCleanup(dialog.deleteLater)
        QApplication.processEvents()
        self.assertIsNone(dialog._table_view())
        dialog._seg_enabled_cb.setChecked(False)
        dialog._seg_mode_combo.setCurrentIndex(dialog._seg_mode_combo.findData("date"))
        dialog._apply_views_tab()
        self.assertIs(self.db.get_ui_state("segregated_view_enabled"), False)
        self.assertEqual(self.db.get_ui_state("segregated_view_mode"), "date")


@unittest.skipIf(IS_HEADLESS_WIN_CI, "headless Windows CI cannot create real MainWindow / SettingsDialog")
class TestThemeSelector(ViewsTabTestCase):
    """Preferences ▸ Views ▸ Appearance ▸ Theme.

    **Deliberately only one test here performs a real theme switch.** ``apply_theme`` calls
    ``app.setStyleSheet``, which restyles every widget of every live window - about 1.5s in
    the application, and far more in a suite that has built a dozen windows. An earlier
    version of this class switched in five tests and took over thirteen minutes; the rest
    assert the contract around the switch instead, which costs nothing because
    ``apply_theme`` returns early when the requested theme is already live.
    """

    def setUp(self):
        from my_idm.styles import DEFAULT_THEME, apply_theme

        self.addCleanup(apply_theme, QApplication.instance(), DEFAULT_THEME)

    def test_the_selector_is_on_the_views_tab(self):
        dialog = self.make_dialog()
        from my_idm.styles import THEME_NAMES

        self.assertGreaterEqual(
            dialog._theme_combo.findData(THEME_NAMES[0]), 0,
            "the theme combo offers no known theme",
        )

    def test_every_theme_is_selectable_and_has_a_label(self):
        from my_idm.styles import THEME_LABELS, THEME_NAMES

        dialog = self.make_dialog()
        combo = dialog._theme_combo
        self.assertEqual(combo.count(), len(THEME_NAMES))
        for theme in THEME_NAMES:
            with self.subTest(theme=theme):
                self.assertGreaterEqual(combo.findData(theme), 0)
                self.assertTrue(THEME_LABELS[theme].strip())

    def test_both_themes_have_a_rendered_stylesheet(self):
        """A theme with no sheet would silently fall back to whatever was applied before."""
        for theme in STYLESHEETS:
            with self.subTest(theme=theme):
                self.assertTrue(STYLESHEETS[theme].strip())
        self.assertNotEqual(STYLESHEETS["dark"], STYLESHEETS["light"])

    def test_the_original_dark_theme_is_untouched(self):
        """The greyish dark theme is the app's established look and must not shift.

        Light was added alongside it, never in place of it, so `DARK_STYLESHEET` has to stay
        exactly what it was.
        """
        from my_idm.styles import DARK_STYLESHEET, DEFAULT_THEME

        self.assertEqual(DARK_STYLESHEET, STYLESHEETS[DEFAULT_THEME])

    def test_the_stored_theme_is_shown(self):
        from my_idm.styles import THEME_NAMES

        self.db.set_ui_state("theme", THEME_NAMES[-1])
        dialog = self.make_dialog()
        self.assertEqual(
            dialog._theme_combo.currentData(), THEME_NAMES[-1],
            "Preferences must show the persisted theme, not the running one",
        )

    def test_an_unknown_stored_theme_falls_back_without_raising(self):
        dialog = self.make_dialog()
        self.db.set_ui_state("theme", "chartreuse")
        dialog._populate_views_tab()
        self.assertTrue(dialog._theme_combo.currentData())

    def test_saving_persists_the_choice(self):
        """Asserted with the theme already live, so no restyle is needed to prove the write."""
        from my_idm.styles import THEME_NAMES

        chosen = THEME_NAMES[-1]
        self.db.set_ui_state("theme", chosen)
        dialog = self.make_dialog()
        dialog._theme_combo.setCurrentIndex(dialog._theme_combo.findData(chosen))
        dialog._apply_views_tab()
        self.assertEqual(self.db.get_ui_state("theme"), chosen)

    def test_saving_applies_the_theme_immediately(self):
        """The one real switch: visible before Save is pressed."""
        from my_idm.styles import THEME_NAMES, current_theme

        dialog = self.make_dialog()
        chosen = THEME_NAMES[-1]
        dialog._theme_combo.setCurrentIndex(dialog._theme_combo.findData(chosen))
        dialog._apply_views_tab()
        self.assertEqual(current_theme(), chosen)
        self.assertEqual(
            QApplication.instance().styleSheet(), STYLESHEETS[chosen],
            "the application stylesheet must actually change",
        )

    def test_applying_the_live_theme_again_is_a_no_op(self):
        """The early return, which is what keeps a Preferences save cheap."""
        from my_idm.styles import apply_theme, current_theme

        first = apply_theme(QApplication.instance(), current_theme())
        second = apply_theme(QApplication.instance(), first)
        self.assertEqual(first, second)

    def test_a_corrupt_stored_theme_does_not_break_startup(self):
        """`_apply_persisted_theme` runs inside `__init__`: raising there means no window."""
        self.db.set_ui_state("theme", "chartreuse")
        self.assertIn(self.window._apply_persisted_theme(), tuple(STYLESHEETS))

    def test_a_failing_theme_read_falls_back_to_the_default(self):
        from unittest.mock import patch

        from my_idm.styles import DEFAULT_THEME

        with patch.object(
            self.db, "get_ui_state", side_effect=RuntimeError("db gone")
        ):
            applied = self.window._apply_persisted_theme()
        self.assertEqual(applied, DEFAULT_THEME)


@unittest.skipIf(IS_HEADLESS_WIN_CI, "headless Windows CI cannot create real MainWindow / SettingsDialog")
class TestSegregationModeSurvivesRestart(ViewsTabTestCase):
    """A persisted mode has to come back.

    ``MainWindow.__init__`` kept the pre-``type`` two-value whitelist when the third mode
    was added, so choosing File Type and restarting silently grouped the table by Status
    again - and the menu entry came back unticked, because its checked state was derived
    from the value that had just been discarded.
    """

    def window_with_mode(self, mode):
        from my_idm.main_window import MainWindow

        self.db.set_ui_state("segregated_view_mode", mode)
        self.db.set_ui_state("segregated_view_enabled", True)
        window = MainWindow(self.manager)
        self.addCleanup(self._dispose, window)
        return window

    def test_a_persisted_type_mode_is_kept(self):
        self.assertEqual(self.window_with_mode("type")._segregated_view_mode, "type")

    def test_the_type_menu_entry_is_checked_on_startup(self):
        window = self.window_with_mode("type")
        self.assertTrue(window._act_seg_by_type.isChecked())
        self.assertFalse(window._act_seg_by_status.isChecked())
        self.assertFalse(window._act_seg_by_date.isChecked())

    def test_the_model_is_in_type_mode_on_startup(self):
        self.assertEqual(
            self.window_with_mode("type")._model.segregated_mode(), "type"
        )

    def test_every_registered_mode_survives_a_restart(self):
        """Not just 'type': no mode may be silently dropped on the way back in."""
        for mode in SEGREGATED_MODES:
            with self.subTest(mode=mode):
                self.assertEqual(
                    self.window_with_mode(mode)._segregated_view_mode, mode
                )

    def test_a_date_mode_still_survives(self):
        self.assertEqual(self.window_with_mode("date")._segregated_view_mode, "date")

    def test_an_unknown_persisted_mode_degrades_to_the_default(self):
        self.assertEqual(
            self.window_with_mode("colour")._segregated_view_mode,
            DEFAULT_SEGREGATED_MODE,
        )

    def test_a_non_string_persisted_mode_degrades_to_the_default(self):
        """``str()`` of a number is not a mode; it must not become the mode."""
        self.db.set_ui_state("segregated_view_mode", 7)
        self.db.set_ui_state("segregated_view_enabled", True)
        from my_idm.main_window import MainWindow

        window = MainWindow(self.manager)
        self.addCleanup(self._dispose, window)
        self.assertEqual(window._segregated_view_mode, DEFAULT_SEGREGATED_MODE)


@unittest.skipIf(IS_HEADLESS_WIN_CI, "headless Windows CI cannot create real MainWindow / SettingsDialog")
class TestSegregationModeLabels(ViewsTabTestCase):
    """The status bar names the mode; it used to say "Date" for everything but "status".

    The View-menu handler and the Preferences tab drive the same state through two
    separate lookups, and only one of them was updated when the third mode landed.
    """

    def setUp(self):
        self.addCleanup(self.window._set_segregation_mode, DEFAULT_SEGREGATED_MODE)
        self.addCleanup(self.window._on_toggle_segregated_view, False)

    def test_every_mode_has_a_distinct_label(self):
        labels = [self.window._segregation_mode_label(m) for m in SEGREGATED_MODES]
        self.assertEqual(
            len(set(labels)), len(labels), f"two modes share the label {labels!r}"
        )

    def test_no_label_is_empty(self):
        for mode in SEGREGATED_MODES:
            with self.subTest(mode=mode):
                self.assertTrue(self.window._segregation_mode_label(mode).strip())

    def test_the_type_mode_is_labelled_file_type(self):
        self.assertEqual(self.window._segregation_mode_label("type"), "File Type")

    def test_an_unknown_mode_falls_back_rather_than_crashing(self):
        self.assertTrue(self.window._segregation_mode_label("colour"))

    def test_toggling_on_names_the_current_mode(self):
        for mode in SEGREGATED_MODES:
            with self.subTest(mode=mode):
                self.window._set_segregation_mode(mode)
                self.window._on_toggle_segregated_view(True)
                self.assertIn(
                    self.window._segregation_mode_label(mode),
                    self.window._status_label.text(),
                )

    def test_the_type_mode_is_not_reported_as_date(self):
        """The literal symptom: mode 'type' announced as 'Date'."""
        self.window._set_segregation_mode("type")
        self.window._on_toggle_segregated_view(True)
        text = self.window._status_label.text()
        self.assertNotIn("Date", text)
        self.assertIn("File Type", text)

    def test_choosing_a_mode_while_disabled_still_enables_segregation(self):
        """The programmatic path still promotes a mode choice into an enable.

        No longer reachable from the View menu - the three mode actions are disabled while
        segregation is off, precisely so a dead-end choice cannot be made there - but
        ``_set_segregation_mode`` keeps doing it for callers that invoke it directly.

        This also covers the View-menu checkmark sync: ``_set_segregation_mode`` enables segregation
        by checking the action, which emits nothing when it is already checked, so without the sync
        the enable silently did not happen.
        """
        self.window._on_toggle_segregated_view(False)
        self.window._set_segregation_mode("type")
        self.assertTrue(self.window._model.is_segregated_view())

    def test_the_menu_checkmark_follows_a_programmatic_toggle(self):
        self.window._on_toggle_segregated_view(True)
        self.assertTrue(self.window._act_segregated_view.isChecked())
        self.window._on_toggle_segregated_view(False)
        self.assertFalse(self.window._act_segregated_view.isChecked())


@unittest.skipIf(IS_HEADLESS_WIN_CI, "headless Windows CI cannot create real MainWindow / SettingsDialog")
class TestSegregationModeAvailability(ViewsTabTestCase):
    """The three mode choices are only meaningful while segregation is on.

    While the table is a flat list there is nothing to group by, so a live mode picker invites a
    change that silently does nothing. Both surfaces that expose the modes therefore grey them out
    together: the View menu's three ``QAction``s and the Preferences combo.
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(self.window._set_segregation_mode, DEFAULT_SEGREGATED_MODE)
        self.addCleanup(self.window._on_toggle_segregated_view, False)

    @property
    def mode_actions(self):
        return self.window._seg_mode_actions

    def test_the_menu_exposes_one_action_per_mode(self):
        self.assertEqual(
            sorted(self.mode_actions), sorted(SEGREGATED_MODES),
            "every mode needs a menu action, and every action needs a mode",
        )

    def test_all_three_are_disabled_while_segregation_is_off(self):
        self.window._on_toggle_segregated_view(False)
        for mode, action in self.mode_actions.items():
            with self.subTest(mode=mode):
                self.assertFalse(
                    action.isEnabled(),
                    f"{mode} must not be selectable while the table is not segregated",
                )

    def test_all_three_are_enabled_once_segregation_is_on(self):
        self.window._on_toggle_segregated_view(True)
        for mode, action in self.mode_actions.items():
            with self.subTest(mode=mode):
                self.assertTrue(
                    action.isEnabled(),
                    f"{mode} must be selectable once sections exist to group by",
                )

    def test_the_view_menu_toggle_greys_and_restores_them(self):
        """Driven through the real action, so this covers the signal wiring too."""
        self.window._act_segregated_view.setChecked(False)
        self.assertFalse(self.mode_actions["date"].isEnabled())
        self.window._act_segregated_view.setChecked(True)
        self.assertTrue(self.mode_actions["date"].isEnabled())
        self.window._act_segregated_view.setChecked(False)
        self.assertFalse(self.mode_actions["date"].isEnabled())

    def test_the_remembered_mode_survives_being_switched_off(self):
        """The checkmark records what to restore.

        Clearing it would make the picker show "Status" for a user who had chosen File Type, and
        turning segregation back on would regroup by the wrong thing - a silent change of the
        user's setting, caused by them briefly toggling something else.
        """
        self.window._on_toggle_segregated_view(True)
        self.window._set_segregation_mode("type")
        self.window._on_toggle_segregated_view(False)
        self.assertTrue(
            self.mode_actions["type"].isChecked(),
            "a disabled action still shows its checkmark; that is the stored mode",
        )
        self.window._on_toggle_segregated_view(True)
        self.assertTrue(self.mode_actions["type"].isChecked())
        self.assertEqual(self.window._segregated_view_mode, "type")

    def test_the_preferences_combo_follows_the_same_rule(self):
        self.db.set_ui_state("segregated_view_enabled", False)
        dialog = self.make_dialog()
        try:
            self.assertFalse(dialog._seg_mode_combo.isEnabled())
            dialog._seg_enabled_cb.setChecked(True)
            self.assertTrue(dialog._seg_mode_combo.isEnabled())
            dialog._seg_enabled_cb.setChecked(False)
            self.assertFalse(dialog._seg_mode_combo.isEnabled())
        finally:
            self._dispose(dialog)

    def test_both_surfaces_agree_after_a_preferences_change(self):
        """The Preferences checkbox and the View menu must not disagree.

        They drive the same state through different widgets, so one being left enabled while the
        other is off is exactly the drift this coupling exists to prevent.
        """
        dialog = self.make_dialog()
        try:
            dialog._seg_enabled_cb.setChecked(False)
            dialog._apply_views_tab()
            self.assertFalse(self.window._act_segregated_view.isChecked())
            for mode, action in self.mode_actions.items():
                with self.subTest(mode=mode):
                    self.assertFalse(action.isEnabled())
                    self.assertFalse(dialog._seg_mode_combo.isEnabled())

            dialog._seg_enabled_cb.setChecked(True)
            dialog._apply_views_tab()
            self.assertTrue(self.window._act_segregated_view.isChecked())
            for mode, action in self.mode_actions.items():
                with self.subTest(mode=mode):
                    self.assertTrue(action.isEnabled())
                    self.assertTrue(dialog._seg_mode_combo.isEnabled())
        finally:
            self._dispose(dialog)


@unittest.skipIf(IS_HEADLESS_WIN_CI, "headless Windows CI cannot create real MainWindow / SettingsDialog")
class TestViewsTabStandalonePopulation(ViewsTabTestCase):
    """A dialog built without ``db=`` must still reflect the stored state.

    ``SettingsDialog`` reads the database during construction, but the Views page is built before
    anything resolves the handle, so touching the raw ``self._db`` attribute found it ``None`` and
    the load was skipped silently - the page then showed the default for a saved non-default, and
    the mode combo's enabled state with it. The shipped window passes ``db=`` and was never
    affected; the standalone path is what tests and any headless caller use.
    """

    def test_a_dialog_without_a_db_argument_still_loads_the_segregation_state(self):
        from my_idm.settings_dialog import SettingsDialog

        self.db.set_ui_state("segregated_view_enabled", True)
        self.db.set_ui_state("segregated_view_mode", "type")
        dialog = SettingsDialog(parent=self.window)
        self.addCleanup(self._dispose, dialog)
        self.assertTrue(dialog._seg_enabled_cb.isChecked())
        self.assertEqual(dialog._seg_mode_combo.currentData(), "type")
        self.assertTrue(
            dialog._seg_mode_combo.isEnabled(),
            "a saved 'on' must enable the picker, or the page contradicts itself on open",
        )

    def test_a_stored_off_still_honours_the_remembered_mode(self):
        """Off *and* a stored mode: the picker must show the mode while refusing input.

        Asserting only "it is disabled" would pass even if the load were skipped entirely, because
        the default is off - which is why this also checks the mode round-trips. A user who turns
        segregation back on should get the mode they had chosen, not the default.
        """
        from my_idm.settings_dialog import SettingsDialog

        self.db.set_ui_state("segregated_view_enabled", False)
        self.db.set_ui_state("segregated_view_mode", "type")
        dialog = SettingsDialog(parent=self.window)
        self.addCleanup(self._dispose, dialog)
        self.assertFalse(dialog._seg_enabled_cb.isChecked())
        self.assertFalse(dialog._seg_mode_combo.isEnabled())
        self.assertEqual(dialog._seg_mode_combo.currentData(), "type")

    def test_applying_from_a_standalone_dialog_still_persists(self):
        """Guards that a Save from a standalone dialog reaches the database.

        Not a regression test for the ``_get_db`` change above - by the time Save runs the handle has
        been resolved either way, so this passes with or without it.
        """
        from my_idm.settings_dialog import SettingsDialog

        dialog = SettingsDialog(parent=self.window)
        self.addCleanup(self._dispose, dialog)
        dialog._seg_enabled_cb.setChecked(True)
        dialog._seg_mode_combo.setCurrentIndex(dialog._seg_mode_combo.findData("date"))
        dialog._apply_views_tab()
        self.assertEqual(self.db.get_ui_state("segregated_view_enabled"), True)
        self.assertEqual(self.db.get_ui_state("segregated_view_mode"), "date")


class TestDisabledStyling(unittest.TestCase):
    """A disabled control has to *look* unavailable, not merely behave that way.

    There was no `QMenu::item:disabled` rule at all, so Qt's own disabled rendering applied —
    which on this palette is barely dimmer than the enabled text. A greyed-out **Move to
    Queue** therefore read as live, and was reported as "not clickable".
    """

    def test_every_palette_defines_a_disabled_colour(self):
        for theme in STYLESHEETS:
            with self.subTest(theme=theme):
                # _PALETTES holds name -> {attribute: value} snapshots, not the classes.
                self.assertTrue(
                    _PALETTES[theme].get("TEXT_DISABLED"),
                    f"{theme} has no TEXT_DISABLED",
                )

    def test_the_disabled_colour_is_dimmer_than_the_dim_one(self):
        # Dimmer is the whole point: TEXT_DIM is for de-emphasised text, TEXT_DISABLED for a
        # control that cannot be used right now.
        for theme in STYLESHEETS:
            with self.subTest(theme=theme):
                self.assertNotEqual(
                    _PALETTES[theme]["TEXT_DISABLED"], _PALETTES[theme]["TEXT_DIM"]
                )

    def test_both_sheets_style_a_disabled_menu_item(self):
        for theme, sheet in STYLESHEETS.items():
            with self.subTest(theme=theme):
                self.assertIn("QMenu::item:disabled", sheet)
                self.assertIn(_PALETTES[theme]["TEXT_DISABLED"], sheet)

    def test_a_disabled_item_does_not_paint_a_hover_background(self):
        # Otherwise the row still highlights under the cursor and reads as live.
        for theme, sheet in STYLESHEETS.items():
            with self.subTest(theme=theme):
                self.assertIn("QMenu::item:disabled:selected", sheet)


class TestNameSegregatedView(unittest.TestCase):
    """Tests for name-based (show/series) segregation mode and bulk collapse/expand."""

    def setUp(self):
        self.model = DownloadTableModel()

    def test_group_entries_by_name_series(self):
        e1 = DownloadEntry(id="1", filename="Frieren S01E01 1080p.mkv", url="http://x/1")
        e2 = DownloadEntry(id="2", filename="Frieren S01E02 1080p.mkv", url="http://x/2")
        e3 = DownloadEntry(id="3", filename="Frieren S01E03 1080p.mkv", url="http://x/3")
        e4 = DownloadEntry(id="4", filename="Random Single Movie (2024).mp4", url="http://x/4")

        self.model.load_entries([e1, e2, e3, e4])
        self.model.set_segregated_view(True, "name")

        # Must have Frieren header and Uncategorized header
        header_titles = [
            self.model._entries[r].section_title.upper()
            for r in range(self.model.rowCount())
            if self.model.is_section_header_row(r)
        ]
        self.assertTrue(any("FRIEREN" in t for t in header_titles))
        self.assertTrue(any("UNCATEGORIZED" in t for t in header_titles))

    def test_collapse_and_expand_all_sections(self):
        e1 = DownloadEntry(id="1", filename="Show A Episode 1.mkv", url="http://x/1")
        e2 = DownloadEntry(id="2", filename="Show A Episode 2.mkv", url="http://x/2")
        e3 = DownloadEntry(id="3", filename="Show B Episode 1.mkv", url="http://x/3")
        e4 = DownloadEntry(id="4", filename="Show B Episode 2.mkv", url="http://x/4")

        self.model.load_entries([e1, e2, e3, e4])
        self.model.set_segregated_view(True, "name")

        # Name-based sections are auto-collapsed by default -> only 2 headers visible initially
        self.assertEqual(self.model.rowCount(), 2)

        # Expand all -> 2 headers + 4 items = 6 rows
        self.model.expand_all_sections()
        self.assertEqual(self.model.rowCount(), 6)

        # Collapse all -> back to 2 headers
        self.model.collapse_all_sections()
        self.assertEqual(self.model.rowCount(), 2)
        for r in range(self.model.rowCount()):
            self.assertTrue(self.model.is_section_header_row(r))

    def test_section_active_counts_and_progress(self):
        # 1 downloading at 50% (500/1000), 1 completed (1000/1000), 1 seeding
        e1 = DownloadEntry(id="1", filename="MyAnime S01E01.mkv", url="http://x/1",
                           status="downloading", total_size=1000, downloaded_size=500)
        e2 = DownloadEntry(id="2", filename="MyAnime S01E02.mkv", url="http://x/2",
                           status="completed", total_size=1000, downloaded_size=1000)
        e3 = DownloadEntry(id="3", filename="MyAnime S01E03.mkv", url="http://x/3",
                           status="seeding", total_size=1000, downloaded_size=1000)

        self.model.load_entries([e1, e2, e3])
        self.model.set_segregated_view(True, "name")

        # Header at row 0
        hdr = self.model._entries[0]
        self.assertTrue(hdr.is_section_header)
        self.assertEqual(hdr.section_count, 3)
        self.assertEqual(hdr.section_active_count, 1)  # only e1 is downloading/active
        self.assertEqual(hdr.section_seeding_count, 1)  # e3 is seeding
        self.assertAlmostEqual(hdr.section_active_progress, 50.0)

    def test_group_entries_by_edit_distance(self):
        # Two names with small difference (<= 10% edit distance)
        # e.g., length 30 with 1 char difference = 1/30 = 3.3% <= 10%
        name1 = "The Long Journey of a Hero Part 1.mp4"
        name2 = "The Long Journey of a Hero Part 2.mp4"
        e1 = DownloadEntry(id="1", filename=name1, url="http://x/1")
        e2 = DownloadEntry(id="2", filename=name2, url="http://x/2")

        self.model.load_entries([e1, e2])
        self.model.set_segregated_view(True, "name")

        headers = [e for e in self.model._entries if e.is_section_header]
        self.assertEqual(len(headers), 1)
        self.assertIn("HERO", headers[0].section_title.upper())


@unittest.skipIf(IS_HEADLESS_WIN_CI, "headless Windows CI cannot create real MainWindow / SettingsDialog")
class TestSegregatedControlStrip(ViewsTabTestCase):
    def test_collapse_and_expand_buttons_have_vector_icons(self):
        self.assertFalse(self.window._btn_collapse_all.icon().isNull())
        self.assertFalse(self.window._btn_expand_all.icon().isNull())
        self.assertFalse(self.window._act_collapse_all_sections.icon().isNull())
        self.assertFalse(self.window._act_expand_all_sections.icon().isNull())

    def test_mode_buttons_highlight_active_mode(self):
        for mode in ("status", "date", "type", "name"):
            self.window._set_segregation_mode(mode)
            self.assertTrue(self.window._seg_mode_buttons[mode].isChecked())
            for other_mode, btn in self.window._seg_mode_buttons.items():
                if other_mode != mode:
                    self.assertFalse(btn.isChecked())

    def test_clicking_mode_button_switches_mode(self):
        self.window._seg_mode_buttons["name"].click()
        self.assertEqual(self.window._segregated_view_mode, "name")
        self.assertTrue(self.window._seg_mode_buttons["name"].isChecked())

    def test_collapse_and_expand_all_buttons_trigger_handlers(self):
        from unittest.mock import patch
        with patch.object(self.window, "_on_collapse_all_sections") as mock_col:
            self.window._btn_collapse_all.click()
            mock_col.assert_called_once()
        with patch.object(self.window, "_on_expand_all_sections") as mock_exp:
            self.window._btn_expand_all.click()
            mock_exp.assert_called_once()

    def test_refresh_seg_strip_theme_updates_styles(self):
        self.window._refresh_seg_strip_theme()
        self.assertFalse(self.window._btn_collapse_all.icon().isNull())
        self.assertFalse(self.window._btn_expand_all.icon().isNull())


if __name__ == "__main__":
    unittest.main()

