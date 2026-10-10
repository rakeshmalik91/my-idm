"""Unit tests for the bottom details panel and sub-tabs (Overview, Files, Peers, Trackers, Segments)."""

import json
import sys
import tempfile
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
    QTableView,
    QWidget,
)

from my_idm.database import Database, DownloadEntry, SegmentEntry
from my_idm.details_panel import DetailsPanel
from my_idm.main_window import MainWindow
from my_idm.manager import DownloadManager

app = QApplication.instance() or QApplication([])


def destroy_window(win):
    """Actually close and destroy a ``MainWindow``.

    ``MainWindow.closeEvent`` ignores the close event and merely hides the
    window whenever ``close_to_tray`` / ``enable_system_tray`` are on (both
    default True), so a bare ``win.close()`` leaves the window alive for the
    rest of the session -- together with its 1 Hz ``_details_timer``, its 4 s
    ``_tor_availability_timer`` (which opens a real socket probe), the panel's
    250 ms ``_log_timer`` and 300 ms ``_browser_monitor_timer`` (which poll
    ``~/.my-idm/logs/animepahe_console.log``), and its tray icon. ``_force_exit``
    short-circuits the intercept; the call then also runs ``manager.stop()``,
    which is idempotent.
    """
    for name in ("_tor_availability_timer", "_details_timer"):
        timer = getattr(win, name, None)
        if timer is not None:
            timer.stop()
    stop_panel_timers(getattr(win, "_details_panel", None))
    win._force_exit = True
    win.close()
    win.deleteLater()
    QApplication.processEvents()


def stop_panel_timers(panel):
    """Silence a DetailsPanel's periodic timers and drop it from the event loop."""
    if panel is None:
        return
    for name in ("_log_timer", "_browser_monitor_timer", "_queues_timer"):
        timer = getattr(panel, name, None)
        if timer is not None:
            timer.stop()
    panel.deleteLater()
    QApplication.processEvents()


