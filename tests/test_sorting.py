import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from datetime import datetime, timezone

from PySide6.QtCore import Qt, QModelIndex, QSettings
from PySide6.QtWidgets import QApplication, QTableView

from my_idm.database import Database, DownloadEntry
from my_idm.download_model import DownloadTableModel, Col
from my_idm.main_window import MainWindow
from my_idm.manager import DownloadManager

# Ensure single QApplication instance for tests
app = QApplication.instance() or QApplication([])


def _make_entry(id: str, name: str, size: int = 1000, progress: float = 0.0,
                status: str = "queued", speed: float = 0.0, eta: float = 0.0,
                download_type: str = "http", added_at: str = "2026-01-01T00:00:00Z",
                completed_at: str = "") -> DownloadEntry:
    return DownloadEntry(
        id=id,
        url=f"https://example.com/{name}",
        save_path=f"C:/Downloads/{name}",
        filename=name,
        total_size=size,
        downloaded_size=int(size * (progress / 100.0)),
        status=status,
        download_type=download_type,
        added_at=added_at,
        completed_at=completed_at,
        speed=speed,
        eta_seconds=eta,
    )


class TestDownloadSorting(unittest.TestCase):

    def setUp(self):
        self.model = DownloadTableModel()
        self.e1 = _make_entry("1", "bravo.zip", size=2000, progress=50.0,
                              status="downloading", speed=500.0, eta=30.0,
                              added_at="2026-03-01T10:00:00Z", completed_at="")
        self.e2 = _make_entry("2", "alpha.iso", size=1000, progress=100.0,
                              status="completed", speed=0.0, eta=0.0,
                              added_at="2026-01-01T10:00:00Z", completed_at="2026-01-01T11:00:00Z")
        self.e3 = _make_entry("3", "charlie.mp4", size=5000, progress=10.0,
                              status="downloading", speed=1500.0, eta=120.0,
                              added_at="2026-05-01T10:00:00Z", completed_at="")

    def test_default_sort_configuration(self):
        """Model defaults to Added column descending."""
        self.assertEqual(self.model.sort_column, Col.ADDED)
        self.assertEqual(self.model.sort_order, Qt.SortOrder.DescendingOrder)

    def test_load_entries_default_sorted_added_desc(self):
        """Loading entries automatically sorts them by Added DESC (newest first)."""
        self.model.load_entries([self.e1, self.e2, self.e3])
        # e3 (May) -> e1 (March) -> e2 (Jan)
        self.assertEqual(self.model.rowCount(), 3)
        self.assertEqual(self.model.get_entry(0).id, "3")
        self.assertEqual(self.model.get_entry(1).id, "1")
        self.assertEqual(self.model.get_entry(2).id, "2")

    def test_sort_by_added_asc(self):
        """Sorting by Added ASC orders oldest first."""
        self.model.load_entries([self.e1, self.e2, self.e3])
        self.model.sort(Col.ADDED, Qt.SortOrder.AscendingOrder)
        # e2 (Jan) -> e1 (March) -> e3 (May)
        self.assertEqual(self.model.get_entry(0).id, "2")
        self.assertEqual(self.model.get_entry(1).id, "1")
        self.assertEqual(self.model.get_entry(2).id, "3")

    def test_sort_by_name(self):
        """Sorting by Name ASC and DESC."""
        self.model.load_entries([self.e1, self.e2, self.e3])
        self.model.sort(Col.NAME, Qt.SortOrder.AscendingOrder)
        # alpha -> bravo -> charlie
        self.assertEqual(self.model.get_entry(0).filename, "alpha.iso")
        self.assertEqual(self.model.get_entry(1).filename, "bravo.zip")
        self.assertEqual(self.model.get_entry(2).filename, "charlie.mp4")

        self.model.sort(Col.NAME, Qt.SortOrder.DescendingOrder)
        # charlie -> bravo -> alpha
        self.assertEqual(self.model.get_entry(0).filename, "charlie.mp4")
        self.assertEqual(self.model.get_entry(1).filename, "bravo.zip")
        self.assertEqual(self.model.get_entry(2).filename, "alpha.iso")

    def test_sort_by_size(self):
        """Sorting by Size ASC and DESC."""
        self.model.load_entries([self.e1, self.e2, self.e3])
        self.model.sort(Col.SIZE, Qt.SortOrder.AscendingOrder)
        # 1000 (alpha) -> 2000 (bravo) -> 5000 (charlie)
        self.assertEqual(self.model.get_entry(0).total_size, 1000)
        self.assertEqual(self.model.get_entry(1).total_size, 2000)
        self.assertEqual(self.model.get_entry(2).total_size, 5000)

        self.model.sort(Col.SIZE, Qt.SortOrder.DescendingOrder)
        self.assertEqual(self.model.get_entry(0).total_size, 5000)
        self.assertEqual(self.model.get_entry(1).total_size, 2000)
        self.assertEqual(self.model.get_entry(2).total_size, 1000)

    def test_sort_by_speed(self):
        """Sorting by Speed ASC and DESC."""
        self.model.load_entries([self.e1, self.e2, self.e3])
        self.model.sort(Col.SPEED, Qt.SortOrder.AscendingOrder)
        # 0.0 (alpha) -> 500.0 (bravo) -> 1500.0 (charlie)
        self.assertEqual(self.model.get_entry(0).id, "2")
        self.assertEqual(self.model.get_entry(1).id, "1")
        self.assertEqual(self.model.get_entry(2).id, "3")

        self.model.sort(Col.SPEED, Qt.SortOrder.DescendingOrder)
        self.assertEqual(self.model.get_entry(0).id, "3")
        self.assertEqual(self.model.get_entry(1).id, "1")
        self.assertEqual(self.model.get_entry(2).id, "2")

    def test_sort_by_eta(self):
        """Active ETAs appear before inactive ones in both ASC and DESC."""
        self.model.load_entries([self.e1, self.e2, self.e3])
        self.model.sort(Col.ETA, Qt.SortOrder.AscendingOrder)
        # e1 (30s) -> e3 (120s) -> e2 (no ETA, completed)
        self.assertEqual(self.model.get_entry(0).id, "1")
        self.assertEqual(self.model.get_entry(1).id, "3")
        self.assertEqual(self.model.get_entry(2).id, "2")

        self.model.sort(Col.ETA, Qt.SortOrder.DescendingOrder)
        # e3 (120s) -> e1 (30s) -> e2 (no ETA, completed)
        self.assertEqual(self.model.get_entry(0).id, "3")
        self.assertEqual(self.model.get_entry(1).id, "1")
        self.assertEqual(self.model.get_entry(2).id, "2")

    def test_sort_by_completed(self):
        """Completed downloads appear before uncompleted ones."""
        e4 = _make_entry("4", "delta.tar", completed_at="2026-02-01T10:00:00Z")
        self.model.load_entries([self.e1, self.e2, self.e3, e4])

        self.model.sort(Col.COMPLETED, Qt.SortOrder.AscendingOrder)
        # Completed: e2 (Jan 1) -> e4 (Feb 1), followed by uncompleted e1, e3
        self.assertEqual(self.model.get_entry(0).id, "2")
        self.assertEqual(self.model.get_entry(1).id, "4")

        self.model.sort(Col.COMPLETED, Qt.SortOrder.DescendingOrder)
        # Completed: e4 (Feb 1) -> e2 (Jan 1), followed by uncompleted e1, e3
        self.assertEqual(self.model.get_entry(0).id, "4")
        self.assertEqual(self.model.get_entry(1).id, "2")

    def test_add_entry_inserts_into_correct_sorted_row(self):
        """Adding an entry inserts it at the proper position under current sort."""
        self.model.load_entries([self.e1, self.e2])  # e1: March, e2: Jan (default sort: Added DESC)
        # e1 (March) at row 0, e2 (Jan) at row 1

        # Add newest entry (July) -> should insert at row 0
        newest = _make_entry("new", "new.zip", added_at="2026-07-01T10:00:00Z")
        self.model.add_entry(newest)
        self.assertEqual(self.model.get_entry(0).id, "new")
        self.assertEqual(self.model.get_entry(1).id, "1")
        self.assertEqual(self.model.get_entry(2).id, "2")

        # Add oldest entry (2025) -> should insert at bottom (row 3)
        oldest = _make_entry("old", "old.zip", added_at="2025-01-01T10:00:00Z")
        self.model.add_entry(oldest)
        self.assertEqual(self.model.get_entry(3).id, "old")

    def test_selection_tracking_across_sort(self):
        """Persistent indexes and selection stay anchored to the item when sorted."""
        self.model.load_entries([self.e1, self.e2, self.e3])
        view = QTableView()
        view.setModel(self.model)
        view.setSortingEnabled(True)

        # In default Added DESC: row 0 is e3 ("3"), row 1 is e1 ("1"), row 2 is e2 ("2")
        self.assertEqual(self.model.get_entry(0).id, "3")
        view.selectRow(0)  # Select e3 ("charlie.mp4")

        selected_ids = self.model.get_selected_ids(view.selectionModel().selectedIndexes())
        self.assertEqual(selected_ids, ["3"])

        # Sort by Name ASC: e2 (alpha) at row 0, e1 (bravo) at row 1, e3 (charlie) at row 2
        view.sortByColumn(Col.NAME, Qt.SortOrder.AscendingOrder)
        self.assertEqual(self.model.get_entry(2).id, "3")

        # Selection should now point to row 2 (e3)
        selected_ids_after = self.model.get_selected_ids(view.selectionModel().selectedIndexes())
        self.assertEqual(selected_ids_after, ["3"])


