"""Unit tests for the bottom details panel and sub-tabs (Overview, Files, Peers, Trackers, Segments)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QSplitter, QTableWidget

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

        # Uncheck checkbox on file 1 -> priority becomes 0 (Don't Download)
        file1.setCheckState(0, Qt.CheckState.Unchecked)
        panel._on_tree_item_changed(file1, 0)
        self.manager._torrent.set_torrent_file_priority.assert_called_with("test-torrent-1", 1, 0)
        self.assertEqual(combo_file1.currentText(), "Don't Download")

        # Folder checkState becomes partially checked because file0 is checked and file1 is unchecked
        self.assertEqual(root_folder.checkState(0), Qt.CheckState.PartiallyChecked)

        # Uncheck root folder -> unchecks all child files and sets priorities to 0
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


if __name__ == "__main__":
    unittest.main()


