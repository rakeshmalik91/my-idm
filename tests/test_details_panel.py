"""Unit tests for the bottom details panel and sub-tabs (Overview, Files, Peers, Trackers, Segments)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QTableWidget,
)

from my_idm.database import Database, DownloadEntry, SegmentEntry
from my_idm.details_panel import DetailsPanel
from my_idm.main_window import MainWindow
from my_idm.manager import DownloadManager

app = QApplication.instance() or QApplication([])


class TestDetailsPanel(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.manager = DownloadManager(self.db)
        self.win = MainWindow(self.manager)

    def tearDown(self):
        self.win.close()
        self.db.close()

    def test_details_panel_splitter_integration(self):
        """MainWindow central widget should be a vertical QSplitter with table and details panel."""
        splitter = self.win.centralWidget()
        self.assertIsInstance(splitter, QSplitter)
        self.assertEqual(splitter.orientation(), Qt.Orientation.Vertical)
        self.assertEqual(splitter.count(), 2)

        self.assertIs(splitter.widget(0), self.win._table)
        self.assertIs(splitter.widget(1), self.win._details_panel)
        self.assertIsInstance(self.win._details_panel, DetailsPanel)

    def test_details_panel_initial_empty_state(self):
        """When no item is selected, details panel shows empty placeholder."""
        panel = self.win._details_panel
        self.assertIn("Select a download", panel._lbl_title.text())
        self.assertEqual(panel._ov_status.text(), "—")
        self.assertEqual(panel._ov_size.text(), "—")
        self.assertEqual(panel._table_files.rowCount(), 0)
        self.assertEqual(panel._table_peers.rowCount(), 0)
        self.assertEqual(panel._table_trackers.rowCount(), 0)
        self.assertEqual(panel._table_segments.rowCount(), 0)

    def test_details_panel_http_download_inspection(self):
        """Details panel displays HTTP metadata and segment breakdown."""
        # Insert HTTP entry
        entry = DownloadEntry(
            id="test-http-1",
            url="https://example.com/archive.zip",
            filename="archive.zip",
            save_path="C:/Downloads",
            file_path="C:/Downloads/archive.zip",
            total_size=10485760,  # 10 MB
            downloaded_size=5242880,  # 5 MB
            status="downloading",
            download_type="http",
            num_segments=2,
            speed=1048576.0,  # 1 MB/s
            eta_seconds=5.0,
        )
        self.db.add_download(entry)
        self.win._model.add_entry(entry)

        # Add 2 segments
        seg1 = SegmentEntry(
            id="seg-1",
            download_id="test-http-1",
            index=0,
            start_byte=0,
            end_byte=5242879,
            downloaded_bytes=3000000,
            status="downloading",
        )
        seg2 = SegmentEntry(
            id="seg-2",
            download_id="test-http-1",
            index=1,
            start_byte=5242880,
            end_byte=10485759,
            downloaded_bytes=2242880,
            status="downloading",
        )
        self.db.add_segments([seg1, seg2])

        panel = self.win._details_panel
        panel.set_download_id("test-http-1")

        # Check header and overview
        self.assertEqual(panel._lbl_title.text(), "archive.zip")
        self.assertEqual(panel._lbl_badge.text(), "HTTP / Direct")
        self.assertIn("Downloading", panel._ov_status.text())
        self.assertIn("50.0%", panel._ov_size.text())

        # Check Files tab (Col 0: Download Checkbox, Col 1: #, Col 2: Name)
        self.assertEqual(panel._table_files.rowCount(), 1)
        self.assertEqual(panel._table_files.item(0, 2).text(), "archive.zip")
        chk = panel._table_files.cellWidget(0, 0).findChild(QCheckBox)
        self.assertIsNotNone(chk)
        self.assertTrue(chk.isChecked())
        self.assertFalse(chk.isEnabled())

        # Check Segments tab
        self.assertEqual(panel._table_segments.rowCount(), 2)
        self.assertEqual(panel._table_segments.item(0, 0).text(), "Segment #1")
        self.assertEqual(panel._table_segments.item(1, 0).text(), "Segment #2")

        # Peers tab should be hidden for HTTP downloads
        peers_tab_idx = panel._tabs.indexOf(panel._tab_peers)
        self.assertFalse(panel._tabs.isTabVisible(peers_tab_idx))

        # Trackers tab should be hidden for HTTP downloads
        trackers_tab_idx = panel._tabs.indexOf(panel._tab_trackers)
        self.assertFalse(panel._tabs.isTabVisible(trackers_tab_idx))

    def test_details_panel_torrent_download_inspection(self):
        """Details panel displays torrent files, peers, trackers, and handles priority changes."""
        entry = DownloadEntry(
            id="test-torrent-1",
            url="magnet:?xt=urn:btih:abcdef1234567890",
            filename="ubuntu-24.04.iso",
            save_path="C:/Torrents",
            file_path="C:/Torrents/ubuntu-24.04.iso",
            total_size=4000000000,
            downloaded_size=2000000000,
            status="downloading",
            download_type="torrent",
            speed=5242880.0,
            upload_speed=512000.0,
            seeds=15,
            peers=42,
            torrent_info_hash="abcdef1234567890",
        )
        self.db.add_download(entry)
        self.win._model.add_entry(entry)

        # Mock torrent engine responses
        mock_files = [
            {
                "index": 0,
                "path": "ubuntu-24.04/README.txt",
                "size": 1024,
                "downloaded": 1024,
                "progress": 1.0,
                "priority": 4,
                "priority_label": "Normal",
                "status": "completed",
            },
            {
                "index": 1,
                "path": "ubuntu-24.04/ubuntu-live.iso",
                "size": 3999998976,
                "downloaded": 1999998976,
                "progress": 0.5,
                "priority": 7,
                "priority_label": "High",
                "status": "downloading",
            },
        ]
        mock_peers = [
            {
                "ip": "192.168.1.50:6881",
                "client": b"qBittorrent/4.5.2",
                "progress": 0.95,
                "down_speed": 2048000.0,
                "up_speed": 102400.0,
                "flags": b"uH",
            }
        ]
        mock_trackers = [
            {
                "tier": 0,
                "url": "udp://tracker.opentrackr.org:1337/announce",
                "status": "Working",
                "send_stats": True,
            }
        ]

        self.manager._torrent.get_torrent_files = MagicMock(return_value=mock_files)
        self.manager._torrent.get_torrent_peers = MagicMock(return_value=mock_peers)
        self.manager._torrent.get_torrent_trackers = MagicMock(return_value=mock_trackers)
        self.manager._torrent.set_torrent_file_priority = MagicMock(return_value=True)

        panel = self.win._details_panel
        panel.set_download_id("test-torrent-1")

        # Verify Header & Overview
        self.assertEqual(panel._lbl_title.text(), "ubuntu-24.04.iso")
        self.assertEqual(panel._lbl_badge.text(), "BitTorrent")
        self.assertIn("15 seeds, 42 peers", panel._ov_swarm.text())
        self.assertEqual(panel._ov_hash.text(), "abcdef1234567890")

        # Verify Peers Tab is visible for torrents
        peers_tab_idx = panel._tabs.indexOf(panel._tab_peers)
        self.assertTrue(panel._tabs.isTabVisible(peers_tab_idx))

        # Verify Trackers Tab is visible for torrents
        trackers_tab_idx = panel._tabs.indexOf(panel._tab_trackers)
        self.assertTrue(panel._tabs.isTabVisible(trackers_tab_idx))

        # Verify Files Tab (Folder hierarchy in QTreeWidget)
        self.assertEqual(panel._tree_files.topLevelItemCount(), 1)
        root_folder = panel._tree_files.topLevelItem(0)
        self.assertEqual(root_folder.text(0), "📁 ubuntu-24.04")
        self.assertEqual(root_folder.childCount(), 2)

        file0 = root_folder.child(0)
        self.assertEqual(file0.text(0), "📄 README.txt")
        self.assertEqual(file0.checkState(0), Qt.CheckState.Checked)

        file1 = root_folder.child(1)
        self.assertEqual(file1.text(0), "📄 ubuntu-live.iso")
        self.assertEqual(file1.checkState(0), Qt.CheckState.Checked)

        # Check Priority Combo on file 1 (Col 3)
        combo_file1 = panel._tree_files.itemWidget(file1, 3)
        self.assertIsInstance(combo_file1, QComboBox)
        self.assertEqual(combo_file1.currentText(), "Max (100%)")

        # Change priority to Low (1) via combo
        combo_file1.setCurrentText("Low (25%)")
        self.manager._torrent.set_torrent_file_priority.assert_called_with("test-torrent-1", 1, 1)

        # Uncheck checkbox on file 1 -> priority becomes 0 (Don't Download) with prompt confirmed
        with unittest.mock.patch("my_idm.details_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            file1.setCheckState(0, Qt.CheckState.Unchecked)
            panel._on_tree_item_changed(file1, 0)
        self.manager._torrent.set_torrent_file_priority.assert_called_with("test-torrent-1", 1, 0)
        self.assertEqual(combo_file1.currentText(), "Don't Download")

        # Folder checkState becomes partially checked because file0 is checked and file1 is unchecked
        self.assertEqual(root_folder.checkState(0), Qt.CheckState.PartiallyChecked)

        # Uncheck root folder -> unchecks all child files and sets priorities to 0 with prompt confirmed
        with unittest.mock.patch("my_idm.details_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            root_folder.setCheckState(0, Qt.CheckState.Unchecked)
            panel._on_tree_item_changed(root_folder, 0)
        self.assertEqual(file0.checkState(0), Qt.CheckState.Unchecked)
        self.assertIn(unittest.mock.call("test-torrent-1", 0, 0), self.manager._torrent.set_torrent_file_priority.mock_calls)
        self.assertIn(unittest.mock.call("test-torrent-1", 1, 0), self.manager._torrent.set_torrent_file_priority.mock_calls)

        # Verify Peers Tab
        self.assertEqual(panel._table_peers.rowCount(), 1)
        self.assertEqual(panel._table_peers.item(0, 0).text(), "192.168.1.50:6881")
        self.assertEqual(panel._table_peers.item(0, 1).text(), "qBittorrent/4.5.2")

        # Verify Trackers Tab
        self.assertEqual(panel._table_trackers.rowCount(), 1)
        self.assertEqual(panel._table_trackers.item(0, 1).text(), "udp://tracker.opentrackr.org:1337/announce")

    def test_details_panel_files_tree_double_click_opens_file(self):
        """Double-clicking a file in the Files tree opens it via os.startfile."""
        entry = DownloadEntry(
            id="test-dbl-1",
            url="https://example.com/file.zip",
            filename="file.zip",
            save_path="C:/Downloads",
            file_path="C:/Downloads/file.zip",
            total_size=1048576,
            downloaded_size=1048576,
            status="completed",
            download_type="http",
        )
        self.db.add_download(entry)
        self.win._model.add_entry(entry)

        # Mock get_download_files to return file list
        mock_files = [
            {
                "index": 0,
                "path": "file.zip",
                "size": 1048576,
                "downloaded": 1048576,
                "progress": 1.0,
                "priority": 4,
                "status": "completed",
            },
        ]
        self.manager.get_download_files = MagicMock(return_value=mock_files)

        panel = self.win._details_panel
        panel.set_download_id("test-dbl-1")

        # Mock os.startfile and Path.exists
        with unittest.mock.patch("my_idm.details_panel.os.startfile") as mock_startfile, \
                unittest.mock.patch("my_idm.details_panel.Path.exists", return_value=True):
            file_item = panel._tree_files.topLevelItem(0)
            panel._on_tree_item_double_clicked(file_item)
            mock_startfile.assert_called_once_with(str(Path("C:/Downloads") / "file.zip"))

    def test_details_panel_files_tree_double_click_folder_ignored(self):
        """Double-clicking a folder in the Files tree does nothing."""
        entry = DownloadEntry(
            id="test-dbl-folder-1",
            url="https://example.com/archive.zip",
            filename="archive.zip",
            save_path="C:/Downloads",
            file_path="C:/Downloads/archive.zip",
            total_size=1048576,
            downloaded_size=1048576,
            status="completed",
            download_type="http",
        )
        self.db.add_download(entry)
        self.win._model.add_entry(entry)

        mock_files = [
            {
                "index": 0,
                "path": "folder/sub/file.txt",
                "size": 100,
                "downloaded": 100,
                "progress": 1.0,
                "priority": 4,
                "status": "completed",
            },
        ]
        self.manager.get_download_files = MagicMock(return_value=mock_files)

        panel = self.win._details_panel
        panel.set_download_id("test-dbl-folder-1")

        with unittest.mock.patch("my_idm.details_panel.os.startfile") as mock_startfile:
            folder_item = panel._tree_files.topLevelItem(0)
            panel._on_tree_item_double_clicked(folder_item)
            mock_startfile.assert_not_called()

    def test_toggle_details_panel_visibility(self):
        """Toggle action F4 and close button manage panel visibility."""
        panel = self.win._details_panel
        toggle_act = self.win._act_toggle_details

        toggle_act.setChecked(True)
        self.assertFalse(panel.isHidden())
        self.assertTrue(toggle_act.isChecked())

        # Toggle off
        toggle_act.setChecked(False)
        self.assertTrue(panel.isHidden())

        # Toggle on
        toggle_act.setChecked(True)
        self.assertFalse(panel.isHidden())

        # Close button has valid text, objectName, and emits close_requested
        self.assertEqual(panel._btn_close.text(), "✕")
        self.assertEqual(panel._btn_close.objectName(), "detailsCloseBtn")
        panel.close_requested.emit()
        self.assertFalse(toggle_act.isChecked())
        self.assertTrue(panel.isHidden())

    def test_live_segments_and_metadata_persistence(self):
        """Live segments are fetched from HTTPEngine during download, and torrent tab metadata persists offline."""
        # 1. Live segments
        http_entry = DownloadEntry(
            id="live-http-1",
            url="http://example.com/test.bin",
            status="downloading",
            total_size=1000000,
        )
        self.db.add_download(http_entry)
        live_seg = SegmentEntry(
            download_id="live-http-1",
            index=0,
            start_byte=0,
            end_byte=999999,
            downloaded_bytes=450000,
            status="downloading",
        )
        self.manager._http.get_live_segments = MagicMock(return_value=[live_seg])
        segments = self.manager.get_download_segments("live-http-1")
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].downloaded_bytes, 450000)

        # 2. Tab metadata persistence across restart
        tor_entry = DownloadEntry(
            id="persisted-tor-1",
            url="magnet:?xt=urn:btih:1111222233334444",
            filename="persisted.iso",
            download_type="torrent",
            status="completed",
        )
        self.db.add_download(tor_entry)

        cached_files = [{"index": 0, "path": "persisted.iso", "size": 5000, "priority": 4, "progress": 1.0, "status": "completed"}]
        cached_trackers = [{"url": "http://tracker.example.com", "tier": 0, "status": "Working", "send_stats": True}]
        cached_peers = [{"ip": "1.2.3.4:5678", "client": "qBittorrent", "progress": 1.0, "down_speed": 0, "up_speed": 0, "flags": ""}]

        self.manager._torrent.get_torrent_files = MagicMock(return_value=cached_files)
        self.manager._torrent.get_torrent_trackers = MagicMock(return_value=cached_trackers)
        self.manager._torrent.get_torrent_peers = MagicMock(return_value=cached_peers)

        # Query once to populate metadata cache in DB
        self.assertEqual(self.manager.get_download_files("persisted-tor-1"), cached_files)
        self.assertEqual(self.manager.get_torrent_trackers("persisted-tor-1"), cached_trackers)
        self.assertEqual(self.manager.get_torrent_peers("persisted-tor-1"), cached_peers)

        # Now simulate app restart / engine offline (torrent engine returns empty)
        self.manager._torrent.get_torrent_files = MagicMock(return_value=[])
        self.manager._torrent.get_torrent_trackers = MagicMock(return_value=[])
        self.manager._torrent.get_torrent_peers = MagicMock(return_value=[])

        # Manager should return cached records from DB metadata
        self.assertEqual(self.manager.get_download_files("persisted-tor-1"), cached_files)
        self.assertEqual(self.manager.get_torrent_trackers("persisted-tor-1"), cached_trackers)
        self.assertEqual(self.manager.get_torrent_peers("persisted-tor-1"), cached_peers)

    def test_completed_torrent_click_does_not_fetch_metadata(self):
        """Clicking on a completed torrent leaves status as completed and does not trigger metadata fetching."""
        tor_entry = DownloadEntry(
            id="completed-tor-click",
            url="magnet:?xt=urn:btih:aabbccddeeff00112233445566778899aabbccdd&dn=CompletedMedia",
            filename="CompletedMedia",
            download_type="torrent",
            status="completed",
            total_size=1024 * 1024,
            downloaded_size=1024 * 1024,
            metadata_json='{"files": [{"index": 0, "path": "CompletedMedia/video.mkv", "size": 1048576, "priority": 4, "progress": 1.0, "status": "completed"}]}',
        )
        self.db.add_download(tor_entry)
        self.win._load_history()

        # Select row in table view (triggers _on_table_selection_changed -> set_download_id -> _build_files_tree)
        self.win._table.selectRow(0)

        # Status must stay completed
        db_entry = self.db.get_download("completed-tor-click")
        self.assertEqual(db_entry.status, "completed")
        model_entry = self.win._model.get_entry(0)
        self.assertEqual(model_entry.status, "completed")
        self.assertNotEqual(model_entry.status, "fetching_metadata")

    def test_details_panel_live_progress_updates_overview_and_swarm(self):
        """When progress is updated, DetailsPanel updates live overview speeds, seeds, and peers."""
        tor_entry = DownloadEntry(
            id="live-tor-1",
            url="magnet:?xt=urn:btih:1111222233334444555566667777888899990000",
            filename="LiveTorrent",
            download_type="torrent",
            status="downloading",
            total_size=10000,
            downloaded_size=1000,
        )
        self.db.add_download(tor_entry)
        self.win._model.add_entry(tor_entry)

        panel = self.win._details_panel
        panel.setVisible(True)
        panel.set_download_id("live-tor-1")

        # Simulate live progress update signal from manager
        self.win._on_progress_updated(
            "live-tor-1", 5000, 10000, 1048576.0, 5.0, seeds=9, peers=25, upload_speed=262144.0
        )

        # Overview should now display updated speeds, seeds and peers
        self.assertIn("9 seeds, 25 peers connected", panel._ov_swarm.text())
        self.assertIn("1.0 MiB/s", panel._ov_speed.text())
        self.assertIn("256.0 KiB/s", panel._ov_speed.text())

    def test_details_panel_swarm_totals_display(self):
        """Overview swarm and peers status labels reflect swarm totals when available."""
        tor_entry = DownloadEntry(
            id="swarm-tor-1",
            url="magnet:?xt=urn:btih:9999888877776666555544443333222211110000",
            filename="SwarmTorrent",
            download_type="torrent",
            status="downloading",
            total_size=10000,
            downloaded_size=2000,
            seeds=5,
            peers=12,
            total_seeds=45,
            total_peers=110,
        )
        self.db.add_download(tor_entry)
        self.win._model.add_entry(tor_entry)

        panel = self.win._details_panel
        panel.set_download_id("swarm-tor-1")

        self.assertIn("5 (45) seeds, 12 (110) peers connected", panel._ov_swarm.text())
        self.assertIn("45 seeds, 110 peers in swarm", panel._lbl_peers_status.text())

    def test_details_panel_restore_from_collapsed_or_vanished_state(self):
        """Details panel automatically recovers if saved state had it collapsed to 0 height."""
        self.assertFalse(self.win._splitter.childrenCollapsible())
        self.assertGreaterEqual(self.win._details_panel.minimumHeight(), 120)

        # Simulate the vanished state (collapsed to 0 and marked hidden)
        self.manager.save_ui_state({
            "splitter_sizes": [903, 0],
            "details_visible": False,
        })

        win2 = MainWindow(self.manager)
        win2.show()
        try:
            # Must auto-heal to visible with healthy non-zero height
            self.assertFalse(win2._details_panel.isHidden())
            self.assertTrue(win2._details_panel.isVisible())
            self.assertTrue(win2._act_toggle_details.isChecked())
            sizes = win2._splitter.sizes()
            self.assertGreaterEqual(sizes[1], 140)
            self.assertEqual(win2._details_status_btn.text(), "📋 Details: ON")

            # Toggle off
            win2._act_toggle_details.setChecked(False)
            self.assertFalse(win2._details_panel.isVisible())
            self.assertEqual(win2._details_status_btn.text(), "📋 Details: OFF")

            # Toggle back on — must not restore to 0 height
            win2._act_toggle_details.setChecked(True)
            self.assertTrue(win2._details_panel.isVisible())
            sizes2 = win2._splitter.sizes()
            self.assertGreaterEqual(sizes2[1], 140)
            self.assertEqual(win2._details_status_btn.text(), "📋 Details: ON")
        finally:
            win2.close()

    def test_details_panel_refresh_with_integer_peers_metadata(self):
        """Regression test: metadata['peers'] storing an int count must not crash details panel refresh with TypeError."""
        tor_entry = DownloadEntry(
            id="tor-int-peers",
            url="magnet:?xt=urn:btih:2222333344445555666677778888999900001111",
            filename="integers.iso",
            download_type="torrent",
            status="downloading",
            metadata_json='{"seeds": 5, "peers": 12, "total_seeds": 20, "total_peers": 50}',
        )
        self.db.add_download(tor_entry)
        self.win._model.add_entry(tor_entry)

        # Torrent engine has 0 active peer dicts
        self.manager._torrent.get_torrent_peers = MagicMock(return_value=[])

        # Verify manager returns [] instead of the int 12
        peers = self.manager.get_torrent_peers("tor-int-peers")
        self.assertIsInstance(peers, list)
        self.assertEqual(peers, [])

        # Set download ID on panel and refresh
        panel = self.win._details_panel
        panel.set_download_id("tor-int-peers")
        panel.refresh()

        # Status should show 0 connected peer(s) without raising TypeError
        self.assertIn("0 connected peer(s)", panel._lbl_peers_status.text())
        self.assertIn("20 seeds, 50 peers in swarm", panel._lbl_peers_status.text())

    def test_details_panel_height_and_state_persistence_in_db(self):
        """Details panel height, active tab, and visibility are persisted to DB and restored on restart."""
        self.win.show()
        # Set custom details height (e.g. 320px) and active tab (Files tab = index 1)
        self.win._details_height = 320
        self.win._splitter.setSizes([480, 320])
        self.win._details_panel._tabs.setCurrentIndex(1)
        self.win._save_ui_state_to_db()

        # Check that DB contains details_height and details_state
        state = self.db.get_window_state()
        self.assertEqual(state.get("details_height"), 320)
        self.assertTrue(state.get("details_visible"))
        self.assertEqual(state.get("details_state", {}).get("current_tab"), 1)

        # Launch a second window to verify restoration
        win2 = MainWindow(self.manager)
        win2.show()
        try:
            self.assertEqual(win2._details_height, 320)
            self.assertTrue(win2._details_panel.isVisible())
            self.assertEqual(win2._details_panel._tabs.currentIndex(), 1)
            sizes2 = win2._splitter.sizes()
            self.assertEqual(sizes2[1], 320)

            # Now hide details panel in win2 and save state
            win2._act_toggle_details.setChecked(False)
            self.assertFalse(win2._details_panel.isVisible())
            self.assertEqual(win2._details_height, 320)
            win2._save_ui_state_to_db()

            state2 = self.db.get_window_state()
            self.assertFalse(state2.get("details_visible"))
            self.assertEqual(state2.get("details_height"), 320)

            # Launch a third window to verify it starts hidden with saved height intact
            win3 = MainWindow(self.manager)
            win3.show()
            try:
                self.assertFalse(win3._details_panel.isVisible())
                self.assertFalse(win3._act_toggle_details.isChecked())
                self.assertEqual(win3._details_height, 320)

                # When user toggles it back ON, it restores to exactly 320px on the Files tab
                win3._act_toggle_details.setChecked(True)
                self.assertTrue(win3._details_panel.isVisible())
                self.assertEqual(win3._splitter.sizes()[1], 320)
                self.assertEqual(win3._details_panel._tabs.currentIndex(), 1)
            finally:
                win3.close()
        finally:
            win2.close()


    def test_details_panel_displays_persisted_file_hierarchy_and_progress_when_offline(self):
        """Details panel displays full file hierarchy, progress details, trackers, and swarm metrics loaded from DB metadata."""
        tor_entry = DownloadEntry(
            id="persisted-hierarchy-test",
            url="magnet:?xt=urn:btih:ffff000011112222333344445555666677778888",
            filename="MultiFileProject",
            download_type="torrent",
            status="paused",
            total_size=100000,
            downloaded_size=60000,
            metadata_json='''{
                "seeds": 14,
                "peers": 38,
                "total_seeds": 45,
                "total_peers": 90,
                "files": [
                    {
                        "index": 0,
                        "path": "MultiFileProject/docs/manual.pdf",
                        "name": "manual.pdf",
                        "size": 20000,
                        "downloaded": 20000,
                        "progress": 100.0,
                        "priority": 4,
                        "priority_label": "Medium (50%)",
                        "status": "completed"
                    },
                    {
                        "index": 1,
                        "path": "MultiFileProject/src/main.py",
                        "name": "main.py",
                        "size": 50000,
                        "downloaded": 30000,
                        "progress": 60.0,
                        "priority": 7,
                        "priority_label": "Max (100%)",
                        "status": "downloading"
                    },
                    {
                        "index": 2,
                        "path": "MultiFileProject/assets/logo.png",
                        "name": "logo.png",
                        "size": 15000,
                        "downloaded": 0,
                        "progress": 0.0,
                        "priority": 4,
                        "priority_label": "Medium (50%)",
                        "status": "pending"
                    },
                    {
                        "index": 3,
                        "path": "MultiFileProject/temp.tmp",
                        "name": "temp.tmp",
                        "size": 15000,
                        "downloaded": 0,
                        "progress": 0.0,
                        "priority": 0,
                        "priority_label": "Don\'t Download",
                        "status": "skipped"
                    }
                ],
                "trackers": [
                    {
                        "tier": 0,
                        "url": "udp://tracker.openbittorrent.com:80/announce",
                        "status": "Working",
                        "seeds": 14,
                        "peers": 38,
                        "send_stats": true
                    },
                    {
                        "tier": 1,
                        "url": "http://tracker.backup.org/announce",
                        "status": "Updating",
                        "seeds": 0,
                        "peers": 0,
                        "send_stats": false
                    }
                ],
                "peer_list": [
                    {
                        "ip": "10.0.0.1:6881",
                        "client": "Transmission/4.0.0",
                        "progress": 0.85,
                        "down_speed": 102400.0,
                        "up_speed": 20480.0,
                        "flags": "D H"
                    }
                ]
            }'''
        )
        self.db.add_download(tor_entry)
        self.win._model.add_entry(tor_entry)

        # Ensure database reloads entry with seeds and peers populated from metadata
        loaded = self.db.get_download("persisted-hierarchy-test")
        self.assertEqual(loaded.seeds, 14)
        self.assertEqual(loaded.peers, 38)
        self.assertEqual(loaded.total_seeds, 45)
        self.assertEqual(loaded.total_peers, 90)

        # Set panel to this entry
        panel = self.win._details_panel
        panel.set_download_id("persisted-hierarchy-test")

        # Verify Overview swarm display
        self.assertIn("14 (45) seeds, 38 (90) peers connected", panel._ov_swarm.text())

        # Verify Files Tree hierarchy
        self.assertEqual(panel._tree_files.topLevelItemCount(), 1)
        root = panel._tree_files.topLevelItem(0)
        self.assertEqual(root.text(0), "📁 MultiFileProject")
        self.assertEqual(root.childCount(), 4)  # docs, src, assets, temp.tmp

        # Find items by file_index
        item_manual = panel._file_item_map[0]
        item_main = panel._file_item_map[1]
        item_logo = panel._file_item_map[2]
        item_temp = panel._file_item_map[3]

        self.assertEqual(item_manual.text(0), "📄 manual.pdf")
        self.assertEqual(item_manual.text(4), "Completed")
        self.assertEqual(item_manual.checkState(0), Qt.CheckState.Checked)

        self.assertEqual(item_main.text(0), "📄 main.py")
        self.assertIn("Paused", item_main.text(4))  # since entry is paused and downloaded > 0
        self.assertEqual(item_main.checkState(0), Qt.CheckState.Checked)

        self.assertEqual(item_logo.text(0), "📄 logo.png")
        self.assertEqual(item_logo.text(4), "Paused")
        self.assertEqual(item_logo.checkState(0), Qt.CheckState.Checked)

        self.assertEqual(item_temp.text(0), "📄 temp.tmp")
        self.assertEqual(item_temp.text(4), "Skipped")
        self.assertEqual(item_temp.checkState(0), Qt.CheckState.Unchecked)

        # Verify Trackers tab populated from metadata
        self.assertEqual(panel._table_trackers.rowCount(), 2)
        self.assertEqual(panel._table_trackers.item(0, 1).text(), "udp://tracker.openbittorrent.com:80/announce")
        self.assertEqual(panel._table_trackers.item(0, 2).text(), "Working")
        self.assertEqual(panel._table_trackers.item(1, 1).text(), "http://tracker.backup.org/announce")
        self.assertEqual(panel._table_trackers.item(1, 2).text(), "Updating")

        # Verify Peers tab populated from metadata
        self.assertEqual(panel._table_peers.rowCount(), 1)
        self.assertEqual(panel._table_peers.item(0, 0).text(), "10.0.0.1:6881")
        self.assertEqual(panel._table_peers.item(0, 1).text(), "Transmission/4.0.0")


    def test_uncheck_downloaded_file_cancels_when_prompt_rejected(self):
        """Unchecking a downloaded file prompts confirmation; clicking No reverts check state without trashing."""
        tor_entry = DownloadEntry(
            id="test-trash-prompt-1",
            url="magnet:?xt=urn:btih:3333444455556666777788889999000011112222",
            filename="TestTrashPrompt",
            download_type="torrent",
            status="downloading",
            save_path="C:/Downloads/TestTrashPrompt",
            metadata_json='''{
                "files": [
                    {
                        "index": 0,
                        "path": "TestTrashPrompt/doc.pdf",
                        "size": 1000,
                        "downloaded": 1000,
                        "progress": 100.0,
                        "priority": 4,
                        "priority_label": "Medium (50%)",
                        "status": "completed"
                    }
                ]
            }'''
        )
        self.db.add_download(tor_entry)
        self.win._model.add_entry(tor_entry)

        panel = self.win._details_panel
        panel.set_download_id("test-trash-prompt-1")
        item = panel._file_item_map[0]

        with unittest.mock.patch("my_idm.details_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.No) as mock_q, \
             unittest.mock.patch("my_idm.details_panel.send_to_trash") as mock_trash:
            item.setCheckState(0, Qt.CheckState.Unchecked)
            mock_q.assert_called_once()
            mock_trash.assert_not_called()
            # Must be reverted back to Checked
            self.assertEqual(item.checkState(0), Qt.CheckState.Checked)

    def test_uncheck_downloaded_file_trashes_when_prompt_accepted(self):
        """Unchecking a downloaded file and confirming moves the file to trash and sets priority to 0."""
        tor_entry = DownloadEntry(
            id="test-trash-prompt-2",
            url="magnet:?xt=urn:btih:4444555566667777888899990000111122223333",
            filename="TestTrashAccepted",
            download_type="torrent",
            status="downloading",
            save_path="C:/Downloads/TestTrashAccepted",
            metadata_json='''{
                "files": [
                    {
                        "index": 0,
                        "path": "TestTrashAccepted/data.bin",
                        "size": 5000,
                        "downloaded": 2500,
                        "progress": 50.0,
                        "priority": 4,
                        "priority_label": "Medium (50%)",
                        "status": "downloading"
                    }
                ]
            }'''
        )
        self.db.add_download(tor_entry)
        self.win._model.add_entry(tor_entry)

        panel = self.win._details_panel
        panel.set_download_id("test-trash-prompt-2")
        item = panel._file_item_map[0]

        with unittest.mock.patch("my_idm.details_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes) as mock_q, \
             unittest.mock.patch("my_idm.details_panel.send_to_trash") as mock_trash, \
             unittest.mock.patch("pathlib.Path.exists", return_value=True), \
             unittest.mock.patch.object(self.manager, "set_torrent_file_priority") as mock_set_prio:
            item.setCheckState(0, Qt.CheckState.Unchecked)
            mock_q.assert_called_once()
            mock_trash.assert_called_once()
            mock_set_prio.assert_called_with("test-trash-prompt-2", 0, 0)
            self.assertEqual(item.text(4), "Skipped")

    def test_combo_dont_download_prompts_and_trashes(self):
        """Setting priority combo to 'Don't Download' prompts user and trashes file if accepted."""
        tor_entry = DownloadEntry(
            id="test-trash-combo-1",
            url="magnet:?xt=urn:btih:5555666677778888999900001111222233334444",
            filename="TestTrashCombo",
            download_type="torrent",
            status="downloading",
            save_path="C:/Downloads/TestTrashCombo",
            metadata_json='''{
                "files": [
                    {
                        "index": 0,
                        "path": "TestTrashCombo/video.mp4",
                        "size": 10000,
                        "downloaded": 10000,
                        "progress": 100.0,
                        "priority": 4,
                        "priority_label": "Medium (50%)",
                        "status": "completed"
                    }
                ]
            }'''
        )
        self.db.add_download(tor_entry)
        self.win._model.add_entry(tor_entry)

        panel = self.win._details_panel
        panel.set_download_id("test-trash-combo-1")
        item = panel._file_item_map[0]
        combo = panel._tree_files.itemWidget(item, 3)

        # 1. User rejects prompt -> combo reverts
        with unittest.mock.patch("my_idm.details_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.No) as mock_q, \
             unittest.mock.patch("my_idm.details_panel.send_to_trash") as mock_trash:
            combo.setCurrentText("Don't Download")
            mock_q.assert_called_once()
            mock_trash.assert_not_called()
            self.assertNotEqual(combo.currentText(), "Don't Download")

        # 2. User accepts prompt -> file is trashed and priority set to 0
        with unittest.mock.patch("my_idm.details_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes) as mock_q2, \
             unittest.mock.patch("my_idm.details_panel.send_to_trash") as mock_trash2, \
             unittest.mock.patch("pathlib.Path.exists", return_value=True), \
             unittest.mock.patch.object(self.manager, "set_torrent_file_priority") as mock_set_prio:
            combo.setCurrentText("Don't Download")
            mock_q2.assert_called_once()
            mock_trash2.assert_called_once()
            mock_set_prio.assert_called_with("test-trash-combo-1", 0, 0)
            self.assertEqual(item.text(4), "Skipped")

    def test_multi_selection_dont_download_single_confirmation(self):
        """Setting multiple files to Don't Download via selection triggers exactly ONE prompt."""
        tor_entry = DownloadEntry(
            id="test-trash-multi-1",
            url="magnet:?xt=urn:btih:6666777788889999000011112222333344445555",
            filename="TestTrashMulti",
            download_type="torrent",
            status="downloading",
            save_path="C:/Downloads/TestTrashMulti",
            metadata_json='''{
                "files": [
                    {
                        "index": 0,
                        "path": "TestTrashMulti/file1.bin",
                        "size": 1000,
                        "downloaded": 1000,
                        "progress": 100.0,
                        "priority": 4,
                        "status": "completed"
                    },
                    {
                        "index": 1,
                        "path": "TestTrashMulti/file2.bin",
                        "size": 2000,
                        "downloaded": 2000,
                        "progress": 100.0,
                        "priority": 4,
                        "status": "completed"
                    }
                ]
            }'''
        )
        self.db.add_download(tor_entry)
        self.win._model.add_entry(tor_entry)

        panel = self.win._details_panel
        panel.set_download_id("test-trash-multi-1")
        item0 = panel._file_item_map[0]
        item1 = panel._file_item_map[1]

        with unittest.mock.patch("my_idm.details_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes) as mock_q, \
             unittest.mock.patch("my_idm.details_panel.send_to_trash") as mock_trash, \
             unittest.mock.patch("pathlib.Path.exists", return_value=True), \
             unittest.mock.patch.object(self.manager, "set_torrent_file_priority") as mock_set_prio:
            panel._set_items_priority([item0, item1], 0)
            mock_q.assert_called_once()
            self.assertEqual(mock_trash.call_count, 2)
            self.assertEqual(mock_set_prio.call_count, 2)

    def test_details_panel_overview_displays_filename(self):
        """DetailsPanel Overview tab displays filename in _ov_filename."""
        entry = DownloadEntry(
            id="test-fn-ov-1",
            url="https://example.com/myfile.zip",
            filename="my_custom_name.zip",
            status="completed",
        )
        self.db.add_download(entry)
        self.win._model.add_entry(entry)

        panel = self.win._details_panel
        panel.set_download_id("test-fn-ov-1")
        self.assertEqual(panel._ov_filename.text(), "my_custom_name.zip")

    def test_details_panel_overview_displays_seeded_bytes_and_ratio(self):
        """DetailsPanel Overview tab displays total seeded bytes and upload/download ratio."""
        entry = DownloadEntry(
            id="test-seed-ov-1",
            url="magnet:?xt=urn:btih:seeded123",
            filename="seeded_movie.iso",
            total_size=1000000,
            downloaded_size=1000000,
            uploaded_size=2500000,
            download_type="torrent",
            status="seeding",
        )
        self.db.add_download(entry)
        self.win._model.add_entry(entry)

        panel = self.win._details_panel
        panel.set_download_id("test-seed-ov-1")
        seeded_text = panel._ov_seeded.text()
        self.assertIn("Ratio: 2.50", seeded_text)

    def test_animepahe_console_tab_and_live_log_streaming(self):
        """DetailsPanel AnimePahe console tab actively streams logs, handles filtering and clear."""
        import tempfile
        from my_idm.config import ExternalToolsConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "console_log.txt"
            log_file.write_text("Line 1: Initializing animepahe scraper...\n", encoding="utf-8")

            cfg = ExternalToolsConfig(animepahe_repo_path=tmpdir)
            self.manager._external_tools_config = cfg

            panel = self.win._details_panel

            # Side tabs on left side for Details & Console
            self.assertEqual(panel._side_tabs.count(), 2)
            self.assertIn("Details", panel._side_tabs.tabText(0))
            self.assertIn("Console", panel._side_tabs.tabText(1))

            # Details tabs are strictly download tabs (Overview, Files, Peers, Trackers, Segments)
            self.assertEqual(panel._tabs.count(), 5)
            self.assertEqual(panel._tabs.indexOf(panel._tab_console), -1)

            # Initially in details mode
            self.assertEqual(panel.current_mode(), "details")
            self.assertFalse(panel.is_animepahe_console_active())

            # Show animepahe console
            panel.show_animepahe_console()
            self.assertEqual(panel.current_mode(), "console")
            self.assertTrue(panel.is_animepahe_console_active())
            self.assertEqual(panel._side_tabs.currentIndex(), 1)

            # Check header
            self.assertEqual(panel._lbl_icon.text(), "🎬")
            self.assertEqual(panel._lbl_title.text(), "AnimePahe CLI Scraper Console")

            # Check initial log loaded
            panel._poll_console_log()
            self.assertIn("Line 1: Initializing animepahe scraper...", panel._console_visible_text())

            # Append new lines to simulate real-time active output
            with open(log_file, "a", encoding="utf-8") as f:
                f.write("Line 2: Checking episode 5...\n")
                f.write("Line 3: Found magnet link, forwarding to backlog.\n")

            panel._poll_console_log()
            text = panel._console_visible_text()
            self.assertIn("Line 2: Checking episode 5...", text)
            self.assertIn("Line 3: Found magnet link", text)

            # Test filter
            panel._console_filter_edit.setText("magnet")
            filtered = panel._console_visible_text()
            self.assertIn("Line 3: Found magnet link", filtered)
            self.assertNotIn("Line 2: Checking episode 5...", filtered)

            # Clear filter
            panel._console_filter_edit.setText("")
            self.assertIn("Line 2: Checking episode 5...", panel._console_visible_text())

            # Test clear button
            panel._console_clear_btn.click()
            self.assertEqual(panel._console_visible_text(), "")

            # Test wrap toggle
            panel._console_wrap_cb.setChecked(True)
            self.assertEqual(
                panel._console_text.lineWrapMode(),
                QPlainTextEdit.LineWrapMode.WidgetWidth,
            )
            panel._console_wrap_cb.setChecked(False)
            self.assertEqual(
                panel._console_text.lineWrapMode(),
                QPlainTextEdit.LineWrapMode.NoWrap,
            )

    @staticmethod
    def _session_banner(ts, cmd="py scraper.py"):
        rule = "=" * 55
        return (
            f"\n{rule}\n"
            f"  AnimePahe CLI Scraper Session Started: {ts}\n"
            f"  Command: {cmd}\n"
            f"{rule}\n\n"
        )

    def _reset_console(self):
        panel = self.win._details_panel
        panel.show_animepahe_console()
        panel._raw_log_lines.clear()
        panel._console_text.clear()
        panel._console_session_tabs.set_sessions([])
        panel._console_filter_edit.setText("")
        return panel

    def test_animepahe_console_lists_one_side_tab_per_session(self):
        """Each scraper run gets its own entry in the left-hand list."""
        panel = self._reset_console()
        for ts in ("2026-09-29 10:00:00", "2026-09-29 12:00:00", "2026-09-29 16:18:01"):
            panel._append_log_text(self._session_banner(ts))
            panel._append_log_text("line for " + ts + "\n")

        tabs = panel._console_session_tabs
        self.assertEqual(tabs.count(), 3)
        # Newest first, so the live session is visible without scrolling.
        self.assertIn("16:18", tabs.tabText(0))
        self.assertIn("10:00", tabs.tabText(2))

    def test_animepahe_console_defaults_to_the_newest_session(self):
        panel = self._reset_console()
        for ts in ("2026-09-29 10:00:00", "2026-09-29 16:18:01"):
            panel._append_log_text(self._session_banner(ts))
            panel._append_log_text("line for " + ts + "\n")

        text = panel._console_visible_text()
        self.assertIn("16:18:01", text)
        self.assertIn("line for 2026-09-29 16:18:01", text)
        # Older runs are reachable but not shown.
        self.assertNotIn("line for 2026-09-29 10:00:00", text)

    def test_animepahe_console_selecting_a_tab_switches_the_pane(self):
        panel = self._reset_console()
        for ts in ("2026-09-29 10:00:00", "2026-09-29 16:18:01"):
            panel._append_log_text(self._session_banner(ts))
            panel._append_log_text("line for " + ts + "\n")

        tabs = panel._console_session_tabs
        older = next(
            tabs.keyAt(i) for i in range(tabs.count())
            if "10:00:00" in tabs.keyAt(i)
        )
        tabs.set_current_key(older)
        panel._on_console_session_selected(older)

        text = panel._console_visible_text()
        self.assertIn("line for 2026-09-29 10:00:00", text)
        self.assertNotIn("line for 2026-09-29 16:18:01", text)

    def test_animepahe_console_supports_multi_line_selection_and_copy(self):
        """Regression: a tree widget lost free-text select-and-copy.

        The log body must stay a real text widget so a user can drag-select
        several lines and press Ctrl+C.
        """
        panel = self._reset_console()
        panel._append_log_text("\n".join(f"line {i}" for i in range(40)) + "\n")

        text_widget = panel._console_text
        self.assertTrue(text_widget.isReadOnly())

        text_widget.selectAll()
        selected = text_widget.textCursor().selectedText()
        self.assertIn("line 0", selected)
        self.assertIn("line 39", selected)
        # QTextCursor uses U+2029 (paragraph separator) between blocks, not "\n".
        # A selection spanning many of them is what a row-based list cannot do.
        self.assertGreater(selected.count(chr(0x2029)), 10)

    def test_animepahe_console_filter_finds_a_match_in_any_session(self):
        panel = self._reset_console()
        panel._append_log_text(self._session_banner("2026-09-29 08:00:00") + "A1\nNEEDLE\n")
        panel._append_log_text(self._session_banner("2026-09-29 09:00:00") + "B1\n")

        # The needle sits in an older, unselected session.
        self.assertNotIn("NEEDLE", panel._console_visible_text())

        panel._console_filter_edit.setText("NEEDLE")
        text = panel._console_visible_text()
        self.assertIn("NEEDLE", text, "filter must jump to the session that matches")
        self.assertNotIn("B1", text)

    def test_animepahe_console_keeps_your_selection_when_a_session_starts(self):
        """A new run must not yank the user off the session they are reading."""
        panel = self._reset_console()
        panel._append_log_text(self._session_banner("2026-09-29 09:00:00") + "OLD\n")
        panel._append_log_text(self._session_banner("2026-09-29 10:00:00") + "NEW\n")

        tabs = panel._console_session_tabs
        older = next(
            tabs.keyAt(i) for i in range(tabs.count()) if "09:00:00" in tabs.keyAt(i)
        )
        tabs.set_current_key(older)
        panel._on_console_session_selected(older)
        self.assertIn("OLD", panel._console_visible_text())

        panel._append_log_text(self._session_banner("2026-09-29 11:00:00") + "LATEST\n")
        self.assertEqual(tabs.current_key(), older, "selection must be preserved")
        self.assertIn("OLD", panel._console_visible_text())

    def test_animepahe_console_follows_a_new_session_while_tailing_live(self):
        """If you are on the newest session, a new run should take over."""
        panel = self._reset_console()
        panel._append_log_text(self._session_banner("2026-09-29 09:00:00") + "A\n")
        self.assertTrue(panel._console_session_tabs.is_following_live())

        panel._append_log_text(self._session_banner("2026-09-29 10:00:00") + "B\n")
        self.assertIn("B", panel._console_visible_text())
        self.assertTrue(panel._console_session_tabs.is_following_live())

    def test_animepahe_console_clear_empties_both_panes(self):
        panel = self._reset_console()
        panel._append_log_text(self._session_banner("2026-09-29 09:00:00") + "A\n")
        self.assertEqual(panel._console_session_tabs.count(), 1)

        panel._console_clear_btn.click()
        self.assertEqual(panel._console_visible_text(), "")
        self.assertEqual(panel._console_session_tabs.count(), 0)

    # -- console log retention ------------------------------------------

    def test_console_log_keeps_only_the_last_three_days(self):
        """Only sessions inside the retention window survive a prune."""
        from datetime import datetime, timedelta, timezone

        rule = "=" * 55

        def banner(ts):
            return (
                f"\n{rule}\n  AnimePahe CLI Scraper Session Started: {ts}\n"
                f"  Command: py scraper.py\n{rule}\n\n"
            )

        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        just_outside = (now - timedelta(days=3, hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        recent = (now - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")

        log_text = (
            banner(old) + "OLD-LINE\n"
            + banner(just_outside) + "EDGE-LINE\n"
            + banner(recent) + "RECENT-LINE\n"
        )

        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock
        from my_idm.config import ExternalToolsConfig

        tmpdir = tempfile.mkdtemp()
        log_path = Path(tmpdir) / "console_log.txt"
        log_path.write_text(log_text, encoding="utf-8")

        cfg = ExternalToolsConfig(ytdlp_ffmpeg_path="")
        mgr = MagicMock()
        mgr.external_tools_config = cfg
        mgr.is_animepahe_running.return_value = False
        cfg.get_console_log_path = MagicMock(return_value=log_path)
        cfg.get_debug_log_path = MagicMock(return_value=log_path)

        panel = DetailsPanel(mgr)
        panel.show_animepahe_console()
        panel._poll_console_log()
        self.assertEqual(panel._console_session_tabs.count(), 3)

        panel._prune_console_log()

        body = "".join(panel._raw_log_lines)
        self.assertIn("RECENT-LINE", body)
        self.assertNotIn("OLD-LINE", body)
        self.assertNotIn("EDGE-LINE", body)

        # The tab list shrinks to the surviving session...
        self.assertEqual(panel._console_session_tabs.count(), 1)

        # ...and the file on disk is compacted so it stops growing.
        on_disk = log_path.read_text(encoding="utf-8")
        self.assertIn("RECENT-LINE", on_disk)
        self.assertNotIn("OLD-LINE", on_disk)
        self.assertLess(log_path.stat().st_size, len(log_text))

    def test_console_log_prune_keeps_undated_preamble(self):
        """Output before any banner is kept rather than dropped on a guess."""
        rule = "=" * 55
        from datetime import datetime, timedelta, timezone
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock
        from my_idm.config import ExternalToolsConfig

        now = datetime.now(timezone.utc)
        recent = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        log_text = (
            "preamble line before any banner\n"
            f"\n{rule}\n  AnimePahe CLI Scraper Session Started: {recent}\n"
            f"  Command: py scraper.py\n{rule}\n\nRECENT\n"
        )
        tmpdir = tempfile.mkdtemp()
        log_path = Path(tmpdir) / "console_log.txt"
        log_path.write_text(log_text, encoding="utf-8")

        cfg = ExternalToolsConfig(ytdlp_ffmpeg_path="")
        mgr = MagicMock()
        mgr.external_tools_config = cfg
        mgr.is_animepahe_running.return_value = False
        cfg.get_console_log_path = MagicMock(return_value=log_path)
        cfg.get_debug_log_path = MagicMock(return_value=log_path)

        panel = DetailsPanel(mgr)
        panel.show_animepahe_console()
        panel._poll_console_log()
        panel._prune_console_log()

        body = "".join(panel._raw_log_lines)
        self.assertIn("preamble line before any banner", body)
        self.assertIn("RECENT", body)

    def test_console_log_not_rewritten_while_scraper_runs(self):
        """The scraper holds the log open, so pruning must not touch the file."""
        from datetime import datetime, timedelta, timezone
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock
        from my_idm.config import ExternalToolsConfig

        rule = "=" * 55
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S")
        recent = (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        log_text = (
            f"\n{rule}\n  AnimePahe CLI Scraper Session Started: {old}\n"
            f"  Command: py x.py\n{rule}\n\nOLD\n"
            f"\n{rule}\n  AnimePahe CLI Scraper Session Started: {recent}\n"
            f"  Command: py x.py\n{rule}\n\nNEW\n"
        )
        tmpdir = tempfile.mkdtemp()
        log_path = Path(tmpdir) / "console_log.txt"
        log_path.write_text(log_text, encoding="utf-8")

        cfg = ExternalToolsConfig(ytdlp_ffmpeg_path="")
        mgr = MagicMock()
        mgr.external_tools_config = cfg
        mgr.is_animepahe_running.return_value = True
        cfg.get_console_log_path = MagicMock(return_value=log_path)
        cfg.get_debug_log_path = MagicMock(return_value=log_path)

        panel = DetailsPanel(mgr)
        panel.show_animepahe_console()
        panel._poll_console_log()
        panel._prune_console_log()

        # In-memory view is pruned...
        self.assertNotIn("OLD", "".join(panel._raw_log_lines))
        # ...but the file the running scraper owns is left untouched.
        self.assertIn("OLD", log_path.read_text(encoding="utf-8"))

    def test_animepahe_console_status_change_reflection(self):
        """AnimePahe status change reflects on console tab badges and action button."""
        panel = self.win._details_panel
        panel.show_animepahe_console()

        # Scraper starts
        self.manager.animepahe_status_changed.emit(True)
        self.assertIn("Active", panel._console_status_lbl.text())
        self.assertIn("ACTIVE", panel._lbl_badge.text())
        self.assertIn("Stop", panel._console_action_btn.text())

        # Scraper stops
        self.manager.animepahe_status_changed.emit(False)
        self.assertIn("Stopped", panel._console_status_lbl.text())
        self.assertIn("STOPPED", panel._lbl_badge.text())
        self.assertIn("Start", panel._console_action_btn.text())

    def test_animepahe_embedded_browser_subtab_lifecycle(self):
        """DetailsPanel creates a Browser subtab inside the Console tab, docks browser, and handles lifecycle."""
        panel = self.win._details_panel

        # 1. Console view has subtabs with Log present by default
        self.assertIsNotNone(panel._console_subtabs)
        self.assertEqual(panel._console_subtabs.count(), 1)
        self.assertIn("Log", panel._console_subtabs.tabText(0))
        self.assertFalse(panel.is_browser_tab_active())
        self.assertFalse(panel.is_browser_attached())

        # 2. Browser container HWND is registered
        container_hwnd = panel.browser_container_hwnd
        self.assertIsNotNone(container_hwnd)
        self.assertIsInstance(container_hwnd, int)
        self.assertEqual(self.manager.browser_container_hwnd, container_hwnd)

        # 3. Simulate AnimePahe browser opening (calling show_browser_tab with mock HWND)
        req_signal_fired = False
        panel.browser_tab_requested.connect(lambda: nonlocal_set())
        def nonlocal_set():
            nonlocal req_signal_fired
            req_signal_fired = True

        mock_hwnd = 123456
        with unittest.mock.patch.object(panel._browser_container, "attach_window", return_value=True) as mock_attach:
            panel.show_browser_tab(mock_hwnd)
            mock_attach.assert_called_once_with(mock_hwnd)

        # Subtab is created and active
        self.assertEqual(panel._console_subtabs.count(), 2)
        self.assertIn("Browser", panel._console_subtabs.tabText(1))
        self.assertTrue(panel.is_browser_tab_active())
        self.assertEqual(panel.current_mode(), "console")
        self.assertTrue(req_signal_fired)
        self.assertIn("Active", panel._browser_status_lbl.text())

        # 4. Test float / detach toggle
        panel._browser_container._chrome_hwnd = mock_hwnd
        panel._on_toggle_float_browser()
        self.assertTrue(panel._is_browser_floating)
        self.assertIn("Embed", panel._btn_float_browser.text())

        panel._on_toggle_float_browser()
        self.assertFalse(panel._is_browser_floating)
        self.assertIn("Detach", panel._btn_float_browser.text())

        # 5. Hide browser tab on challenge completion
        panel.hide_browser_tab()
        self.assertEqual(panel._console_subtabs.count(), 1)
        self.assertFalse(panel.is_browser_tab_active())
        self.assertEqual(panel._console_subtabs.currentIndex(), 0)
        self.assertIn("Idle", panel._browser_status_lbl.text())


if __name__ == "__main__":
    unittest.main()


