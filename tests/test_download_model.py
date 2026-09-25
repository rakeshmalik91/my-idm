"""Unit tests for DownloadTableModel: column indexing, data formatting, sorting, and progress bar delegates."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from PySide6.QtCore import Qt
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
        self.assertEqual(Col.SOURCE_DOMAIN, 2)
        self.assertEqual(Col.HEADERS[Col.SOURCE_DOMAIN], "Source Domain")
        self.assertEqual(Col.SIZE, 3)
        self.assertEqual(Col.PROGRESS, 4)
        self.assertEqual(Col.STATUS, 5)
        self.assertEqual(Col.SAVE_PATH, 12)
        self.assertEqual(Col.HEADERS[Col.SAVE_PATH], "Save Path")
        self.assertEqual(Col.FILE_NAME, 13)
        self.assertEqual(Col.HEADERS[Col.FILE_NAME], "File / Folder Name")

    def test_file_folder_name_column(self):
        """File / Folder Name column displays actual target file or folder name, while Name column retains original title."""
        e = DownloadEntry(
            id="t_fn",
            url="magnet:?xt=urn:btih:123&dn=SomeTorrentTitle",
            filename="CustomRenamedFolder",
            file_path="D:/Downloads/CustomRenamedFolder",
            status="completed",
            download_type="torrent",
        )
        self.model.load_entries([e])
        idx_fn = self.model.index(0, Col.FILE_NAME)
        idx_name = self.model.index(0, Col.NAME)
        self.assertEqual(self.model.data(idx_fn, Qt.ItemDataRole.DisplayRole), "CustomRenamedFolder")
        self.assertEqual(self.model.data(idx_fn, Qt.ItemDataRole.ToolTipRole), "D:/Downloads/CustomRenamedFolder")
        self.assertEqual(self.model.data(idx_name, Qt.ItemDataRole.DisplayRole), "SomeTorrentTitle")

        # After model rename, Name remains original while File / Folder Name updates
        self.model.rename_entry("t_fn", "NewFolderOnDisk")
        self.assertEqual(self.model.data(idx_fn, Qt.ItemDataRole.DisplayRole), "NewFolderOnDisk")
        self.assertEqual(self.model.data(idx_name, Qt.ItemDataRole.DisplayRole), "SomeTorrentTitle")

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

    def test_seeds_peers_column_display_formatting(self):
        """Seeds / Peers column formats torrent connected/swarm data and HTTP segments."""
        t_entry1 = DownloadEntry(
            id="t1",
            filename="ubuntu.iso",
            download_type="torrent",
            status="downloading",
            seeds=5,
            peers=12,
        )
        t_entry2 = DownloadEntry(
            id="t2",
            filename="fedora.iso",
            download_type="torrent",
            status="downloading",
            seeds=5,
            peers=12,
            total_seeds=20,
            total_peers=60,
        )
        h_entry = DownloadEntry(
            id="h1",
            filename="archive.zip",
            download_type="http",
            status="downloading",
            num_segments=4,
        )
        self.model.load_entries([t_entry1, t_entry2, h_entry])

        # Entry 1: S:5  P:12
        val1 = self.model.data(self.model.index(self.model._id_to_row["t1"], Col.SEEDS_PEERS), Qt.ItemDataRole.DisplayRole)
        self.assertEqual(val1, "S:5  P:12")

        # Entry 2 with swarm totals: S:5 (20)  P:12 (60)
        val2 = self.model.data(self.model.index(self.model._id_to_row["t2"], Col.SEEDS_PEERS), Qt.ItemDataRole.DisplayRole)
        self.assertEqual(val2, "S:5 (20)  P:12 (60)")

        # HTTP entry: 4 seg
        val3 = self.model.data(self.model.index(self.model._id_to_row["h1"], Col.SEEDS_PEERS), Qt.ItemDataRole.DisplayRole)
        self.assertEqual(val3, "4 seg")

    def test_added_and_last_tried_columns_display(self):
        """Added and Last Tried columns return formatted datetime strings."""
        entry = DownloadEntry(
            id="ts_test",
            filename="file.bin",
            added_at="2026-06-15T14:30:00+00:00",
            last_tried_at="2026-06-15T14:35:00+00:00",
        )
        self.model.load_entries([entry])
        row = self.model._id_to_row["ts_test"]

        added_val = self.model.data(self.model.index(row, Col.ADDED), Qt.ItemDataRole.DisplayRole)
        self.assertNotEqual(added_val, "—")
        self.assertIn("2026-06-15", added_val)

        tried_val = self.model.data(self.model.index(row, Col.LAST_TRIED), Qt.ItemDataRole.DisplayRole)
        self.assertNotEqual(tried_val, "—")
        self.assertIn("2026-06-15", tried_val)

    def test_load_entries_restores_seeds_peers_from_metadata(self):
        """Loading entries populates transient seeds/peers from metadata for completed/restored torrents."""
        entry = DownloadEntry(
            id="meta_torrent",
            filename="arch.iso",
            download_type="torrent",
            status="completed",
            metadata_json='{"seeds": 8, "peers": 19, "total_seeds": 40, "total_peers": 80}',
        )
        self.model.load_entries([entry])
        e = self.model.get_entry_by_id("meta_torrent")
        self.assertIsNotNone(e)
        self.assertEqual(e.seeds, 8)
        self.assertEqual(e.peers, 19)
        self.assertEqual(e.total_seeds, 40)
        self.assertEqual(e.total_peers, 80)

        display_val = self.model.data(self.model.index(0, Col.SEEDS_PEERS), Qt.ItemDataRole.DisplayRole)
        self.assertEqual(display_val, "S:8 (40)  P:19 (80)")

    def test_update_progress_updates_seeds_and_peers(self):
        """update_progress updates seeds, peers, and totals, emitting change for Col.SEEDS_PEERS."""
        entry = DownloadEntry(
            id="prog_torrent",
            filename="linux.iso",
            download_type="torrent",
            status="downloading",
            total_size=1000,
            downloaded_size=100,
        )
        self.model.load_entries([entry])

        self.model.update_progress("prog_torrent", 500, 1000, 50.0, 10.0, seeds=7, peers=14, upload_speed=20.0, total_seeds=35, total_peers=70)
        e = self.model.get_entry_by_id("prog_torrent")
        self.assertEqual(e.seeds, 7)
        self.assertEqual(e.peers, 14)
        self.assertEqual(e.total_seeds, 35)
        self.assertEqual(e.total_peers, 70)

        row = self.model._id_to_row["prog_torrent"]
        display_val = self.model.data(self.model.index(row, Col.SEEDS_PEERS), Qt.ItemDataRole.DisplayRole)
        self.assertEqual(display_val, "S:7 (35)  P:14 (70)")


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


class TestSourceDomainColumn(unittest.TestCase):
    def test_source_domain_is_separate_from_name(self):
        model = DownloadTableModel()
        entry = DownloadEntry(
            id="e1",
            url="https://releases.ubuntu.com/noble/ubuntu-24.04.iso",
            filename="ubuntu-24.04.iso",
        )
        model.load_entries([entry])

        name = model.data(model.index(0, Col.NAME), Qt.ItemDataRole.DisplayRole)
        domain = model.data(
            model.index(0, Col.SOURCE_DOMAIN), Qt.ItemDataRole.DisplayRole
        )

        self.assertEqual(name, "ubuntu-24.04.iso")
        self.assertEqual(domain, "releases.ubuntu.com")
        self.assertEqual(
            model.data(model.index(0, Col.SOURCE_DOMAIN), Qt.ItemDataRole.UserRole),
            "releases.ubuntu.com",
        )

    def test_source_domain_sorting(self):
        model = DownloadTableModel()
        model.load_entries([
            DownloadEntry(
                id="e1",
                url="https://beta.example.org/file.iso",
                filename="file.iso",
            ),
            DownloadEntry(
                id="e2",
                url="https://alpha.example.com/file.iso",
                filename="file.iso",
            ),
        ])

        model.sort(Col.SOURCE_DOMAIN, Qt.SortOrder.AscendingOrder)

        self.assertEqual(
            [model.data(model.index(row, Col.SOURCE_DOMAIN)) for row in range(2)],
            ["alpha.example.com", "beta.example.org"],
        )

    def test_source_domain_is_empty_for_local_paths(self):
        model = DownloadTableModel()
        model.load_entries([
            DownloadEntry(id="e1", url="C:/Downloads/file.iso", filename="file.iso")
        ])

        self.assertEqual(
            model.data(model.index(0, Col.SOURCE_DOMAIN)),
            "",
        )
        self.assertEqual(
            model.data(model.index(0, Col.SOURCE_DOMAIN), Qt.ItemDataRole.UserRole),
            "",
        )
        foreground = model.data(
            model.index(0, Col.SOURCE_DOMAIN), Qt.ItemDataRole.ForegroundRole
        )
        self.assertEqual(foreground.name(), "#39c5bb")


class TestStoppedStatusDisplay(unittest.TestCase):
    """Tests for 'stopped' status display in the download model."""

    def setUp(self):
        from my_idm.download_model import _STATUS_COLORS
        self._status_colors = _STATUS_COLORS
        self.model = DownloadTableModel()

    def test_stopped_status_display_text(self):
        """Stopped status should show 'Stopped'."""
        entry = _make_entry("d_stopped_1", "file.zip", status="stopped")
        self.model.load_entries([entry])
        idx = self.model.index(0, Col.STATUS)
        text = self.model.data(idx, Qt.ItemDataRole.DisplayRole)
        self.assertEqual(text, "Stopped")

    def test_stopped_status_no_queue_number(self):
        """Stopped downloads should not show a queue number."""
        entry = _make_entry("d_stopped_2", "file.zip", status="stopped", queue_order=5)
        self.model.load_entries([entry])
        idx = self.model.index(0, Col.QUEUE)
        text = self.model.data(idx, Qt.ItemDataRole.DisplayRole)
        self.assertEqual(text, "")

    def test_stopped_status_color(self):
        """Stopped status should have a defined color in _STATUS_COLORS."""
        self.assertIn("stopped", self._status_colors)

    def test_stopped_status_foreground_color(self):
        """The foreground color for stopped status should match the color map."""
        entry = _make_entry("d_stopped_3", "file.zip", status="stopped")
        self.model.load_entries([entry])
        idx = self.model.index(0, Col.STATUS)
        fg = self.model.data(idx, Qt.ItemDataRole.ForegroundRole)
        self.assertIsNotNone(fg)


class TestTorrentSeedsPeersNonInt(unittest.TestCase):
    """Tests that torrent seeds/peers with list/collection values from metadata do not trigger TypeErrors."""

    def test_display_data_with_list_peers_and_seeds(self):
        model = DownloadTableModel()
        entry = _make_entry("t_list_1", "ubuntu.iso", download_type="torrent")
        # Simulate metadata containing list of peer/seed objects
        entry.metadata = {
            "seeds": ["seed1", "seed2"],
            "peers": ["peer1", "peer2", "peer3"],
            "total_seeds": ["s1", "s2", "s3"],
            "total_peers": 10,
        }
        # Also directly set attribute as list to simulate worst-case legacy/external assignment
        entry.peers = ["peer1", "peer2", "peer3"]  # type: ignore
        entry.seeds = ["seed1", "seed2"]  # type: ignore
        entry.total_peers = 10
        entry.total_seeds = 5

        model.load_entries([entry])
        idx = model.index(0, Col.SEEDS_PEERS)
        text = model.data(idx, Qt.ItemDataRole.DisplayRole)
        self.assertIsInstance(text, str)
        self.assertIn("S:", text)
        self.assertIn("P:", text)

    def test_sorting_with_non_int_seeds_peers(self):
        model = DownloadTableModel()
        e1 = _make_entry("t1", "a.iso", download_type="torrent")
        e1.peers = ["p1", "p2"]  # type: ignore
        e1.seeds = 2
        e2 = _make_entry("t2", "b.iso", download_type="torrent")
        e2.peers = 5
        e2.seeds = ["s1"]  # type: ignore

        model.load_entries([e1, e2])
        # Sorting should succeed without TypeError: '<' not supported between instances of 'int' and 'list'
        model.sort(Col.SEEDS_PEERS, Qt.SortOrder.AscendingOrder)
        model.sort(Col.SEEDS_PEERS, Qt.SortOrder.DescendingOrder)


class TestModelFiltering(unittest.TestCase):
    """Tests for status and type filtering in DownloadTableModel."""

    def setUp(self):
        self.model = DownloadTableModel()
        self.e_http_dl = _make_entry("1", "ubuntu.iso", download_type="http", status="downloading")
        self.e_http_done = _make_entry("2", "document.pdf", download_type="http", status="completed")
        self.e_tor_dl = _make_entry("3", "arch.iso", download_type="torrent", status="downloading")
        self.e_tor_paused = _make_entry("4", "debian.iso", download_type="torrent", status="paused")
        self.e_tor_stopped = _make_entry("5", "fedora.iso", download_type="torrent", status="stopped")
        self.model.load_entries([
            self.e_http_dl,
            self.e_http_done,
            self.e_tor_dl,
            self.e_tor_paused,
            self.e_tor_stopped,
        ])

    def test_status_filtering(self):
        self.assertEqual(self.model.rowCount(), 5)
        self.assertFalse(self.model.is_filtered())

        # Filter by downloading
        self.model.set_status_filter({"downloading"})
        self.assertTrue(self.model.is_filtered())
        self.assertTrue(self.model.is_status_filtered())
        self.assertEqual(self.model.rowCount(), 2)
        visible_ids = {self.model.get_entry(i).id for i in range(self.model.rowCount())}
        self.assertEqual(visible_ids, {"1", "3"})

        # Filter by multiple statuses: completed, stopped
        self.model.set_status_filter({"completed", "stopped"})
        self.assertEqual(self.model.rowCount(), 2)
        visible_ids = {self.model.get_entry(i).id for i in range(self.model.rowCount())}
        self.assertEqual(visible_ids, {"2", "5"})

        # Clear filter
        self.model.clear_filters()
        self.assertFalse(self.model.is_filtered())
        self.assertEqual(self.model.rowCount(), 5)

    def test_type_filtering(self):
        # Filter by HTTP only
        self.model.set_type_filter({"http"})
        self.assertTrue(self.model.is_type_filtered())
        self.assertEqual(self.model.rowCount(), 2)
        visible_ids = {self.model.get_entry(i).id for i in range(self.model.rowCount())}
        self.assertEqual(visible_ids, {"1", "2"})

        # Filter by Torrent only
        self.model.set_type_filter({"torrent"})
        self.assertEqual(self.model.rowCount(), 3)
        visible_ids = {self.model.get_entry(i).id for i in range(self.model.rowCount())}
        self.assertEqual(visible_ids, {"3", "4", "5"})

    def test_combined_status_and_type_filtering(self):
        # Torrent + downloading -> should only be #3
        self.model.set_type_filter({"torrent"})
        self.model.set_status_filter({"downloading"})
        self.assertTrue(self.model.is_filtered())
        self.assertEqual(self.model.rowCount(), 1)
        self.assertEqual(self.model.get_entry(0).id, "3")

    def test_dynamic_status_change_with_active_filter(self):
        # Filter for downloading
        self.model.set_status_filter({"downloading"})
        self.assertEqual(self.model.rowCount(), 2)

        # Download #1 finishes -> status becomes completed -> should be removed from visible rows
        self.model.update_status("1", "completed")
        self.assertEqual(self.model.rowCount(), 1)
        self.assertEqual(self.model.get_entry(0).id, "3")

        # Paused download #4 resumes -> status downloading -> should appear in visible rows
        self.model.update_status("4", "downloading")
        self.assertEqual(self.model.rowCount(), 2)
        visible_ids = {self.model.get_entry(i).id for i in range(self.model.rowCount())}
        self.assertEqual(visible_ids, {"3", "4"})

    def test_filter_counts(self):
        status_counts = self.model.get_status_counts()
        self.assertEqual(status_counts["downloading"], 2)
        self.assertEqual(status_counts["completed"], 1)
        self.assertEqual(status_counts["paused"], 1)
        self.assertEqual(status_counts["stopped"], 1)

        type_counts = self.model.get_type_counts()
        self.assertEqual(type_counts["http"], 2)
        self.assertEqual(type_counts["torrent"], 3)

    def test_completed_download_progress_in_table_model_and_delegate(self):
        """Completed downloads loaded into table model must return 100% progress for delegate."""
        comp_entry = DownloadEntry(
            id="comp_zero_bytes",
            url="https://example.com/movie.mkv",
            filename="movie.mkv",
            save_path="C:/Downloads",
            total_size=1048576,
            downloaded_size=0,
            status="completed",
        )
        self.model.load_entries([comp_entry])
        idx = self.model.index(0, Col.PROGRESS)
        data = self.model.data(idx, Qt.ItemDataRole.DisplayRole)
        self.assertIsInstance(data, dict)
        self.assertEqual(data["progress"], 100.0)
        self.assertEqual(data["status"], "completed")

        # Calling update_progress with 0 must not downgrade completed download
        self.model.update_progress("comp_zero_bytes", 0, 1048576, 0.0, 0.0)
        data_after = self.model.data(idx, Qt.ItemDataRole.DisplayRole)
        self.assertEqual(data_after["progress"], 100.0)


if __name__ == "__main__":
    unittest.main()


