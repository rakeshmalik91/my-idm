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

import sys
import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QCheckBox, QListWidget

from my_idm.database import Database, DownloadEntry
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

class ViewsTabTestCase(unittest.TestCase):
    """A real MainWindow, so the tab is exercised against the header it edits."""

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

    def make_dialog(self):
        from my_idm.settings_dialog import SettingsDialog

        dialog = SettingsDialog(db=self.db, parent=self.window)
        self.addCleanup(dialog.close)
        self.addCleanup(dialog.deleteLater)
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
            titles.index("👁️ Views & Columns"), 1,
            "the Views tab belongs immediately after General",
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

        Note the app's *default* order is not identity order: ``_apply_default_tail_order``
        deliberately pins SOURCE_DOMAIN, FILE_NAME and the seeding columns to the tail. So
        this asserts the list mirrors the header rather than that it is sorted.
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


if __name__ == "__main__":
    unittest.main()
