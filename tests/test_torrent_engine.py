"""Unit tests for TorrentEngine: libtorrent session, alert handling, fastresume pairing, recheck, and stalled swarm detection."""

import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from unittest.mock import MagicMock, patch

from my_idm.database import Database, DownloadEntry
from my_idm.torrent_engine import TorrentEngine


class FakeSaveResumeDataAlert:
    def __init__(self, handle, params):
        self.handle = handle
        self.params = params

    def what(self):
        return "save_resume_data_alert"


class TestTorrentEngine(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test.db"
        self.db = Database(self.db_path)
        self.db.open()
        self.fastresume_dir = Path(self.tmp_dir.name) / "fastresume"
        self.fastresume_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.db.close()
        self.tmp_dir.cleanup()

    def test_alert_matching_to_correct_download_id(self):
        """save_resume_data_alert for handle 1 must NOT write into handle 2's fastresume."""
        with patch("my_idm.torrent_engine.FASTRESUME_DIR", self.fastresume_dir):
            te = TorrentEngine(self.db)
            te._running = True
            te._session = MagicMock()

            mock_h1 = MagicMock()
            mock_h1.is_valid.return_value = True
            del mock_h1.info_hashes
            mock_h1.info_hash.return_value = "1111111111111111111111111111111111111111"

            mock_h2 = MagicMock()
            mock_h2.is_valid.return_value = True
            del mock_h2.info_hashes
            mock_h2.info_hash.return_value = "2222222222222222222222222222222222222222"

            te._handles["t1_completed"] = mock_h1
            te._handles["t2_downloading"] = mock_h2

            with patch("my_idm.torrent_engine._HAS_LIBTORRENT", True), \
                 patch("my_idm.torrent_engine.lt") as mock_lt:
                mock_params = MagicMock()
                del mock_params.info_hashes
                mock_params.info_hash = "1111111111111111111111111111111111111111"
                mock_alert = FakeSaveResumeDataAlert(mock_h1, mock_params)

                mock_lt.write_resume_data_buf.return_value = b"resume_data_for_h1"
                mock_session = te._session
                mock_session.pop_alerts.return_value = [mock_alert]

                te._process_alerts()

                h1_file = self.fastresume_dir / "t1_completed.fastresume"
                h2_file = self.fastresume_dir / "t2_downloading.fastresume"
                self.assertTrue(h1_file.exists(), "Fastresume for handle 1 should be written")
                self.assertFalse(h2_file.exists(), "Fastresume for handle 2 should NOT be written")
                self.assertEqual(h1_file.read_bytes(), b"resume_data_for_h1")

    def test_bad_fastresume_with_mismatched_hash_is_rejected_on_add_torrent(self):
        """If a fastresume file contains data for another torrent hash, it is discarded."""
        with patch("my_idm.torrent_engine.FASTRESUME_DIR", self.fastresume_dir):
            te = TorrentEngine(self.db)
            te._running = True
            mock_session = MagicMock()
            te._session = mock_session

            bad_resume_file = self.fastresume_dir / "t2.fastresume"
            bad_resume_file.write_bytes(b"bad_corrupted_resume_from_t1")

            entry_t2 = DownloadEntry(
                id="t2",
                download_type="torrent",
                url="magnet:?xt=urn:btih:2222222222222222222222222222222222222222&dn=Torrent2",
                save_path="/downloads",
                status="downloading",
                filename="Torrent2",
            )
            self.db.add_download(entry_t2)

            with patch("my_idm.torrent_engine._HAS_LIBTORRENT", True), \
                 patch("my_idm.torrent_engine.lt") as mock_lt:
                magnet_params = MagicMock()
                del magnet_params.info_hashes
                magnet_params.info_hash = "2222222222222222222222222222222222222222"
                mock_lt.parse_magnet_uri.return_value = magnet_params

                bad_params = MagicMock()
                del bad_params.info_hashes
                bad_params.info_hash = "1111111111111111111111111111111111111111"
                mock_lt.read_resume_data.return_value = bad_params

                mock_handle = MagicMock()
                del mock_handle.info_hashes
                mock_handle.info_hash.return_value = "2222222222222222222222222222222222222222"
                mock_session.add_torrent.return_value = mock_handle

                success = te.add_torrent(entry_t2)
                self.assertTrue(success)
                mock_session.add_torrent.assert_called_once_with(magnet_params)
                self.assertFalse(bad_resume_file.exists())

    def test_three_torrents_shutdown_and_restart_preserves_identities(self):
        """Simulates 3 torrents (1 completed, 2 downloading), shutdown and restart preserving identity."""
        with patch("my_idm.torrent_engine.FASTRESUME_DIR", self.fastresume_dir):
            e1 = DownloadEntry(
                id="t1",
                download_type="torrent",
                url="magnet:?xt=urn:btih:1111111111111111111111111111111111111111&dn=Ubuntu.iso",
                filename="Ubuntu.iso",
                status="completed",
                total_size=1000,
                downloaded_size=1000,
                torrent_info_hash="1111111111111111111111111111111111111111",
            )
            e2 = DownloadEntry(
                id="t2",
                download_type="torrent",
                url="magnet:?xt=urn:btih:2222222222222222222222222222222222222222&dn=Debian.iso",
                filename="Debian.iso",
                status="downloading",
                total_size=2000,
                downloaded_size=200,
                torrent_info_hash="2222222222222222222222222222222222222222",
            )
            e3 = DownloadEntry(
                id="t3",
                download_type="torrent",
                url="magnet:?xt=urn:btih:3333333333333333333333333333333333333333&dn=Fedora.iso",
                filename="Fedora.iso",
                status="downloading",
                total_size=3000,
                downloaded_size=300,
                torrent_info_hash="3333333333333333333333333333333333333333",
            )
            self.db.add_download(e1)
            self.db.add_download(e2)
            self.db.add_download(e3)

            te1 = TorrentEngine(self.db)
            te1._running = True
            mock_sess1 = MagicMock()
            te1._session = mock_sess1

            h1, h2, h3 = MagicMock(), MagicMock(), MagicMock()
            for h, hash_val in [(h1, "1111111111111111111111111111111111111111"),
                                (h2, "2222222222222222222222222222222222222222"),
                                (h3, "3333333333333333333333333333333333333333")]:
                h.is_valid.return_value = True
                del h.info_hashes
                h.info_hash.return_value = hash_val

            te1._handles["t1"] = h1
            te1._handles["t2"] = h2
            te1._handles["t3"] = h3

            alerts = []
            for h, hash_val in [(h1, "1111111111111111111111111111111111111111"),
                                (h2, "2222222222222222222222222222222222222222"),
                                (h3, "3333333333333333333333333333333333333333")]:
                p = MagicMock()
                del p.info_hashes
                p.info_hash = hash_val
                alt = FakeSaveResumeDataAlert(h, p)
                alerts.append(alt)

            mock_sess1.pop_alerts.side_effect = [alerts, []]

            with patch("my_idm.torrent_engine._HAS_LIBTORRENT", True), \
                 patch("my_idm.torrent_engine.lt") as mock_lt:
                mock_lt.save_resume_data_alert = FakeSaveResumeDataAlert
                mock_lt.write_resume_data_buf.side_effect = lambda p: f"resume_{p.info_hash}".encode()
                te1.stop()

            # Check fastresume files match their own torrents
            self.assertEqual((self.fastresume_dir / "t1.fastresume").read_bytes(), b"resume_1111111111111111111111111111111111111111")
            self.assertEqual((self.fastresume_dir / "t2.fastresume").read_bytes(), b"resume_2222222222222222222222222222222222222222")
            self.assertEqual((self.fastresume_dir / "t3.fastresume").read_bytes(), b"resume_3333333333333333333333333333333333333333")

            # App restart
            te2 = TorrentEngine(self.db)
            te2._running = True
            mock_sess2 = MagicMock()
            te2._session = mock_sess2

            with patch("my_idm.torrent_engine._HAS_LIBTORRENT", True), \
                 patch("my_idm.torrent_engine.lt") as mock_lt:
                def fake_read_resume(buf_bytes):
                    p = MagicMock()
                    del p.info_hashes
                    p.info_hash = buf_bytes.decode().replace("resume_", "")
                    return p

                mock_lt.read_resume_data.side_effect = fake_read_resume
                mock_lt.parse_magnet_uri.side_effect = lambda url: fake_read_resume(url.split("btih:")[1].split("&")[0].encode())

                h2_new = MagicMock()
                del h2_new.info_hashes
                h2_new.info_hash.return_value = "2222222222222222222222222222222222222222"

                h3_new = MagicMock()
                del h3_new.info_hashes
                h3_new.info_hash.return_value = "3333333333333333333333333333333333333333"

                mock_sess2.add_torrent.side_effect = [h2_new, h3_new]

                te2.add_torrent(self.db.get_download("t2"))
                te2.add_torrent(self.db.get_download("t3"))

                db_t1 = self.db.get_download("t1")
                db_t2 = self.db.get_download("t2")
                db_t3 = self.db.get_download("t3")

                self.assertEqual(db_t1.filename, "Ubuntu.iso")
                self.assertEqual(db_t2.filename, "Debian.iso")
                self.assertEqual(db_t3.filename, "Fedora.iso")
                self.assertEqual(db_t2.torrent_info_hash, "2222222222222222222222222222222222222222")
                self.assertEqual(db_t3.torrent_info_hash, "3333333333333333333333333333333333333333")

    def test_torrent_recheck_transitions_out_of_checking(self):
        """When checking completes with 0 peers/seeds, status transitions to downloading."""
        te = TorrentEngine(self.db)
        te._running = True
        te._session = MagicMock()

        entry = DownloadEntry(
            id="t_check",
            download_type="torrent",
            status="checking",
            total_size=10000,
            downloaded_size=0,
        )
        self.db.add_download(entry)

        mock_handle = MagicMock()
        mock_handle.status.return_value.paused = False
        mock_handle.status.return_value.is_paused = False
        te._handles["t_check"] = mock_handle

        status_updates = []
        te.set_callbacks(None, lambda did, stat, err: status_updates.append((did, stat)))

        with patch.object(te, "get_status", return_value={
            "total_size": 10000,
            "downloaded": 0,
            "progress": 0.0,
            "state": "downloading",
            "speed": 0.0,
            "upload_speed": 0.0,
            "seeds": 0,
            "peers": 0,
            "eta": 0,
            "name": "test_torrent",
        }):
            te.poll_all()

        updated = self.db.get_download("t_check")
        self.assertEqual(updated.status, "downloading")
        self.assertEqual(status_updates, [("t_check", "downloading")])

    def test_stalled_torrent_detection(self):
        """Downloads with zero seeds and zero speed for >45s are marked stalled and reannounced."""
        te = TorrentEngine(self.db)
        te._running = True
        te._session = MagicMock()

        entry = DownloadEntry(id="t_stall", download_type="torrent", status="downloading", total_size=50000, downloaded_size=1000)
        self.db.add_download(entry)

        mock_handle = MagicMock()
        mock_handle.status.return_value.is_paused = False
        te._handles["t_stall"] = mock_handle

        import time
        te._last_active_time["t_stall"] = time.monotonic() - 60.0

        status_updates = []
        te.set_callbacks(None, lambda did, stat, err: status_updates.append((did, stat)))

        with patch.object(te, "get_status", return_value={
            "total_size": 50000,
            "downloaded": 1000,
            "progress": 2.0,
            "state": "downloading",
            "speed": 0.0,
            "upload_speed": 0.0,
            "seeds": 0,
            "peers": 0,
            "eta": 0,
            "name": "test_stall",
        }):
            te.poll_all()

        self.assertEqual(self.db.get_download("t_stall").status, "stalled")
        mock_handle.force_reannounce.assert_called_once()

    def test_torrent_engine_guards_against_http_downloads(self):
        """Torrent engine immediately returns False for non-torrent entries and file priority changes."""
        te = TorrentEngine(self.db)
        te._running = True
        te._session = MagicMock()

        http_entry = DownloadEntry(
            id="http_stream",
            url="https://vault-99.owocdn.top/mp4/123?file=video.mp4",
            download_type="http",
            status="downloading",
        )
        self.db.add_download(http_entry)

        # add_torrent should immediately reject non-torrent entry without making network requests
        self.assertFalse(te.add_torrent(http_entry))

        # set_torrent_file_priority should reject non-torrent entry
        self.assertFalse(te.set_torrent_file_priority("http_stream", 0, 1))

    def test_rename_root_updates_handle_and_disk(self):
        """rename_root renames files in handle and moves directory on disk."""
        te = TorrentEngine(self.db)
        te._running = True
        te._session = MagicMock()

        old_folder = Path(self.tmp_dir.name) / "OldTorrentDir"
        old_folder.mkdir(parents=True, exist_ok=True)
        (old_folder / "file1.txt").write_text("Test", encoding="utf-8")

        entry = DownloadEntry(
            id="t_ren",
            url="magnet:?xt=urn:btih:1111222233334444&dn=OldTorrentDir",
            filename="OldTorrentDir",
            save_path=self.tmp_dir.name,
            file_path=str(old_folder),
            status="completed",
            download_type="torrent",
            metadata_json='{"files": [{"index": 0, "path": "OldTorrentDir/file1.txt", "name": "file1.txt"}]}',
        )
        self.db.add_download(entry)

        mock_handle = MagicMock()
        mock_ti = MagicMock()
        mock_ti.num_files.return_value = 1
        mock_files = MagicMock()
        mock_files.file_path.return_value = "OldTorrentDir/file1.txt"
        mock_ti.files.return_value = mock_files
        mock_handle.torrent_file.return_value = mock_ti
        te._handles["t_ren"] = mock_handle

        success = te.rename_root("t_ren", "NewTorrentDir")
        self.assertTrue(success)

        # Handle calls
        mock_handle.rename_file.assert_called_once_with(0, "NewTorrentDir/file1.txt")
        mock_handle.save_resume_data.assert_called_once()

        # Disk move
        self.assertFalse(old_folder.exists())
        new_folder = Path(self.tmp_dir.name) / "NewTorrentDir"
        self.assertTrue(new_folder.exists())
        self.assertTrue((new_folder / "file1.txt").exists())

    def test_add_torrent_preserves_completed_and_seeding_status(self):
        """add_torrent never sets fetching_metadata on completed or seeding torrents."""
        te = TorrentEngine(self.db)
        te._running = True
        mock_session = MagicMock()
        mock_handle = MagicMock()
        mock_session.add_torrent.return_value = mock_handle
        te._session = mock_session

        entry = DownloadEntry(
            id="t_comp",
            url="magnet:?xt=urn:btih:5555666677778888&dn=CompletedTorrent",
            filename="CompletedTorrent",
            save_path=self.tmp_dir.name,
            status="completed",
            download_type="torrent",
        )
        self.db.add_download(entry)

        with patch("my_idm.torrent_engine._HAS_LIBTORRENT", True), \
             patch("my_idm.torrent_engine.lt") as mock_lt:
            mock_lt.parse_magnet_uri.return_value = MagicMock()
            ok = te.add_torrent(entry)
            self.assertTrue(ok)

        # Handle paused, status preserved as completed
        mock_handle.pause.assert_called_once()
        db_entry = self.db.get_download("t_comp")
        self.assertEqual(db_entry.status, "completed")
        self.assertNotEqual(db_entry.status, "fetching_metadata")

    def test_poll_all_does_not_override_completed_torrent_with_metadata(self):
        """poll_all ignores downloading_metadata state if entry is already completed."""
        te = TorrentEngine(self.db)
        te._running = True
        te._session = MagicMock()

        entry = DownloadEntry(
            id="t_comp_poll",
            url="magnet:?xt=urn:btih:9999888877776666&dn=Finished",
            filename="Finished",
            status="completed",
            download_type="torrent",
            total_size=1000,
            downloaded_size=1000,
        )
        self.db.add_download(entry)

        mock_handle = MagicMock()
        te._handles["t_comp_poll"] = mock_handle

        with patch.object(te, "get_status", return_value={
            "total_size": 1000,
            "downloaded": 1000,
            "progress": 100.0,
            "state": "downloading_metadata",
            "speed": 0.0,
            "upload_speed": 0.0,
            "seeds": 0,
            "peers": 0,
            "eta": 0,
            "name": "Finished",
        }):
            te.poll_all()

        db_entry = self.db.get_download("t_comp_poll")
        self.assertEqual(db_entry.status, "completed")

    def test_poll_all_respects_explicit_filename_over_torrent_name(self):
        """poll_all does not revert renamed torrent to swarm metadata name."""
        te = TorrentEngine(self.db)
        te._running = True
        te._session = MagicMock()

        entry = DownloadEntry(
            id="t_explicit",
            url="magnet:?xt=urn:btih:1234123412341234&dn=OriginalSwarmName",
            filename="UserRenamedFolder",
            save_path=self.tmp_dir.name,
            status="downloading",
            download_type="torrent",
            total_size=5000,
            downloaded_size=2500,
            metadata_json='{"explicit_filename": true}',
        )
        self.db.add_download(entry)

        mock_handle = MagicMock()
        te._handles["t_explicit"] = mock_handle

        with patch.object(te, "get_status", return_value={
            "total_size": 5000,
            "downloaded": 2500,
            "progress": 50.0,
            "state": "downloading",
            "speed": 100.0,
            "upload_speed": 0.0,
            "seeds": 5,
            "peers": 10,
            "eta": 25,
            "name": "OriginalSwarmName",
        }):
            te.poll_all()

        db_entry = self.db.get_download("t_explicit")
        self.assertEqual(db_entry.filename, "UserRenamedFolder")

    def test_get_torrent_peers_flag_and_client_formatting(self):
        """get_torrent_peers properly translates peer flags and handles client strings."""
        te = TorrentEngine(self.db)
        te._running = True

        mock_handle = MagicMock()
        mock_handle.is_valid.return_value = True

        # Mock a peer with seed, downloading, encrypted flags
        p1 = MagicMock()
        p1.ip = ("10.0.0.1", 6881)
        p1.client = b"Transmission/3.00"
        p1.progress = 1.0
        p1.down_speed = 50000
        p1.up_speed = 0
        p1.seed = True
        p1.upload_only = True
        p1.choked = False
        p1.interesting = True
        p1.remote_choked = True
        p1.remote_interested = False
        p1.optimistic_unchoke = False
        p1.snubbed = False
        p1.rc4_encrypted = True
        p1.plaintext_encrypted = False
        p1.dht = True
        p1.pex = False
        p1.outgoing_connection = True
        p1.local_connection = False

        # Mock a peer with blank client (b'') and incoming uploading
        p2 = MagicMock()
        p2.ip = ("10.0.0.2", 51413)
        p2.client = b""
        p2.progress = 0.5
        p2.down_speed = 0
        p2.up_speed = 100000
        p2.seed = False
        p2.upload_only = False
        p2.choked = True
        p2.interesting = False
        p2.remote_choked = False
        p2.remote_interested = True
        p2.optimistic_unchoke = True
        p2.snubbed = False
        p2.rc4_encrypted = False
        p2.plaintext_encrypted = False
        p2.dht = False
        p2.pex = True
        p2.outgoing_connection = False
        p2.local_connection = False

        mock_handle.get_peer_info.return_value = [p1, p2]
        te._handles["t_peer_test"] = mock_handle

        peers = te.get_torrent_peers("t_peer_test")
        self.assertEqual(len(peers), 2)

        # Peer 1: Transmission/3.00, flags include S (Seed), D (Downloading), E (Encrypted), H (DHT)
        self.assertEqual(peers[0]["ip"], "10.0.0.1:6881")
        self.assertEqual(peers[0]["client"], "Transmission/3.00")
        self.assertIn("S", peers[0]["flags"])
        self.assertIn("D", peers[0]["flags"])
        self.assertIn("E", peers[0]["flags"])
        self.assertIn("H", peers[0]["flags"])

        # Peer 2: Client falls back to Unknown, flags include U (Uploading), O (Optimistic), X (PEX), I (Incoming)
        self.assertEqual(peers[1]["ip"], "10.0.0.2:51413")
        self.assertEqual(peers[1]["client"], "Unknown")
        self.assertIn("U", peers[1]["flags"])
        self.assertIn("O", peers[1]["flags"])
        self.assertIn("X", peers[1]["flags"])
        self.assertIn("I", peers[1]["flags"])

    def test_fetching_metadata_timeout_suspends_and_clears_queue_order(self):
        """Torrent in fetching_metadata for more than configured timeout becomes suspended with queue order 0."""
        from datetime import datetime, timezone, timedelta
        from my_idm.config import GeneralConfig
        te = TorrentEngine(self.db)
        te._running = True
        te._session = MagicMock()
        cfg = GeneralConfig(metadata_fetch_timeout_days=1)
        te.set_general_config(cfg)

        two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        entry = DownloadEntry(
            id="t_timeout",
            url="magnet:?xt=urn:btih:aabbccddeeff1122&dn=StuckTorrent",
            filename="StuckTorrent",
            status="fetching_metadata",
            download_type="torrent",
            queue_order=5,
            fetching_metadata_since=two_days_ago,
        )
        self.db.add_download(entry)

        mock_handle = MagicMock()
        mock_handle.status.return_value = MagicMock(paused=False, is_paused=False)
        te._handles["t_timeout"] = mock_handle

        status_reports = []
        te._status_cb = lambda did, st, msg: status_reports.append((did, st))

        with patch.object(te, "get_status", return_value={
            "total_size": 0, "downloaded": 0, "progress": 0.0,
            "state": "downloading_metadata", "speed": 0.0, "upload_speed": 0.0,
            "seeds": 0, "peers": 0, "eta": 0, "name": "",
        }):
            te.poll_all()

        updated = self.db.get_download("t_timeout")
        self.assertEqual(updated.status, "suspended")
        self.assertEqual(updated.queue_order, 0)
        self.assertEqual(updated.fetching_metadata_since, "")
        mock_handle.pause.assert_called()
        self.assertIn(("t_timeout", "suspended"), status_reports)

    def test_fetching_metadata_timer_persists_across_restart(self):
        """When an interrupted fetching_metadata torrent is resumed on startup, fetching_metadata_since is preserved."""
        from datetime import datetime, timezone, timedelta
        from my_idm.manager import DownloadManager
        from my_idm.config import GeneralConfig, TorConfig
        from my_idm.network import NetworkConfig
        from my_idm.security import SecurityConfig

        start_time = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        entry = DownloadEntry(
            id="t_resume_timer",
            url="magnet:?xt=urn:btih:1122334455667788&dn=OngoingMeta",
            filename="OngoingMeta",
            status="fetching_metadata",
            download_type="torrent",
            queue_order=3,
            fetching_metadata_since=start_time,
        )
        self.db.add_download(entry)

        mgr = DownloadManager(self.db)
        with patch.object(mgr, "_start_entry"):
            mgr.resume_download("t_resume_timer")

        updated = self.db.get_download("t_resume_timer")
        # fetching_metadata_since must NOT be wiped
        self.assertEqual(updated.fetching_metadata_since, start_time)

    def test_manual_resume_of_suspended_torrent_resets_timer(self):
        """Manually resuming a suspended torrent resets fetching_metadata_since."""
        from datetime import datetime, timezone, timedelta
        from my_idm.manager import DownloadManager

        start_time = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        entry = DownloadEntry(
            id="t_manual_resume",
            url="magnet:?xt=urn:btih:3344556677889900&dn=SuspendedTorrent",
            filename="SuspendedTorrent",
            status="suspended",
            download_type="torrent",
            queue_order=0,
            fetching_metadata_since=start_time,
        )
        self.db.add_download(entry)

        mgr = DownloadManager(self.db)
        with patch.object(mgr, "_start_entry"):
            mgr.resume_download("t_manual_resume")

        updated = self.db.get_download("t_manual_resume")
        self.assertEqual(updated.fetching_metadata_since, "")
        self.assertGreater(updated.queue_order, 0)
        self.assertEqual(updated.status, "queued")

    def test_successful_progress_clears_fetching_metadata_timer(self):
        """When torrent receives metadata or progresses, fetching_metadata_since is cleared."""
        from datetime import datetime, timezone, timedelta
        te = TorrentEngine(self.db)
        te._running = True
        te._session = MagicMock()

        two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        entry = DownloadEntry(
            id="t_prog_clear",
            url="magnet:?xt=urn:btih:4455667788990011&dn=ResolvedTorrent",
            filename="ResolvedTorrent",
            status="fetching_metadata",
            download_type="torrent",
            fetching_metadata_since=two_hours_ago,
        )
        self.db.add_download(entry)

        mock_handle = MagicMock()
        mock_handle.status.return_value = MagicMock(paused=False, is_paused=False)
        te._handles["t_prog_clear"] = mock_handle

        with patch.object(te, "get_status", return_value={
            "total_size": 1000, "downloaded": 100, "progress": 10.0,
            "state": "downloading", "speed": 50000.0, "upload_speed": 0.0,
            "seeds": 5, "peers": 10, "eta": 18, "name": "ResolvedTorrent",
        }):
            te.poll_all()

        updated = self.db.get_download("t_prog_clear")
        self.assertEqual(updated.status, "downloading")
        self.assertEqual(updated.fetching_metadata_since, "")

    def test_torrent_transitions_to_seeding_on_completion(self):
        """When seeding_after_complete is True, completed torrent transitions to seeding."""
        from my_idm.config import TorrentConfig
        te = TorrentEngine(self.db)
        te._running = True
        te._session = MagicMock()
        te.set_torrent_config(TorrentConfig(seeding_after_complete=True, max_seeding_speed=200))

        entry = DownloadEntry(
            id="t_seeding",
            url="magnet:?xt=urn:btih:5566778899001122&dn=CompleteTorrent",
            filename="CompleteTorrent",
            status="downloading",
            download_type="torrent",
        )
        self.db.add_download(entry)

        mock_handle = MagicMock()
        mock_handle.status.return_value = MagicMock(paused=False, is_paused=False)
        mock_handle.is_valid.return_value = True
        te._handles["t_seeding"] = mock_handle

        status_reports = []
        te.set_callbacks(
            lambda *args: None,
            lambda did, status, err: status_reports.append((did, status)),
        )

        with patch.object(te, "get_status", return_value={
            "total_size": 2048, "downloaded": 2048, "progress": 100.0,
            "state": "seeding", "speed": 0.0, "upload_speed": 50000.0,
            "seeds": 10, "peers": 20, "eta": 0, "name": "CompleteTorrent",
        }), patch.object(te, "get_torrent_files", return_value=[]), \
           patch.object(te, "get_torrent_trackers", return_value=[]):
            te.poll_all()

        updated = self.db.get_download("t_seeding")
        self.assertEqual(updated.status, "seeding")
        self.assertIn(("t_seeding", "seeding"), status_reports)
        mock_handle.set_upload_limit.assert_called_with(200 * 1024)

    def test_torrent_transitions_to_completed_when_seeding_disabled(self):
        """When seeding_after_complete is False, finished torrent transitions to completed."""
        from my_idm.config import TorrentConfig
        te = TorrentEngine(self.db)
        te._running = True
        te._session = MagicMock()
        te.set_torrent_config(TorrentConfig(seeding_after_complete=False))

        entry = DownloadEntry(
            id="t_completed_no_seed",
            url="magnet:?xt=urn:btih:6677889900112233&dn=NoSeedTorrent",
            filename="NoSeedTorrent",
            status="downloading",
            download_type="torrent",
        )
        self.db.add_download(entry)

        mock_handle = MagicMock()
        mock_handle.status.return_value = MagicMock(paused=False, is_paused=False)
        mock_handle.is_valid.return_value = True
        te._handles["t_completed_no_seed"] = mock_handle

        with patch.object(te, "get_status", return_value={
            "total_size": 2048, "downloaded": 2048, "progress": 100.0,
            "state": "finished", "speed": 0.0, "upload_speed": 0.0,
            "seeds": 10, "peers": 20, "eta": 0, "name": "NoSeedTorrent",
        }), patch.object(te, "get_torrent_files", return_value=[]), \
           patch.object(te, "get_torrent_trackers", return_value=[]):
            te.poll_all()

        updated = self.db.get_download("t_completed_no_seed")
        self.assertEqual(updated.status, "completed")
        mock_handle.pause.assert_called()

    def test_seeding_speed_limit_ratio(self):
        """Seeding upload limit is derived from network download limit and ratio."""
        from my_idm.config import TorrentConfig
        from my_idm.network import NetworkConfig
        te = TorrentEngine(self.db)
        te._running = True
        te._session = MagicMock()
        # 1,000,000 B/s download limit / 2.0 ratio = 500,000 B/s upload limit
        te.apply_network_config(NetworkConfig(download_limit=1_000_000))
        te.set_torrent_config(TorrentConfig(seeding_after_complete=True, max_seeding_speed=0, download_to_seeding_ratio=2.0))

        entry = DownloadEntry(
            id="t_ratio",
            url="magnet:?xt=urn:btih:7788990011223344&dn=RatioTorrent",
            filename="RatioTorrent",
            status="seeding",
            download_type="torrent",
        )
        self.db.add_download(entry)

        mock_handle = MagicMock()
        mock_handle.is_valid.return_value = True
        te._handles["t_ratio"] = mock_handle

        te._apply_seeding_limits()
        mock_handle.set_upload_limit.assert_called_with(500_000)


if __name__ == "__main__":
    unittest.main()