class TestDetailsPanel(unittest.TestCase):

    def setUp(self):
        # Registered with addCleanup (LIFO) so a failing assert cannot leak a
        # window, six QTimers, a manager thread, or an open sqlite handle.
        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)
        self.manager = DownloadManager(self.db)
        self.addCleanup(self.manager.stop)
        self.win = MainWindow(self.manager)
        self.addCleanup(destroy_window, self.win)
        self.addCleanup(stop_panel_timers, self.win._details_panel)
        # Per-test scratch directory; the three console-retention tests used
        # tempfile.mkdtemp() with no cleanup, leaking a directory into %TEMP%
        # on every single run.
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.tmp_path = Path(self.tmp_dir.name)

    def _build(self, entry, manager=None):
        """Persist *entry* and mirror it into the window's model."""
        self.db.add_download(entry)
        self.win._model.add_entry(entry)
        return entry

    def test_details_panel_splitter_integration(self):
        """MainWindow central widget should be a vertical QSplitter with table and details panel."""
        splitter = self.win.centralWidget()
        self.assertIsInstance(splitter, QSplitter)
        self.assertEqual(splitter.orientation(), Qt.Orientation.Vertical)
        self.assertEqual(splitter.count(), 2)

        # Table is now wrapped in a container for the segregated view control strip
        table_container = splitter.widget(0)
        self.assertIsInstance(table_container, QWidget)
        # The table should be a child of the container
        self.assertIsInstance(self.win._table, QTableView)
        self.assertTrue(self.win._table.parent() is table_container or 
                       self.win._table in table_container.findChildren(QTableView))
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
        """Double-clicking an existing file in the Files tree opens it via os.startfile.

        A REAL file on a REAL temp directory is used. The old version patched
        ``pathlib.Path.exists`` process-wide, which -- because
        ``my_idm.details_panel.Path`` *is* ``pathlib.Path`` -- made every
        existence check in the interpreter (tempfile, importlib, Qt's resource
        loader, the manager, every later test module) return True for the
        duration and neutralised the very line under test
        (details_panel.py:1597).
        """
        target_dir = self.tmp_path / "Downloads"
        target_dir.mkdir()
        real_file = target_dir / "file.zip"
        real_file.write_bytes(b"payload" * 32)

        entry = DownloadEntry(
            id="test-dbl-1",
            url="https://example.com/file.zip",
            filename="file.zip",
            save_path=str(target_dir),
            file_path=str(real_file),
            total_size=1048576,
            downloaded_size=1048576,
            status="completed",
            download_type="http",
        )
        self._build(entry)

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
        self.assertTrue(real_file.exists(), "precondition: the file really exists on disk")

        # Patch the helper the panel now calls, not `os.startfile`. The panel lazy-imports it
        # inside the handler, so patching the source module is what intercepts it - and it has to
        # be patched, because the real helper reaches `QDesktopServices.openUrl` / `os.startfile`
        # and would hand the file to the actual shell.
        with unittest.mock.patch(
            "my_idm.external_tools.open_file_in_default_app"
        ) as mock_open, \
             unittest.mock.patch.object(self.manager, "mark_file_not_found") as mock_missing:
            file_item = panel._tree_files.topLevelItem(0)
            panel._on_tree_item_double_clicked(file_item)
            mock_open.assert_called_once_with(str(real_file), create_if_missing=False)
            mock_missing.assert_not_called()
        self.assertTrue(real_file.exists(), "opening must not delete anything")

    def test_details_panel_files_tree_double_click_marks_missing_file(self):
        """The untested other half: a file that is genuinely gone is flagged.

        details_panel.py:1599-1600 routes a vanished file to
        ``mark_file_not_found`` instead of launching it. The file is created and
        then really removed, so no existence check has to be faked.
        """
        target_dir = self.tmp_path / "Downloads"
        target_dir.mkdir()
        real_file = target_dir / "vanished.zip"
        real_file.write_bytes(b"here for now")
        self.assertTrue(real_file.exists())

        entry = DownloadEntry(
            id="test-dbl-gone",
            url="https://example.com/vanished.zip",
            filename="vanished.zip",
            save_path=str(target_dir),
            file_path=str(real_file),
            total_size=1048576,
            downloaded_size=1048576,
            status="completed",
            download_type="http",
        )
        self._build(entry)

        self.manager.get_download_files = MagicMock(return_value=[
            {
                "index": 0,
                "path": "vanished.zip",
                "size": 1048576,
                "downloaded": 1048576,
                "progress": 1.0,
                "priority": 4,
                "status": "completed",
            },
        ])

        panel = self.win._details_panel
        panel.set_download_id("test-dbl-gone")
        file_item = panel._tree_files.topLevelItem(0)

        real_file.unlink()
        self.assertFalse(real_file.exists(), "precondition: the file is really gone")

        with unittest.mock.patch("my_idm.details_panel.os.startfile", create=True) as mock_startfile, \
             unittest.mock.patch.object(self.manager, "mark_file_not_found") as mock_missing:
            panel._on_tree_item_double_clicked(file_item)
            mock_startfile.assert_not_called()
            mock_missing.assert_called_once_with("test-dbl-gone")

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

        with unittest.mock.patch("my_idm.details_panel.os.startfile", create=True) as mock_startfile:
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
        # The window must really be shown: MainWindow._on_progress_updated
        # gates the refresh on `not self._details_panel.isHidden()`, and the
        # old test only worked because setVisible() happens to clear the
        # explicit-hide flag on a never-shown window.
        self.win.show()
        QApplication.processEvents()
        self.assertTrue(panel.isVisible(), "the panel must be visible for the refresh to run")
        panel.set_download_id("live-tor-1")
        QApplication.processEvents()

        # Simulate live progress update signal from manager
        self.win._on_progress_updated(
            "live-tor-1", 5000, 10000, 1048576.0, 5.0, seeds=9, peers=25, upload_speed=262144.0
        )

        # Overview should now display updated speeds, seeds and peers.
        # The FULL label is asserted: separate assertIn() calls are
        # order-independent, so a swapped down/up label would have passed.
        self.assertEqual(panel._ov_swarm.text(), "9 seeds, 25 peers connected")
        self.assertEqual(
            panel._ov_speed.text(), "↓ 1.0 MiB/s   |   ↑ 256.0 KiB/s",
            "download speed must come first, upload second, with the same separator",
        )
        # The refresh must have reached the model too, not just the label.
        model_entry = self.win._model.get_entry_by_id("live-tor-1")
        self.assertEqual(model_entry.downloaded_size, 5000)
        self.assertEqual(model_entry.seeds, 9)
        self.assertEqual(model_entry.peers, 25)
        self.assertEqual(model_entry.upload_speed, 262144.0)

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
            self.assertIn("ON", win2._details_status_btn.toolTip())

            # Toggle off
            win2._act_toggle_details.setChecked(False)
            self.assertFalse(win2._details_panel.isVisible())
            self.assertIn("OFF", win2._details_status_btn.toolTip())

            # Toggle back on — must not restore to 0 height
            win2._act_toggle_details.setChecked(True)
            self.assertTrue(win2._details_panel.isVisible())
            sizes2 = win2._splitter.sizes()
            self.assertGreaterEqual(sizes2[1], 140)
            self.assertIn("ON", win2._details_status_btn.toolTip())
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

        # Launch a second window to verify restoration.
        # destroy_window (not close) so the extra window and its timers really
        # go away; a plain close() only hides it, because close-to-tray is on by
        # default, and it would survive for the rest of the session.
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
                destroy_window(win3)
        finally:
            destroy_window(win2)



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
            save_path=str(self.tmp_path),
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

        # No pathlib.Path.exists patch: `my_idm.details_panel.Path` IS
        # `pathlib.Path`, so patching it process-wide made every existence check
        # in the interpreter return True. A real file is created instead, so the
        # real `f_disk_path.exists()` gate at details_panel.py:1509 decides.
        disk_path = Path(self.tmp_path) / "TestTrashAccepted" / "data.bin"
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_bytes(b"x" * 2500)
        self.assertTrue(disk_path.exists())

        is_downloaded, resolved = panel._is_file_downloaded(item)
        self.assertTrue(is_downloaded, "a file on disk plus 2500 bytes is downloaded")
        self.assertEqual(resolved, disk_path)

        with unittest.mock.patch("my_idm.details_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes) as mock_q, \
             unittest.mock.patch("my_idm.details_panel.send_to_trash") as mock_trash, \
             unittest.mock.patch.object(self.manager, "set_torrent_file_priority") as mock_set_prio:
            item.setCheckState(0, Qt.CheckState.Unchecked)
            mock_q.assert_called_once()
            mock_trash.assert_called_once()
            mock_trash.assert_called_with(disk_path)
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
            save_path=str(self.tmp_path),
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
        disk_path = Path(self.tmp_path) / "TestTrashCombo" / "video.mp4"
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_bytes(b"x" * 10000)
        self.assertTrue(disk_path.exists())
        with unittest.mock.patch("my_idm.details_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes) as mock_q2, \
             unittest.mock.patch("my_idm.details_panel.send_to_trash") as mock_trash2, \
             unittest.mock.patch.object(self.manager, "set_torrent_file_priority") as mock_set_prio:
            combo.setCurrentText("Don't Download")
            mock_q2.assert_called_once()
            mock_trash2.assert_called_once_with(disk_path)
            mock_set_prio.assert_called_with("test-trash-combo-1", 0, 0)
            self.assertEqual(item.text(4), "Skipped")
            self.assertEqual(combo.currentText(), "Don't Download")
            self.assertEqual(item.checkState(0), Qt.CheckState.Unchecked)

    def test_priority_reset_does_not_persist_to_the_items_user_role_data(self):
        """Documents a real defect: the per-item priority reset is thrown away.

        ``_on_file_priority_combo_changed`` mutates
        ``item.data(0, UserRole)["data"]["priority"]`` in place
        (details_panel.py:1635-1636), but ``QVariant`` -> Python conversion
        hands back a *fresh* dict on every ``item.data()`` call, so the write
        never reaches the item. The visible state (combo label, status column,
        check state) is correct, which is why this has gone unnoticed: a later
        rebuild of the tree restores the stale priority. Pinned as-is; see the
        report.
        """
        tor_entry = DownloadEntry(
            id="test-prio-reset",
            url="magnet:?xt=urn:btih:12345678112233445566778899aabbccddeeff",
            filename="PrioReset",
            download_type="torrent",
            status="downloading",
            save_path=str(self.tmp_path),
            metadata_json='''{
                "files": [
                    {
                        "index": 0,
                        "path": "PrioReset/data.bin",
                        "size": 500,
                        "downloaded": 500,
                        "progress": 1.0,
                        "priority": 4,
                        "status": "completed"
                    }
                ]
            }''',
        )
        self._build(tor_entry)

        panel = self.win._details_panel
        panel.set_download_id("test-prio-reset")
        item = panel._file_item_map[0]
        combo = panel._tree_files.itemWidget(item, 3)
        disk_path = Path(self.tmp_path) / "PrioReset" / "data.bin"
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_bytes(b"x" * 500)

        with unittest.mock.patch("my_idm.details_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
             unittest.mock.patch("my_idm.details_panel.send_to_trash"), \
             unittest.mock.patch.object(self.manager, "set_torrent_file_priority") as mock_set_prio:
            combo.setCurrentText("Don't Download")

        mock_set_prio.assert_called_once_with("test-prio-reset", 0, 0)
        self.assertEqual(item.text(4), "Skipped")
        self.assertEqual(combo.currentText(), "Don't Download")
        # KNOWN DEFECT: the in-place write is lost, so the item still reports 4.
        self.assertEqual(
            item.data(0, Qt.ItemDataRole.UserRole)["data"]["priority"], 4,
            "KNOWN DEFECT: the priority reset never reaches the item because "
            "QVariant -> Python hands back a fresh dict on every data() call",
        )
        # A full rebuild therefore resurrects the stale priority.
        panel.set_download_id("test-prio-reset")
        rebuilt = panel._file_item_map[0]
        self.assertEqual(
            panel._tree_files.itemWidget(rebuilt, 3).currentText(),
            "Medium (50%)",
            "the stale priority is restored on rebuild, confirming the write was lost",
        )

    def test_multi_selection_dont_download_single_confirmation(self):
        """Setting multiple files to Don't Download via selection triggers exactly ONE prompt."""
        tor_entry = DownloadEntry(
            id="test-trash-multi-1",
            url="magnet:?xt=urn:btih:6666777788889999000011112222333344445555",
            filename="TestTrashMulti",
            download_type="torrent",
            status="downloading",
            save_path=str(self.tmp_path),
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

        expected = [
            Path(self.tmp_path) / "TestTrashMulti" / "file1.bin",
            Path(self.tmp_path) / "TestTrashMulti" / "file2.bin",
        ]
        for index, path in enumerate(expected, start=1):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * (1000 * index))
            self.assertTrue(path.exists(), f"precondition: {path} must exist")

        with unittest.mock.patch("my_idm.details_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes) as mock_q, \
             unittest.mock.patch("my_idm.details_panel.send_to_trash") as mock_trash, \
             unittest.mock.patch.object(self.manager, "set_torrent_file_priority") as mock_set_prio:
            panel._set_items_priority([item0, item1], 0)
            mock_q.assert_called_once()
            self.assertEqual(mock_trash.call_count, 2)
            self.assertEqual(
                sorted(c.args[0] for c in mock_trash.call_args_list), sorted(expected),
                "both selected files are trashed, and only those",
            )
            self.assertEqual(mock_set_prio.call_count, 2)
            self.assertEqual(
                sorted(c.args for c in mock_set_prio.call_args_list),
                [("test-trash-multi-1", 0, 0), ("test-trash-multi-1", 1, 0)],
                "one priority write per selected file, both set to 0",
            )
            self.assertEqual(item0.text(4), "Skipped")
            self.assertEqual(item1.text(4), "Skipped")

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
        from my_idm.config import ExternalToolsConfig

        tmpdir = str(self.tmp_path)
        log_file = self.tmp_path / "console_log.txt"
        log_file.write_text("Line 1: Initializing animepahe scraper...\n", encoding="utf-8")

        cfg = ExternalToolsConfig(animepahe_repo_path=tmpdir)
        self.manager._external_tools_config = cfg

        panel = self.win._details_panel
        # The 250 ms _log_timer fires _poll_console_log() -- the very method
        # these assertions drive by hand -- so any event-loop pump from another
        # test module could let a stray tick land between an action and the
        # assertion. Stop it before touching the file.
        self.addCleanup(panel._log_timer.stop)
        self.addCleanup(panel._browser_monitor_timer.stop)
        self.addCleanup(panel.deleteLater)

        # Side tabs on left side for Details, Queues, Console & Stats
        self.assertEqual(panel._side_tabs.count(), 4)
        self.assertIn("Details", panel._side_tabs.tabText(0))
        self.assertIn("Queues", panel._side_tabs.tabText(1))
        self.assertIn("Console", panel._side_tabs.tabText(2))
        self.assertIn("Stats", panel._side_tabs.tabText(3))

        # Details tabs (Overview, Files, Peers, Trackers, Segments)
        self.assertEqual(panel._tabs.count(), 5)
        self.assertEqual(panel._tabs.indexOf(panel._tab_console), -1)
        self.assertEqual(panel._tabs.indexOf(panel._tab_queues), -1)

        # Initially in details mode
        self.assertEqual(panel.current_mode(), "details")
        self.assertFalse(panel.is_animepahe_console_active())

        # Show animepahe console
        panel.show_animepahe_console()
        self.assertEqual(panel.current_mode(), "console")
        self.assertTrue(panel.is_animepahe_console_active())
        self.assertEqual(panel._side_tabs.currentIndex(), 2)
        self.assertTrue(
            panel._log_timer.isActive(),
            "opening the console must start the live-tail timer",
        )
        panel._log_timer.stop()
        self.assertFalse(panel._log_timer.isActive(), "and it must be stoppable again")

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

        # Test Open GUI button
        self.assertEqual(panel._console_open_gui_btn.text(), "🎬 Open GUI")
        with unittest.mock.patch("my_idm.details_panel.launch_animepahe_gui", return_value=(True, "Launched AnimePahe GUI")) as mock_gui:
            panel._console_open_gui_btn.click()
            mock_gui.assert_called_once_with(cfg)

    def test_animepahe_console_open_gui_button_prompts_if_not_configured(self):
        """Clicking Open GUI when repo path is invalid prompts user to configure preferences."""
        from my_idm.config import ExternalToolsConfig

        cfg = ExternalToolsConfig(animepahe_repo_path="/nonexistent/path/for/test")
        self.manager._external_tools_config = cfg

        panel = self.win._details_panel
        panel.show_animepahe_console()
        panel._log_timer.stop()
        self.addCleanup(panel._log_timer.stop)

        with unittest.mock.patch("my_idm.details_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.No) as mock_q:
            panel._console_open_gui_btn.click()
            mock_q.assert_called_once()

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
        # show_animepahe_console() arms the 250 ms _log_timer, which calls the
        # same _poll_console_log() these tests drive by hand. Stop it so a stray
        # tick from any processEvents() pump cannot rewrite the session list
        # between an action and its assertion.
        panel._log_timer.stop()
        self.assertFalse(panel._log_timer.isActive())
        panel._raw_log_lines.clear()
        panel._console_text.clear()
        panel._console_session_tabs.set_sessions([])
        panel._console_filter_edit.setText("")
        self.addCleanup(panel._log_timer.stop)
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

    def test_console_session_title_survives_a_split_banner_rule(self):
        """Regression: the scraper writes each banner without a closing rule
        after the Command line, so a rule-based splitter absorbed the banner
        lines into the previous session's body and surfaced them as a
        headerless "Earlier output" session — which is exactly why some
        session tabs showed no timestamp.

        Uses the exact banner text the scraper writes, including the long
        Command line, so the rule length (55 chars) is what the live
        line-buffered stdout actually splits mid-line.
        """
        panel = self._reset_console()

        def banner(ts, target):
            rule = "=" * 55
            # No closing rule: the scraper writes rule, Session Started, Command,
            # blank, body — and the next banner opens with its own rule.
            return (
                f"\n{rule}\n"
                f"  AnimePahe CLI Scraper Session Started: {ts}\n"
                f"  Command: python animepahe_download.py --my-idm --url {target} -y\n"
                f"\n"
            )

        new = banner("2026-10-08 15:42:02", "https://animepahe.si/anime/abc")
        old = banner("2026-10-08 14:00:00", "https://animepahe.si/anime/def")

        # First chunk: the complete older session + first 30 chars of the
        # newest banner's opening rule (no trailing newline — the rest is
        # pending in the next poll). This is what live line-buffered stdout
        # actually produces: the banner currently being written is split.
        chunk1 = old + "old line\n" + new[:30]
        # Second chunk: the rest of the newest banner + its body.
        chunk2 = new[30:] + "new line\n"

        for chunk in (chunk1, chunk2):
            panel._append_log_text(chunk)

        tabs = panel._console_session_tabs
        titles = [tabs.tabText(i) for i in range(tabs.count())]
        # Both real sessions must keep their timestamp; the phantom
        # "Earlier output" must not swallow a banner. Newest first.
        self.assertTrue(any("15:42" in t for t in titles), titles)
        self.assertTrue(any("14:00" in t for t in titles), titles)
        self.assertNotIn("Earlier output", titles)
        self.assertTrue("15:42" in titles[0], titles)

    def test_console_session_splitter_handles_real_console_log(self):
        """Pinned against the real console_log.txt: every banner must surface
        as a timestamped session, never as "Earlier output".
        """
        panel = self._reset_console()
        log_path = self.tmp_path / "console_log.txt"
        log_path.write_text(
            "\n=======================================================\n"
            "  AnimePahe CLI Scraper Session Started: 2026-10-08 15:42:02\n"
            "  Command: python animepahe_download.py --my-idm --url https://animepahe.si/anime/abc -y\n"
            "\nChecking AnimePahe mirrors...\n"
            " - animepahe.org... OK (redirected to animepahe.pw)\n"
            "\n=======================================================\n"
            "  AnimePahe CLI Scraper Session Started: 2026-10-08 14:00:00\n"
            "  Command: python animepahe_download.py --my-idm --url https://animepahe.si/anime/def -y\n"
            "\nold line\n",
            encoding="utf-8",
        )
        panel._append_log_text(log_path.read_text(encoding="utf-8"))

        tabs = panel._console_session_tabs
        titles = [tabs.tabText(i) for i in range(tabs.count())]
        self.assertTrue(any("15:42" in t for t in titles), titles)
        self.assertTrue(any("14:00" in t for t in titles), titles)
        self.assertNotIn("Earlier output", titles)

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

    def _retention_panel(self, log_text: str, *, scraper_running: bool, name="console_log.txt"):
        """Build a standalone panel over a real log file in the per-test temp dir.

        The 250 ms ``_log_timer`` is stopped immediately: it calls the same
        ``_poll_console_log()`` the test drives by hand, and
        ``_prune_console_log`` truncates the very file whose ``st_size`` is
        asserted, so a stray tick from any ``processEvents()`` pump could land
        between the prune and the assertion.
        """
        from my_idm.config import ExternalToolsConfig

        log_path = self.tmp_path / name
        log_path.write_text(log_text, encoding="utf-8")

        cfg = ExternalToolsConfig(ytdlp_ffmpeg_path="")
        mgr = MagicMock()
        mgr.external_tools_config = cfg
        mgr.is_animepahe_running.return_value = scraper_running
        cfg.get_console_log_path = MagicMock(return_value=log_path)
        cfg.get_debug_log_path = MagicMock(return_value=log_path)

        panel = DetailsPanel(mgr)
        self.addCleanup(panel._log_timer.stop)
        self.addCleanup(panel._browser_monitor_timer.stop)
        self.addCleanup(panel.deleteLater)
        panel.show_animepahe_console()
        panel._log_timer.stop()
        self.assertFalse(panel._log_timer.isActive())
        return panel, log_path

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
        self.assertEqual(DetailsPanel.CONSOLE_LOG_RETENTION_DAYS, 3)

        panel, log_path = self._retention_panel(log_text, scraper_running=False)
        panel._poll_console_log()
        self.assertEqual(panel._console_session_tabs.count(), 3)

        size_before = log_path.stat().st_size
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
        self.assertNotIn("EDGE-LINE", on_disk)
        self.assertEqual(
            on_disk, "".join(panel._raw_log_lines),
            "the compacted file must hold exactly the rebuilt in-memory log",
        )
        self.assertTrue(on_disk.endswith("\n"), "the rebuild always ends with a newline")
        self.assertLess(
            log_path.stat().st_size, size_before,
            "the on-disk file must shrink after two sessions are dropped",
        )
        self.assertLess(log_path.stat().st_size, len(log_text))
        # A second prune is a no-op: nothing is left outside the window.
        panel._prune_console_log()
        self.assertEqual(log_path.read_text(encoding="utf-8"), on_disk)

    def test_console_log_prune_keeps_undated_preamble(self):
        """Output before any banner is kept rather than dropped on a guess."""
        rule = "=" * 55
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        recent = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        log_text = (
            "preamble line before any banner\n"
            f"\n{rule}\n  AnimePahe CLI Scraper Session Started: {recent}\n"
            f"  Command: py scraper.py\n{rule}\n\nRECENT\n"
        )

        panel, log_path = self._retention_panel(log_text, scraper_running=False)
        panel._poll_console_log()
        panel._prune_console_log()

        body = "".join(panel._raw_log_lines)
        self.assertIn("preamble line before any banner", body)
        self.assertIn("RECENT", body)
        # The undated preamble must survive the on-disk compaction too.
        on_disk = log_path.read_text(encoding="utf-8")
        self.assertIn("preamble line before any banner", on_disk)
        self.assertIn("RECENT", on_disk)
        # The undated preamble is itself listed as a (non-dated) session, so the
        # dated run plus the preamble make two tabs.
        self.assertEqual(panel._console_session_tabs.count(), 2)

    def test_console_log_not_rewritten_while_scraper_runs(self):
        """The scraper holds the log open, so pruning must not touch the file."""
        from datetime import datetime, timedelta, timezone

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

        panel, log_path = self._retention_panel(log_text, scraper_running=True)
        panel._poll_console_log()
        size_before = log_path.stat().st_size
        panel._prune_console_log()

        # In-memory view is pruned...
        body = "".join(panel._raw_log_lines)
        self.assertNotIn("OLD", body)
        self.assertIn("NEW", body)
        # ...but the file the running scraper owns is left untouched.
        self.assertEqual(
            log_path.read_text(encoding="utf-8"), log_text,
            "the on-disk log must be byte-for-byte unchanged while the scraper runs",
        )
        self.assertEqual(log_path.stat().st_size, size_before)

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

        # 4. Test float / detach toggle, driven through the REAL state machine.
        # The old test poked `panel._browser_container._chrome_hwnd` directly,
        # so the production attach path was never exercised and the toggle only
        # ever saw a hand-forged handle. IsWindow is stubbed to False so the
        # Win32 branch is deterministic and cannot touch an unrelated top-level
        # window that happens to own this fake hwnd.
        with unittest.mock.patch("ctypes.windll.user32.IsWindow", return_value=False):
            self.assertFalse(
                panel.is_browser_attached(),
                "precondition: the mocked attach_window must not claim attachment",
            )
            self.assertFalse(panel._is_browser_floating, "attaching must not float it")

            panel._browser_container.attach_window(mock_hwnd)
            self.assertTrue(
                panel.is_browser_attached(),
                "attach_window must register the handle even when IsWindow is False",
            )
            self.assertEqual(panel._browser_container.chrome_hwnd, mock_hwnd)

            panel._on_toggle_float_browser()
            self.assertTrue(panel._is_browser_floating)
            self.assertIn("Embed", panel._btn_float_browser.text())

            panel._on_toggle_float_browser()
            self.assertFalse(panel._is_browser_floating)
            self.assertIn("Detach", panel._btn_float_browser.text())
            self.assertTrue(
                panel.is_browser_attached(),
                "re-embedding must keep the handle so the browser stays usable",
            )
            self.assertEqual(panel._browser_container.chrome_hwnd, mock_hwnd)

        # 5. Hide browser tab on challenge completion
        panel.hide_browser_tab()
        self.assertEqual(panel._console_subtabs.count(), 1)
        self.assertFalse(panel.is_browser_tab_active())
        self.assertEqual(panel._console_subtabs.currentIndex(), 0)
        self.assertIn("Idle", panel._browser_status_lbl.text())

    def test_toggle_float_browser_is_a_noop_without_an_attached_handle(self):
        """Floating an unattached browser must not flip the state machine."""
        panel = self.win._details_panel
        self.assertFalse(panel.is_browser_attached())
        self.assertFalse(panel._is_browser_floating)
        panel._on_toggle_float_browser()
        self.assertFalse(
            panel._is_browser_floating,
            "there is no HWND to float, so the button must do nothing",
        )
        self.assertIn(
            "Detach Window", panel._btn_float_browser.text(),
            "the button label must not change when the toggle was a no-op",
        )

    def test_details_panel_displays_referrer_from_headers_and_metadata(self):
        panel = self.win._details_panel
        # Case 1: referrer in metadata headers dict
        entry1 = DownloadEntry(
            id="test-ref-1",
            url="https://vault-01.uwucdn.top/test.mp4",
            filename="test.mp4",
            save_path="C:/Downloads",
            metadata_json=json.dumps({"headers": {"Referer": "https://kwik.cx/"}}),
        )
        self.db.add_download(entry1)
        self.win._model.add_entry(entry1)
        panel.set_download_id("test-ref-1")
        self.assertEqual(panel._ov_referrer.text(), "https://kwik.cx/")

        # Case 2: referrer in metadata directly
        entry2 = DownloadEntry(
            id="test-ref-2",
            url="https://example.com/test.zip",
            filename="test.zip",
            save_path="C:/Downloads",
            metadata_json=json.dumps({"referer": "https://example.com/source"}),
        )
        self.db.add_download(entry2)
        self.win._model.add_entry(entry2)
        panel.set_download_id("test-ref-2")
        self.assertEqual(panel._ov_referrer.text(), "https://example.com/source")

    def test_details_panel_files_tab_updates_during_active_download(self):
        """Active download progress updates files tab progress bar, tooltip, and status."""
        panel = self.win._details_panel
        entry = DownloadEntry(
            id="test-files-prog-1",
            url="https://example.com/video.mp4",
            filename="video.mp4",
            save_path="C:/Downloads",
            total_size=1000000,
            downloaded_size=0,
            status="downloading",
            download_type="http",
        )
        self._build(entry)

        # Show panel and select the download
        self.win._act_toggle_details.setChecked(True)
        panel.setVisible(True)
        panel.set_download_id("test-files-prog-1")

        # Files tab should have the file item
        item = panel._file_item_map.get(0)
        self.assertIsNotNone(item)
        pb = panel._tree_files.itemWidget(item, 2)
        self.assertIsNotNone(pb)
        self.assertEqual(pb.value(), 0)
        self.assertEqual(pb.format(), "0.0%")
        self.assertEqual(item.text(4), "Downloading")

        # Simulate live progress tick from engine (50% progress)
        self.win._on_progress_updated(
            "test-files-prog-1",
            downloaded=500000,
            total=1000000,
            speed=50000.0,
            eta=10.0,
            seeds=0,
            peers=0,
            upload_speed=0.0,
        )

        # Verify Files tab updated dynamically
        self.assertEqual(pb.value(), 50)
        self.assertEqual(pb.format(), "50.0%")
        self.assertEqual(item.text(4), "Downloading")
        self.assertIn("50.0%", pb.toolTip())

        # Simulate progress reaching 100%
        self.win._on_progress_updated(
            "test-files-prog-1",
            downloaded=1000000,
            total=1000000,
            speed=0.0,
            eta=0.0,
            seeds=0,
            peers=0,
            upload_speed=0.0,
        )

        self.assertEqual(pb.value(), 100)
        self.assertEqual(pb.format(), "100.0%")
        self.assertEqual(item.text(4), "Completed")
        self.assertIn("100.0%", pb.toolTip())

    def test_details_panel_queues_tab_creation_and_listing(self):
        """Queues tab displays all queues with spinboxes for max at once and limits."""
        panel = self.win._details_panel
        self.assertEqual(panel._tabs.indexOf(panel._tab_queues), -1)

        # Switch to queues tab via side tabs
        panel.set_mode("queues")
        self.assertEqual(panel.current_mode(), "queues")
        self.assertEqual(panel._side_tabs.currentIndex(), 1)
        self.assertEqual(panel._lbl_title.text(), "Download Queues & Concurrency")

        # Must list at least Default, AnimePahe, YouTube
        queues = self.manager.get_queues()
        self.assertGreaterEqual(len(queues), 3)
        self.assertEqual(panel._table_queues.rowCount(), len(queues))

        for q in queues:
            widgets = panel._queue_row_widgets.get(q.id)
            self.assertIsNotNone(widgets, f"Missing row widgets for queue {q.id}")
            self.assertEqual(widgets["spin_max"].value(), max(0, q.max_concurrent))
            self.assertEqual(widgets["spin_dl"].value(), max(0, int(q.download_limit or 0) // 1024))
            self.assertEqual(widgets["spin_up"].value(), max(0, int(q.upload_limit or 0) // 1024))

    def test_details_panel_queues_tab_edit_limits(self):
        """Editing spinbox values in the Queues tab updates the queue's limits in the manager and DB."""
        panel = self.win._details_panel
        queues = self.manager.get_queues()
        target_q = queues[0]
        qid = target_q.id

        widgets = panel._queue_row_widgets.get(qid)
        self.assertIsNotNone(widgets)

        # 1. Edit max_concurrent
        widgets["spin_max"].setValue(7)
        self.assertEqual(self.manager.get_queue(qid).max_concurrent, 7)

        # 2. Edit download_limit
        widgets["spin_dl"].setValue(1024)  # 1024 KB/s
        self.assertEqual(self.manager.get_queue(qid).download_limit, 1024 * 1024)

        # 3. Edit upload_limit
        widgets["spin_up"].setValue(512)   # 512 KB/s
        self.assertEqual(self.manager.get_queue(qid).upload_limit, 512 * 1024)

    def test_details_panel_queues_tab_pause_and_resume(self):
        """Pause and resume buttons in Queues tab trigger queue pause/resume."""
        panel = self.win._details_panel
        queues = self.manager.get_queues()
        target_q = queues[0]
        qid = target_q.id

        entry = DownloadEntry(
            id="test-queue-pause-1",
            url="https://example.com/test.zip",
            filename="test.zip",
            save_path="C:/Downloads",
            status="downloading",
            queue_id=qid,
        )
        self._build(entry)

        # Refresh panel
        panel.refresh()
        widgets = panel._queue_row_widgets.get(qid)
        self.assertIsNotNone(widgets)

        # Pause queue via button click
        widgets["btn_pause"].click()

        # Wait for status job pool to execute
        self.manager._status_job_pool.wait_idle()
        QApplication.processEvents()

        # Entry should now be paused
        updated_entry = self.db.get_download("test-queue-pause-1")
        self.assertEqual(updated_entry.status, "paused")

        # Resume queue via button click
        panel.refresh()
        widgets["btn_resume"].click()
        self.manager._status_job_pool.wait_idle()
        QApplication.processEvents()

        updated_entry2 = self.db.get_download("test-queue-pause-1")
        self.assertIn(updated_entry2.status, ("queued", "downloading"))

    def test_details_panel_queues_tab_double_click_toggles_queue_visibility(self):
        """Double-clicking a queue row in Queues tab toggles queue visibility in model."""
        panel = self.win._details_panel
        queues = self.manager.get_queues()
        self.assertGreater(len(queues), 1)
        all_qids = {q.id for q in queues}
        target_q = queues[1]

        # Initially all queues visible
        self.assertIsNone(self.win._model.queue_filter())

        row = panel._queue_row_widgets[target_q.id]["row"]
        item = panel._table_queues.item(row, 1) or panel._table_queues.item(row, 2)
        panel._on_queue_row_double_clicked(item)

        # target_q should now be hidden, remaining queues visible
        self.assertEqual(self.win._model.queue_filter(), all_qids - {target_q.id})

    def test_details_panel_queues_tab_filter_toggle_and_icon_buttons(self):
        """Action buttons have icon-only labels and eye button toggles queue filter."""
        panel = self.win._details_panel
        panel.set_mode("queues")
        queues = self.manager.get_queues()
        self.assertGreater(len(queues), 1)
        all_qids = {q.id for q in queues}
        target_q = queues[1]
        widgets = panel._queue_row_widgets[target_q.id]

        # Verify button texts are icon-only and filter button has vector icon
        self.assertEqual(widgets["btn_pause"].text(), "⏸")
        self.assertEqual(widgets["btn_resume"].text(), "▶")
        self.assertEqual(widgets["btn_filter"].text(), "")
        self.assertFalse(widgets["btn_filter"].icon().isNull())

        # Initially no queue filter is active (all queues visible)
        self.assertIsNone(self.win._model.queue_filter())
        self.assertIn("visible", widgets["btn_filter"].toolTip().lower())

        # Click eye button to hide target_q
        widgets["btn_filter"].click()
        self.assertEqual(self.win._model.queue_filter(), all_qids - {target_q.id})
        self.assertIn("hidden", widgets["btn_filter"].toolTip().lower())

        # Click eye button again to unhide target_q (all queues visible again -> filter resets to None)
        widgets["btn_filter"].click()
        self.assertIsNone(self.win._model.queue_filter())
        self.assertIn("visible", widgets["btn_filter"].toolTip().lower())

    def test_details_panel_queues_tab_multiselect_filter_synced_with_table(self):
        """Multiple queues can be filtered and stay bidirectional synced with table model."""
        panel = self.win._details_panel
        panel.set_mode("queues")
        queues = self.manager.get_queues()
        self.assertGreaterEqual(len(queues), 3)
        q0, q1, q2 = queues[0], queues[1], queues[2]

        w0 = panel._queue_row_widgets[q0.id]
        w1 = panel._queue_row_widgets[q1.id]
        w2 = panel._queue_row_widgets[q2.id]

        # 1. Hide q1 via eye button
        w1["btn_filter"].click()
        self.assertIn("hidden", w1["btn_filter"].toolTip().lower())
        self.assertIn("visible", w0["btn_filter"].toolTip().lower())
        self.assertIn("visible", w2["btn_filter"].toolTip().lower())

        # 2. Hide q2 via eye button -> multiple queues filtered
        w2["btn_filter"].click()
        self.assertIn("hidden", w1["btn_filter"].toolTip().lower())
        self.assertIn("hidden", w2["btn_filter"].toolTip().lower())
        self.assertIn("visible", w0["btn_filter"].toolTip().lower())
        self.assertTrue(self.win._model.is_queue_filtered())
        self.assertIn(q0.id, self.win._model.queue_filter())
        self.assertNotIn(q1.id, self.win._model.queue_filter())
        self.assertNotIn(q2.id, self.win._model.queue_filter())

        # 3. Simulate change from queue column filter in downloads table (e.g. user checks q1)
        self.win._model.set_queue_filter({q0.id, q1.id})
        # Queues tab must be synced immediately
        self.assertIn("visible", w0["btn_filter"].toolTip().lower())
        self.assertIn("visible", w1["btn_filter"].toolTip().lower())
        self.assertIn("hidden", w2["btn_filter"].toolTip().lower())

        # 4. Context menu Solo queue: Show only q2
        panel._filter_solo_queue(q2.id)
        self.assertEqual(self.win._model.queue_filter(), {q2.id})
        self.assertIn("hidden", w0["btn_filter"].toolTip().lower())
        self.assertIn("hidden", w1["btn_filter"].toolTip().lower())
        self.assertIn("visible", w2["btn_filter"].toolTip().lower())

        # 5. Show all queues
        panel._filter_show_all_queues()
        self.assertIsNone(self.win._model.queue_filter())
        self.assertIn("visible", w0["btn_filter"].toolTip().lower())
        self.assertIn("visible", w1["btn_filter"].toolTip().lower())
        self.assertIn("visible", w2["btn_filter"].toolTip().lower())

    def test_details_panel_queues_tab_realtime_counts_and_speed_updates(self):
        """Downloads column shows 'X active / Y total' and speed/status updates in realtime."""
        panel = self.win._details_panel
        queues = self.manager.get_queues()
        target_q = queues[0]

        # Add an active downloading entry with speed
        entry = DownloadEntry(
            id="test-live-speed-1",
            url="https://example.com/live.iso",
            filename="live.iso",
            status="downloading",
            total_size=100 * 1024 * 1024,
            downloaded_size=20 * 1024 * 1024,
            speed=1024 * 1024 * 2.5,  # 2.5 MB/s
            upload_speed=1024 * 50,    # 50 KB/s
            queue_id=target_q.id,
        )
        self.db.add_download(entry)
        self.win._load_history()
        self.win._model.update_progress(
            "test-live-speed-1",
            downloaded=20 * 1024 * 1024,
            total=100 * 1024 * 1024,
            speed=1024 * 1024 * 2.5,
            eta=30.0,
            upload_speed=1024 * 50,
        )

        # Switch to queues mode: timer should be active and values updated
        panel.set_mode("queues")
        self.assertTrue(panel._queues_timer.isActive())

        widgets = panel._queue_row_widgets[target_q.id]
        # Download column format: "X active / Y total"
        self.assertEqual(widgets["item_counts"].text(), "1 active / 1 total")
        # Status shows Running (1)
        self.assertEqual(widgets["item_status"].text(), "Running (1)")
        # Speed shows live download and upload speeds
        speed_txt = widgets["item_speed"].text()
        self.assertIn("↓", speed_txt)
        self.assertIn("MiB/s", speed_txt)

        # Switch away from queues mode: timer stops
        panel.set_mode("details")
        self.assertFalse(panel._queues_timer.isActive())

    def test_details_panel_queues_tab_seeding_not_counted_as_active(self):
        """Active count in queues tab should not count seeding torrents."""
        panel = self.win._details_panel
        queues = self.manager.get_queues()
        target_q = queues[0]

        # Add a seeding entry (no active downloading entry)
        seeding_entry = DownloadEntry(
            id="test-seeding-1",
            url="magnet:?xt=urn:btih:dummy1",
            filename="seeding_file.iso",
            status="seeding",
            total_size=500 * 1024 * 1024,
            downloaded_size=500 * 1024 * 1024,
            speed=0.0,
            upload_speed=1024 * 150,  # 150 KB/s
            queue_id=target_q.id,
        )
        self.db.add_download(seeding_entry)
        self.win._load_history()

        panel.set_mode("queues")
        widgets = panel._queue_row_widgets[target_q.id]

        # Seeding must NOT count as active: should be "0 active / 1 total"
        self.assertEqual(widgets["item_counts"].text(), "0 active / 1 total")
        # Status should show Seeding (1), not Running
        self.assertEqual(widgets["item_status"].text(), "Seeding (1)")
        # Tooltip should mention Seeding
        self.assertIn("Active: 0", widgets["item_counts"].toolTip())
        self.assertIn("Seeding: 1", widgets["item_counts"].toolTip())

        # Now add a downloading entry alongside the seeding entry
        dl_entry = DownloadEntry(
            id="test-downloading-1",
            url="https://example.com/file2.zip",
            filename="file2.zip",
            status="downloading",
            total_size=100 * 1024 * 1024,
            downloaded_size=10 * 1024 * 1024,
            speed=1024 * 500,
            queue_id=target_q.id,
        )
        self.db.add_download(dl_entry)
        self.win._load_history()
        panel._update_queues()

        # Active count is 1 (not 2!)
        self.assertEqual(widgets["item_counts"].text(), "1 active / 2 total")
        self.assertEqual(widgets["item_status"].text(), "Running (1)")
        self.assertIn("Active: 1", widgets["item_counts"].toolTip())
        self.assertIn("Seeding: 1", widgets["item_counts"].toolTip())

        panel.set_mode("details")

    def test_details_panel_queues_tab_manage_queues_button(self):
        """Queues tab includes a button to jump to queue management in preferences."""
        panel = self.win._details_panel
        self.assertTrue(hasattr(panel, "_btn_manage_queues"))
        self.assertIn("Manage Queues", panel._btn_manage_queues.text())
        self.assertIn("Preferences", panel._btn_manage_queues.text())

        # Clicking the button emits manage_queues_requested and calls MainWindow._on_manage_queues
        signal_emitted = []
        panel.manage_queues_requested.connect(lambda: signal_emitted.append(True))

        called = []
        original_on_manage = self.win._on_manage_queues
        self.win._on_manage_queues = lambda: called.append(True)
        try:
            panel._btn_manage_queues.click()
            self.assertTrue(signal_emitted)
            self.assertTrue(called)
        finally:
            self.win._on_manage_queues = original_on_manage

    def test_details_panel_stats_tab(self):
        """Stats tab can be activated via show_stats() or set_mode('stats')."""
        panel = self.win._details_panel
        self.assertFalse(panel.is_stats_active())

        # Switch via show_stats
        panel.show_stats()
        self.assertEqual(panel.current_mode(), "stats")
        self.assertTrue(panel.is_stats_active())
        self.assertEqual(panel._side_tabs.currentIndex(), 3)
        self.assertEqual(panel._lbl_title.text(), "Bandwidth Statistics")
        self.assertTrue(hasattr(panel, "_stats_view"))
        self.assertIsNotNone(panel._stats_view)

        # Subtabs in stats view: Volumes, Current Speed, Totals
        self.assertEqual(panel._stats_view._tabs.count(), 3)
        self.assertEqual(panel._stats_view._tabs.tabText(0), "Volumes")
        self.assertEqual(panel._stats_view._tabs.tabText(1), "Current Speed")
        self.assertEqual(panel._stats_view._tabs.tabText(2), "Totals")

        # Switch back to details
        panel.set_mode("details")
        self.assertFalse(panel.is_stats_active())
        self.assertEqual(panel.current_mode(), "details")


if __name__ == "__main__":
    unittest.main()


