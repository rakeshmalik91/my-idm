"""Unit tests for DownloadTableModel: column indexing, data formatting, sorting, and progress bar delegates."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from PySide6.QtCore import Qt, QModelIndex
from PySide6.QtWidgets import QApplication, QTableView

from my_idm.database import Database, DownloadEntry
from my_idm.download_model import DownloadTableModel, Col
from my_idm.delegates import ProgressBarDelegate
from my_idm.manager import DownloadManager
from my_idm.main_window import MainWindow

app = QApplication.instance() or QApplication([])


def _make_entry(id: str, name: str, size: int = 1000, progress: float = 0.0,
                status: str = "queued", speed: float = 0.0, eta: float = 0.0,
                download_type: str = "http", added_at: str = "2026-01-01T00:00:00Z",
                completed_at: str = "", queue_order: int = 0) -> DownloadEntry:
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
        queue_order=queue_order,
    )


class TestDownloadModel(unittest.TestCase):

    def setUp(self):
        self.model = DownloadTableModel()
        self.e1 = _make_entry("1", "bravo.zip", size=2000, progress=50.0,
                              status="downloading", speed=500.0, eta=30.0,
                              added_at="2026-03-01T10:00:00Z", completed_at="", queue_order=1)
        self.e2 = _make_entry("2", "alpha.iso", size=1000, progress=100.0,
                              status="completed", speed=0.0, eta=0.0,
                              added_at="2026-01-01T10:00:00Z", completed_at="2026-01-01T11:00:00Z", queue_order=2)
        self.e3 = _make_entry("3", "charlie.mp4", size=5000, progress=10.0,
                              status="downloading", speed=1500.0, eta=120.0,
                              added_at="2026-05-01T10:00:00Z", completed_at="", queue_order=3)

    def test_column_indices_and_headers(self):
        """Verify column 0 is QUEUE '#' and headers are properly mapped."""
        self.assertEqual(Col.QUEUE, 0)
        self.assertEqual(Col.HEADERS[Col.QUEUE], "#")
        self.assertEqual(Col.NAME, 1)
        self.assertEqual(Col.SIZE, 2)
        self.assertEqual(Col.PROGRESS, 3)
        self.assertEqual(Col.STATUS, 4)

    def test_queue_column_display_and_alignment(self):
        """Queue column displays 1-based order for active downloads and empty for completed."""
        self.model.load_entries([self.e1, self.e2])
        idx1 = self.model.index(0, Col.QUEUE)
        idx2 = self.model.index(1, Col.QUEUE)

        self.assertEqual(self.model.data(idx1, Qt.ItemDataRole.TextAlignmentRole), Qt.AlignmentFlag.AlignCenter)

        # One entry is downloading (shows queue order), the other is completed (empty)
        val1 = self.model.data(idx1, Qt.ItemDataRole.DisplayRole)
        val2 = self.model.data(idx2, Qt.ItemDataRole.DisplayRole)
        # e1 is downloading → shows number, e2 is completed → shows ""
        values = {val1, val2}
        self.assertIn("", values, "Completed download should show empty queue order")
        self.assertTrue(any(v.isdigit() for v in values), "Active download should show numeric queue order")

    def test_torrent_speed_shows_down_and_up(self):
        """Torrent speed column shows both down and up speed formatted."""
        entry = DownloadEntry(
            id="t1",
            filename="ubuntu.iso",
            url="magnet:?xt=urn:btih:abc",
            download_type="torrent",
            status="downloading",
            speed=1048576.0,       # 1 MB/s
            upload_speed=262144.0, # 256 KB/s
        )
        self.model.load_entries([entry])
        idx = self.model.index(0, Col.SPEED)
        display_val = self.model.data(idx, Qt.ItemDataRole.DisplayRole)
        self.assertIn("↓", display_val)
        self.assertIn("↑", display_val)
        self.assertIn("1.0 MiB/s", display_val)
        self.assertIn("256.0 KiB/s", display_val)

    def test_aggregate_speeds(self):
        """Model calculates total aggregate download and upload speeds."""
        e1 = DownloadEntry(id="1", status="downloading", speed=1000.0, upload_speed=100.0)
        e2 = DownloadEntry(id="2", status="downloading", speed=2000.0, upload_speed=200.0)
        e3 = DownloadEntry(id="3", status="seeding", speed=0.0, upload_speed=300.0)
        e4 = DownloadEntry(id="4", status="paused", speed=500.0, upload_speed=500.0)

        self.model.load_entries([e1, e2, e3, e4])
        down, up = self.model.get_aggregate_speeds()
        self.assertEqual(down, 3000.0)
        self.assertEqual(up, 600.0)

    def test_name_column_icon_decoration(self):
        """Name column returns appropriate QIcon for DecorationRole."""
        entry_http = DownloadEntry(id="h1", filename="video.mp4", download_type="http", status="downloading")
        entry_torrent = DownloadEntry(id="t1", filename="linux.iso", download_type="torrent", status="downloading")
        self.model.load_entries([entry_http, entry_torrent])

        icon_http = self.model.data(self.model.index(0, Col.NAME), Qt.ItemDataRole.DecorationRole)
        icon_torrent = self.model.data(self.model.index(1, Col.NAME), Qt.ItemDataRole.DecorationRole)

        self.assertIsNotNone(icon_http)
        self.assertIsNotNone(icon_torrent)

    def test_continuous_queue_numbers_without_gaps(self):
        """Inactive items (completed, paused, error) show no order, active items are continuous 1, 2, 3."""
        items = [
            DownloadEntry(id="1", filename="a.zip", status="downloading"),
            DownloadEntry(id="2", filename="b.zip", status="completed"),
            DownloadEntry(id="3", filename="c.zip", status="paused"),
            DownloadEntry(id="4", filename="d.zip", status="queued"),
            DownloadEntry(id="5", filename="e.zip", status="error"),
            DownloadEntry(id="6", filename="f.zip", status="downloading"),
        ]
        self.model.load_entries(items)
        # Row 0: downloading -> "1"
        self.assertEqual(self.model.data(self.model.index(0, Col.QUEUE), Qt.ItemDataRole.DisplayRole), "1")
        # Row 1: completed -> ""
        self.assertEqual(self.model.data(self.model.index(1, Col.QUEUE), Qt.ItemDataRole.DisplayRole), "")
        # Row 2: paused -> ""
        self.assertEqual(self.model.data(self.model.index(2, Col.QUEUE), Qt.ItemDataRole.DisplayRole), "")
        # Row 3: queued -> "2" (continuous, not 4!)
        self.assertEqual(self.model.data(self.model.index(3, Col.QUEUE), Qt.ItemDataRole.DisplayRole), "2")
        # Row 4: error -> ""
        self.assertEqual(self.model.data(self.model.index(4, Col.QUEUE), Qt.ItemDataRole.DisplayRole), "")
        # Row 5: downloading -> "3" (continuous, not 6!)
        self.assertEqual(self.model.data(self.model.index(5, Col.QUEUE), Qt.ItemDataRole.DisplayRole), "3")

    def test_save_path_normalized_forward_slashes(self):
        """Save path column and data entries are unified with forward slashes."""
        entry = DownloadEntry(id="p1", filename="test.zip", save_path="C:\\Users\\rakes\\Downloads")
        self.model.load_entries([entry])
        idx = self.model.index(0, Col.SAVE_PATH)
        display_path = self.model.data(idx, Qt.ItemDataRole.DisplayRole)
        self.assertNotIn("\\", display_path)
        self.assertIn("/", display_path)

    def test_delegate_progress_text_and_status_colors(self):
        """ProgressBarDelegate supports new lifecycle states and color definitions."""
        self.assertIn("fetching_metadata", ProgressBarDelegate._STATUS_COLORS)
        self.assertIn("file_not_found", ProgressBarDelegate._STATUS_COLORS)
        self.assertIn("stalled", ProgressBarDelegate._STATUS_COLORS)

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
        self.assertEqual(self.model.get_entry(0).id, "1")
        self.assertEqual(self.model.get_entry(1).id, "3")
        self.assertEqual(self.model.get_entry(2).id, "2")

        self.model.sort(Col.ETA, Qt.SortOrder.DescendingOrder)
        self.assertEqual(self.model.get_entry(0).id, "3")
        self.assertEqual(self.model.get_entry(1).id, "1")
        self.assertEqual(self.model.get_entry(2).id, "2")

    def test_sort_by_completed(self):
        """Completed downloads appear before uncompleted ones."""
        e4 = _make_entry("4", "delta.tar", completed_at="2026-02-01T10:00:00Z")
        self.model.load_entries([self.e1, self.e2, self.e3, e4])

        self.model.sort(Col.COMPLETED, Qt.SortOrder.AscendingOrder)
        self.assertEqual(self.model.get_entry(0).id, "2")
        self.assertEqual(self.model.get_entry(1).id, "4")

        self.model.sort(Col.COMPLETED, Qt.SortOrder.DescendingOrder)
        self.assertEqual(self.model.get_entry(0).id, "4")
        self.assertEqual(self.model.get_entry(1).id, "2")

    def test_add_entry_inserts_into_correct_sorted_row(self):
        """Adding an entry inserts it at the proper position under current sort."""
        self.model.load_entries([self.e1, self.e2])
        newest = _make_entry("new", "new.zip", added_at="2026-07-01T10:00:00Z")
        self.model.add_entry(newest)
        self.assertEqual(self.model.get_entry(0).id, "new")
        self.assertEqual(self.model.get_entry(1).id, "1")
        self.assertEqual(self.model.get_entry(2).id, "2")

    def test_selection_tracking_across_sort(self):
        """Persistent indexes and selection stay anchored to the item when sorted."""
        self.model.load_entries([self.e1, self.e2, self.e3])
        view = QTableView()
        view.setModel(self.model)
        view.setSortingEnabled(True)

        self.assertEqual(self.model.get_entry(0).id, "3")
        view.selectRow(0)

        selected_ids = self.model.get_selected_ids(view.selectionModel().selectedIndexes())
        self.assertEqual(selected_ids, ["3"])

        view.sortByColumn(Col.NAME, Qt.SortOrder.AscendingOrder)
        self.assertEqual(self.model.get_entry(2).id, "3")

        selected_ids_after = self.model.get_selected_ids(view.selectionModel().selectedIndexes())
        self.assertEqual(selected_ids_after, ["3"])


class TestMainWindowSortingIntegration(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.manager = DownloadManager(self.db)
        self.win = MainWindow(self.manager)

    def tearDown(self):
        self.win.close()
        self.manager.stop()
        self.db.close()

    def test_main_window_has_sorting_enabled(self):
        """MainWindow table has sorting enabled with Added DESC default."""
        self.assertTrue(self.win._table.isSortingEnabled())
        header = self.win._table.horizontalHeader()
        self.assertEqual(header.sortIndicatorSection(), Col.ADDED)
        self.assertEqual(header.sortIndicatorOrder(), Qt.SortOrder.DescendingOrder)

    def test_main_window_sort_helpers(self):
        """MainWindow sort helpers correctly update the table sort state."""
        self.win._sort_by_column(Col.NAME)
        header = self.win._table.horizontalHeader()
        self.assertEqual(header.sortIndicatorSection(), Col.NAME)

        self.win._set_sort_order(Qt.SortOrder.AscendingOrder)
        self.assertEqual(header.sortIndicatorOrder(), Qt.SortOrder.AscendingOrder)

        # Switching back to Added column defaults to Descending order
        self.win._sort_by_column(Col.ADDED)
        self.assertEqual(header.sortIndicatorSection(), Col.ADDED)
        self.assertEqual(header.sortIndicatorOrder(), Qt.SortOrder.DescendingOrder)

    def test_main_window_header_section_clicked_added_defaults_descending(self):
        """Clicking Date Added column header switches to it in descending order."""
        self.win._table.sortByColumn(Col.NAME, Qt.SortOrder.AscendingOrder)
        self.win._last_sort_section = Col.NAME
        self.assertEqual(self.win._table.horizontalHeader().sortIndicatorSection(), Col.NAME)

        self.win._on_header_section_clicked(Col.ADDED)
        self.assertEqual(self.win._table.horizontalHeader().sortIndicatorSection(), Col.ADDED)
        self.assertEqual(self.win._table.horizontalHeader().sortIndicatorOrder(), Qt.SortOrder.DescendingOrder)


if __name__ == "__main__":
    unittest.main()