class TestMainWindowSorting(unittest.TestCase):

    def setUp(self):
        QSettings("MyIDM", "My-IDM").clear()

    def tearDown(self):
        QSettings("MyIDM", "My-IDM").clear()

    def test_main_window_has_sorting_enabled(self):
        """MainWindow table has sorting enabled with Added DESC default."""
        db = Database(":memory:")
        db.open()
        mgr = DownloadManager(db)
        win = MainWindow(mgr)
        try:
            self.assertTrue(win._table.isSortingEnabled())
            header = win._table.horizontalHeader()
            self.assertEqual(header.sortIndicatorSection(), Col.ADDED)
            self.assertEqual(header.sortIndicatorOrder(), Qt.SortOrder.DescendingOrder)
        finally:
            win.close()
            db.close()

    def test_main_window_sort_helpers(self):
        """MainWindow sort helpers correctly update the table sort state."""
        db = Database(":memory:")
        db.open()
        mgr = DownloadManager(db)
        win = MainWindow(mgr)
        try:
            win._sort_by_column(Col.NAME)
            header = win._table.horizontalHeader()
            self.assertEqual(header.sortIndicatorSection(), Col.NAME)

            win._set_sort_order(Qt.SortOrder.AscendingOrder)
            self.assertEqual(header.sortIndicatorOrder(), Qt.SortOrder.AscendingOrder)

            # Switching back to Added column defaults to Descending order
            win._sort_by_column(Col.ADDED)
            self.assertEqual(header.sortIndicatorSection(), Col.ADDED)
            self.assertEqual(header.sortIndicatorOrder(), Qt.SortOrder.DescendingOrder)
        finally:
            win.close()
            db.close()

    def test_main_window_header_section_clicked_added_defaults_descending(self):
        """Clicking Date Added column header switches to it in descending order."""
        db = Database(":memory:")
        db.open()
        mgr = DownloadManager(db)
        win = MainWindow(mgr)
        try:
            # Change to Name column ascending
            win._table.sortByColumn(Col.NAME, Qt.SortOrder.AscendingOrder)
            win._last_sort_section = Col.NAME
            self.assertEqual(win._table.horizontalHeader().sortIndicatorSection(), Col.NAME)

            # Simulate clicking Added column header
            win._on_header_section_clicked(Col.ADDED)
            self.assertEqual(win._table.horizontalHeader().sortIndicatorSection(), Col.ADDED)
            self.assertEqual(win._table.horizontalHeader().sortIndicatorOrder(), Qt.SortOrder.DescendingOrder)
        finally:
            win.close()
            db.close()


if __name__ == "__main__":
    unittest.main()
