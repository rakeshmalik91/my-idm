"""Unit tests for DownloadManager lifecycle: startup auto-resume, queue ordering, error retries, and recheck verification."""

import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from unittest.mock import patch, MagicMock
from PySide6.QtWidgets import QApplication

from my_idm.database import Database, DownloadEntry, SegmentEntry
from my_idm.download_model import DownloadTableModel, Col
from my_idm.manager import DownloadManager

app = QApplication.instance() or QApplication([])


class TestManagerLifecycle(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test.db"
        self.db = Database(self.db_path)
        self.db.open()
        self.manager = DownloadManager(self.db)

    def tearDown(self):
        self.manager.stop()
        self.db.close()
        self.tmp_dir.cleanup()

    # -- Startup and shutdown ------------------------------------------------

    def test_startup_resumes_queued_and_interrupted_downloads(self):
        """Startup automatically resumes interrupted downloads and queued items."""
        e_active = DownloadEntry(id="d1", url="http://example.com/1.zip", filename="1.zip", save_path="/tmp", status="downloading")
        e_queued = DownloadEntry(id="d2", url="http://example.com/2.zip", filename="2.zip", save_path="/tmp", status="queued")
        e_paused = DownloadEntry(id="d3", url="http://example.com/3.zip", filename="3.zip", save_path="/tmp", status="paused")
        self.db.add_download(e_active)
        self.db.add_download(e_queued)
        self.db.add_download(e_paused)

        resumed = []
        with patch.object(self.manager, "resume_download", side_effect=lambda did: resumed.append(did)):
            self.manager.start()

        self.assertIn("d1", resumed)
        self.assertIn("d2", resumed)
        self.assertNotIn("d3", resumed)

    def test_clean_shutdown_marks_active_as_queued_for_auto_resume(self):
        """Active HTTP downloads are set to queued on stop() so next startup auto-resumes them."""
        import asyncio
        entry = DownloadEntry(
            id="dl-active-1",
            url="https://example.com/video.mp4",
            filename="video.mp4",
            save_path="C:/Downloads",
            status="downloading",
        )
        self.db.add_download(entry)

        async def run_test():
            self.manager._http._tasks["dl-active-1"] = asyncio.create_task(asyncio.sleep(10))
            await self.manager._http.stop()

        asyncio.run(run_test())
        updated = self.db.get_download("dl-active-1")
        self.assertEqual(updated.status, "queued")

    # -- Retries and resume --------------------------------------------------

    def test_manual_resume_resets_exhausted_retries(self):
        """Resuming an errored download resets retry_count to 0 and clears error message."""
        entry = DownloadEntry(
            id="dl-error-1",
            url="https://example.com/file.zip",
            filename="file.zip",
            save_path="C:/Downloads",
            status="error",
            retry_count=5,
            error_message="Single-stream download failed after 5 retries",
        )
        self.db.add_download(entry)
        self.assertEqual(self.db.get_download("dl-error-1").retry_count, 5)

        self.manager.resume_download("dl-error-1")
        updated = self.db.get_download("dl-error-1")
        self.assertEqual(updated.retry_count, 0)
        self.assertEqual(updated.error_message, "")
        self.assertIn(updated.status, ("queued", "downloading"))

    def test_manual_resume_paused_download(self):
        """Resuming a paused download changes state to queued/downloading."""
        entry = DownloadEntry(
            id="dl-paused-1",
            url="https://example.com/archive.tar",
            filename="archive.tar",
            save_path="C:/Downloads",
            status="paused",
        )
        self.db.add_download(entry)
        self.manager.resume_download("dl-paused-1")
        updated = self.db.get_download("dl-paused-1")
        self.assertIn(updated.status, ("queued", "downloading"))

    def test_force_start_download_lifecycle(self):
        """Force start immediately resets error/retries and sets status to downloading."""
        entry = DownloadEntry(
            id="dl-force-1",
            url="https://example.com/archive.zip",
            filename="archive.zip",
            save_path="C:/Downloads",
            status="error",
            retry_count=4,
            error_message="Connection timed out",
            download_type="http",
            total_size=1000,
        )
        self.db.add_download(entry)

        status_emitted = []
        self.manager.status_changed.connect(lambda did, st, msg: status_emitted.append((did, st)))

        self.manager.force_start_download("dl-force-1")
        updated = self.db.get_download("dl-force-1")
        self.assertEqual(updated.status, "downloading")
        self.assertEqual(updated.retry_count, 0)
        self.assertEqual(updated.error_message, "")
        self.assertIn(("dl-force-1", "downloading"), status_emitted)

    # -- Queue management ----------------------------------------------------

    def test_move_queue_up_and_down(self):
        """Move up and down swaps queue orders and emits queue_order_changed."""
        e1 = DownloadEntry(id="d1", url="http://example.com/1.zip", filename="1.zip", save_path="/tmp")
        e2 = DownloadEntry(id="d2", url="http://example.com/2.zip", filename="2.zip", save_path="/tmp")
        self.db.add_download(e1)
        self.db.add_download(e2)

        signals = []
        self.manager.queue_order_changed.connect(lambda: signals.append(True))

        self.manager.move_queue_up("d2")
        self.assertTrue(len(signals) > 0)
        self.assertEqual(self.db.get_download("d2").queue_order, 1)
        self.assertEqual(self.db.get_download("d1").queue_order, 2)

        self.manager.move_queue_down("d2")
        self.assertEqual(self.db.get_download("d2").queue_order, 2)
        self.assertEqual(self.db.get_download("d1").queue_order, 1)

    # -- File not found ------------------------------------------------------

    def test_mark_file_not_found(self):
        """mark_file_not_found sets status to 'file_not_found' and updates in DB."""
        e = DownloadEntry(id="d1", url="http://example.com/f1.zip", filename="f1.zip", save_path="/tmp", status="completed")
        self.db.add_download(e)

        statuses = []
        self.manager.status_changed.connect(lambda did, st, err: statuses.append((did, st)))
        self.manager.mark_file_not_found("d1")

        self.assertIn(("d1", "file_not_found"), statuses)
        self.assertEqual(self.db.get_download("d1").status, "file_not_found")

    # -- Recheck engine ------------------------------------------------------

    def test_recheck_missing_file_resets_progress_to_zero(self):
        """Recheck on a deleted file resets downloaded_size to 0 and status to queued."""
        test_file = Path(self.tmp_dir.name) / "deleted.zip"
        entry = DownloadEntry(
            id="recheck-deleted",
            url="https://example.com/deleted.zip",
            filename="deleted.zip",
            file_path=str(test_file),
            save_path=self.tmp_dir.name,
            total_size=1_000_000,
            downloaded_size=1_000_000,
            status="completed",
            download_type="http",
        )
        self.db.add_download(entry)

        status_events = []
        progress_events = []
        self.manager.status_changed.connect(lambda did, st, err: status_events.append((did, st, err)))
        self.manager.progress_updated.connect(lambda did, dl, total, *_: progress_events.append((did, dl, total)))

        self.manager.recheck_download("recheck-deleted")

        self.assertEqual(len(status_events), 1)
        self.assertEqual(status_events[0], ("recheck-deleted", "queued", "File not found"))
        self.assertEqual(progress_events[0][1], 0)

        updated = self.db.get_download("recheck-deleted")
        self.assertEqual(updated.downloaded_size, 0)
        self.assertEqual(updated.status, "queued")

    def test_recheck_partial_preallocated_file_updates_from_segments(self):
        """Recheck with pre-allocated full size file reads actual written bytes from segments."""
        test_file = Path(self.tmp_dir.name) / "partial.bin"
        test_file.write_bytes(b"\x00" * 1_000_000)

        entry = DownloadEntry(
            id="recheck-partial",
            url="https://example.com/partial.bin",
            filename="partial.bin",
            file_path=str(test_file),
            save_path=self.tmp_dir.name,
            total_size=1_000_000,
            downloaded_size=1_000_000,
            status="completed",
            download_type="http",
        )
        self.db.add_download(entry)

        segs = [
            SegmentEntry(id="s1", download_id="recheck-partial", index=0,
                         start_byte=0, end_byte=499_999,
                         downloaded_bytes=500_000, status="completed"),
            SegmentEntry(id="s2", download_id="recheck-partial", index=1,
                         start_byte=500_000, end_byte=999_999,
                         downloaded_bytes=0, status="pending"),
        ]
        self.db.add_segments(segs)

        self.manager.recheck_download("recheck-partial")
        updated = self.db.get_download("recheck-partial")
        self.assertEqual(updated.downloaded_size, 500_000)
        self.assertEqual(updated.status, "paused")

    def test_recheck_complete_file_confirms_completed(self):
        """Recheck with all segments completed confirms completed status."""
        test_file = Path(self.tmp_dir.name) / "complete.bin"
        test_file.write_bytes(b"B" * 1_000_000)

        entry = DownloadEntry(
            id="recheck-complete",
            url="https://example.com/complete.bin",
            filename="complete.bin",
            file_path=str(test_file),
            save_path=self.tmp_dir.name,
            total_size=1_000_000,
            downloaded_size=900_000,
            status="paused",
            download_type="http",
        )
        self.db.add_download(entry)

        segs = [
            SegmentEntry(id="c1", download_id="recheck-complete", index=0,
                         start_byte=0, end_byte=499_999,
                         downloaded_bytes=500_000, status="completed"),
            SegmentEntry(id="c2", download_id="recheck-complete", index=1,
                         start_byte=500_000, end_byte=999_999,
                         downloaded_bytes=500_000, status="completed"),
        ]
        self.db.add_segments(segs)

        self.manager.recheck_download("recheck-complete")
        updated = self.db.get_download("recheck-complete")
        self.assertEqual(updated.status, "completed")

    def test_recheck_resets_file_not_found(self):
        """Rechecking a download that had file_not_found status resets it."""
        e = DownloadEntry(id="d1", url="http://example.com/f1.zip", filename="f1.zip", save_path="/tmp", status="file_not_found")
        self.db.add_download(e)

        with patch.object(self.manager, "resume_download"):
            self.manager.recheck_download("d1")

        self.assertNotEqual(self.db.get_download("d1").status, "file_not_found")

    def test_recheck_file_not_found_model_progress_resets(self):
        """Model in-memory entry reflects 0 progress after recheck of missing file."""
        test_file = Path(self.tmp_dir.name) / "missing.pdf"
        entry = DownloadEntry(
            id="recheck-model",
            url="https://example.com/missing.pdf",
            filename="missing.pdf",
            file_path=str(test_file),
            save_path=self.tmp_dir.name,
            total_size=2_000_000,
            downloaded_size=2_000_000,
            status="completed",
            download_type="http",
        )
        self.db.add_download(entry)

        model = DownloadTableModel()
        model.load_entries([entry])
        self.manager.status_changed.connect(lambda did, st, err: model.update_status(did, st, err))
        self.manager.progress_updated.connect(
            lambda did, dl, total, spd, eta, s, p, up: model.update_progress(did, dl, total, spd, eta, s, p, up)
        )

        row = model._id_to_row["recheck-model"]
        prog_before = model.data(model.index(row, Col.PROGRESS))
        self.assertAlmostEqual(prog_before["progress"], 100.0)

        self.manager.recheck_download("recheck-model")
        prog_after = model.data(model.index(row, Col.PROGRESS))
        self.assertAlmostEqual(prog_after["progress"], 0.0)
        self.assertEqual(model.data(model.index(row, Col.STATUS)), "Queued")

    def test_delete_download_file_keeps_entry_and_resets_progress(self):
        """delete_download_file deletes disk file, pauses download, and resets progress to 0 while keeping entry."""
        test_file = Path(self.tmp_dir.name) / "to_delete.iso"
        test_file.write_bytes(b"sample bytes data" * 100)
        self.assertTrue(test_file.exists())

        entry = DownloadEntry(
            id="del-file-1",
            url="https://example.com/to_delete.iso",
            filename="to_delete.iso",
            file_path=str(test_file),
            save_path=self.tmp_dir.name,
            total_size=1000,
            downloaded_size=1000,
            status="completed",
            download_type="http",
        )
        self.db.add_download(entry)

        self.manager.delete_download_file("del-file-1")

        # File is gone from disk
        self.assertFalse(test_file.exists())

        # Entry remains in DB, paused, progress 0
        updated = self.db.get_download("del-file-1")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.status, "paused")
        self.assertEqual(updated.downloaded_size, 0)

    def test_delete_download_with_delete_files_moves_to_trash(self):
        """delete_download(..., delete_files=True) removes entry from DB and moves files to trash."""
        test_file = Path(self.tmp_dir.name) / "http_trash.iso"
        test_file.write_bytes(b"http payload data" * 50)
        self.assertTrue(test_file.exists())

        entry = DownloadEntry(
            id="del-trash-1",
            url="https://example.com/http_trash.iso",
            filename="http_trash.iso",
            file_path=str(test_file),
            save_path=self.tmp_dir.name,
            total_size=500,
            downloaded_size=500,
            status="completed",
            download_type="http",
        )
        self.db.add_download(entry)

        self.manager.delete_download("del-trash-1", delete_files=True)

        # File removed from disk (sent to trash)
        self.assertFalse(test_file.exists())
        # Removed from DB
        self.assertIsNone(self.db.get_download("del-trash-1"))

    def test_delete_download_without_delete_files_keeps_disk_file(self):
        """delete_download(..., delete_files=False) removes entry from DB but preserves files on disk."""
        test_file = Path(self.tmp_dir.name) / "keep_file.iso"
        test_file.write_bytes(b"keep me" * 50)
        self.assertTrue(test_file.exists())

        entry = DownloadEntry(
            id="del-keep-1",
            url="https://example.com/keep_file.iso",
            filename="keep_file.iso",
            file_path=str(test_file),
            save_path=self.tmp_dir.name,
            total_size=500,
            downloaded_size=500,
            status="completed",
            download_type="http",
        )
        self.db.add_download(entry)

        self.manager.delete_download("del-keep-1", delete_files=False)

        # File remains on disk
        self.assertTrue(test_file.exists())
        # Removed from DB
        self.assertIsNone(self.db.get_download("del-keep-1"))

    def test_detect_type_web_torrent_url(self):
        """Manager identifies http/https URLs pointing to .torrent files as torrent type."""
        self.assertEqual(self.manager._detect_type("https://releases.ubuntu.com/22.04/ubuntu-22.04.iso.torrent"), "torrent")
        self.assertEqual(self.manager._detect_type("http://example.org/download?file=debian.torrent"), "torrent")
        self.assertEqual(self.manager._detect_type("ftp://ftp.example.com/pub/distro.torrent"), "torrent")
        self.assertEqual(self.manager._detect_type("https://example.com/ubuntu-22.04.iso"), "http")
        self.assertEqual(self.manager._detect_type("magnet:?xt=urn:btih:1234567890"), "torrent")

    # -- Backlog parsing, download locations, auto-clearing & discovery --------

    def test_parse_backlog_entry_syntax(self):
        from my_idm.manager import parse_backlog_entry
        from my_idm.utils import normalize_path

        # 1. Blank & Comments
        self.assertEqual(parse_backlog_entry(""), (None, "", None))
        self.assertEqual(parse_backlog_entry("   # comment"), (None, "", None))
        self.assertEqual(parse_backlog_entry("// another comment"), (None, "", None))

        # 2. Directives
        self.assertEqual(
            parse_backlog_entry("# dir: D:/Downloads/ISO"),
            (None, "", normalize_path("D:/Downloads/ISO")),
        )
        self.assertEqual(
            parse_backlog_entry("[D:/Downloads/Music]"),
            (None, "", normalize_path("D:/Downloads/Music")),
        )
        self.assertEqual(
            parse_backlog_entry("save_path = D:/Torrents"),
            (None, "", normalize_path("D:/Torrents")),
        )

        # 3. Simple URL with active save path fallback
        self.assertEqual(
            parse_backlog_entry("https://example.com/file.zip", active_save_path="D:/ActiveDir"),
            ("https://example.com/file.zip", "D:/ActiveDir", None),
        )

        # 4. Pipe delimiter
        self.assertEqual(
            parse_backlog_entry("https://example.com/file.zip | D:/Custom/Dir"),
            ("https://example.com/file.zip", normalize_path("D:/Custom/Dir"), None),
        )

        # 5. Tab delimiter
        self.assertEqual(
            parse_backlog_entry("https://example.com/file.zip\tD:/Tab/Dir"),
            ("https://example.com/file.zip", normalize_path("D:/Tab/Dir"), None),
        )

        # 6. Arrow delimiter
        self.assertEqual(
            parse_backlog_entry("https://example.com/file.zip -> D:/Arrow/Dir"),
            ("https://example.com/file.zip", normalize_path("D:/Arrow/Dir"), None),
        )

        # 7. aria2 style dir= option
        self.assertEqual(
            parse_backlog_entry('https://example.com/file.zip dir="D:/Aria2/Dir"'),
            ("https://example.com/file.zip", normalize_path("D:/Aria2/Dir"), None),
        )

        # 8. Space separated
        self.assertEqual(
            parse_backlog_entry("https://example.com/file.iso D:/Space/Dir"),
            ("https://example.com/file.iso", normalize_path("D:/Space/Dir"), None),
        )

    def test_load_backlog_with_custom_download_locations(self):
        from my_idm.utils import normalize_path
        dest1 = normalize_path(Path(self.tmp_dir.name) / "folder1")
        dest2 = normalize_path(Path(self.tmp_dir.name) / "folder2")

        backlog_content = f"""# Test Backlog
https://example.com/item1.zip | {dest1}
# dir: {dest2}
https://example.com/item2.zip
"""
        bf = Path(self.tmp_dir.name) / "test_backlog.txt"
        bf.write_text(backlog_content, encoding="utf-8")

        added = self.manager.load_backlog(str(bf))
        self.assertEqual(added, 2)

        e1 = self.db.find_by_url("https://example.com/item1.zip")
        self.assertIsNotNone(e1)
        self.assertEqual(e1.save_path, dest1)

        e2 = self.db.find_by_url("https://example.com/item2.zip")
        self.assertIsNotNone(e2)
        self.assertEqual(e2.save_path, dest2)

    def test_load_backlog_clears_entries_on_success(self):
        bf = Path(self.tmp_dir.name) / "clear_backlog.txt"
        bf.write_text(
            "# Queue\nhttps://example.com/success1.zip\nhttps://example.com/success2.zip\n",
            encoding="utf-8",
        )

        self.manager.load_backlog(str(bf))

        # Because all succeeded and clear_backlog_after_load is True by default, file is emptied
        self.assertTrue(bf.exists())
        self.assertEqual(bf.read_text(encoding="utf-8"), "")

    def test_load_backlog_preserves_failed_lines(self):
        self.manager._general_config.clear_backlog_after_load = True
        bf = Path(self.tmp_dir.name) / "partial_backlog.txt"
        # Security policy blocks dangerous urls if block_dangerous_urls is True
        from my_idm.security import SecurityConfig
        self.manager._security_config = SecurityConfig(block_dangerous_urls=True)

        bf.write_text(
            "https://example.com/good.zip\nhttp://malware.testing.example.com/evil.exe\n",
            encoding="utf-8",
        )

        # Mock add_download to fail for evil.exe
        orig_add = self.manager.add_download

        def mock_add(url, **kwargs):
            if "evil.exe" in url:
                return None
            return orig_add(url, **kwargs)

        with patch.object(self.manager, "add_download", side_effect=mock_add):
            added = self.manager.load_backlog(str(bf))
            self.assertEqual(added, 1)

        # Failed line must still be in the file
        remaining = bf.read_text(encoding="utf-8")
        self.assertIn("evil.exe", remaining)
        self.assertNotIn("good.zip", remaining)

    def test_load_backlog_no_clear_when_disabled(self):
        self.manager._general_config.clear_backlog_after_load = False

        bf = Path(self.tmp_dir.name) / "no_clear.txt"
        content = "https://example.com/preserve.zip\n"
        bf.write_text(content, encoding="utf-8")

        self.manager.load_backlog(str(bf))
        self.assertEqual(bf.read_text(encoding="utf-8"), content)

    def test_process_backlogs_multi_locations(self):
        loc1 = Path(self.tmp_dir.name) / "proj_home"
        loc1.mkdir(parents=True, exist_ok=True)
        (loc1 / "backlog.txt").write_text("https://example.com/p1.zip\n", encoding="utf-8")

        loc2 = Path(self.tmp_dir.name) / "user_home"
        loc2.mkdir(parents=True, exist_ok=True)
        (loc2 / "backlog.txt").write_text("https://example.com/u1.zip\n", encoding="utf-8")

        self.manager._general_config.backlog_locations = [str(loc1), str(loc2)]

        count = self.manager.process_backlogs()
        self.assertEqual(count, 2)
        self.assertIsNotNone(self.db.find_by_url("https://example.com/p1.zip"))
        self.assertIsNotNone(self.db.find_by_url("https://example.com/u1.zip"))

    def test_periodic_backlog_polling_timer_and_tick(self):
        from my_idm.config import GeneralConfig
        # Check default timer interval is 60_000 ms (60 seconds)
        self.assertEqual(self.manager._backlog_timer.interval(), 60_000)

        # Updating general config updates timer interval
        new_cfg = GeneralConfig(backlog_poll_interval=30, backlog_poll_enabled=True)
        self.manager.set_general_config(new_cfg)
        self.assertEqual(self.manager._backlog_timer.interval(), 30_000)

        # Test tick handler calls process_backlogs
        with patch.object(self.manager, "process_backlogs", return_value=3) as mock_proc:
            self.manager._on_backlog_timer_tick()
            mock_proc.assert_called_once()

    def test_parse_backlog_entry_custom_filename_and_headers(self):
        from my_idm.manager import parse_backlog_entry

        # 1. Pipe syntax with filename: url | dir | filename
        res1 = parse_backlog_entry("https://example.com/stream | D:/Anime | episode_01.mp4")
        self.assertEqual(res1.filename, "episode_01.mp4")
        self.assertEqual(res1.save_path, "D:/Anime")

        # 2. Key-value options in pipe
        res2 = parse_backlog_entry(
            "https://vault-99.owocdn.top/mp4/123?file=orig.mp4 | dir=D:/Anime | filename=custom.mp4 | referer=https://kwik.cx/"
        )
        self.assertEqual(res2.filename, "custom.mp4")
        self.assertEqual(res2.save_path, "D:/Anime")
        self.assertEqual(res2.headers.get("Referer"), "https://kwik.cx/")

        # 3. Comment preceding entry with embedded filename
        comment = "# Jaadugar A Witch in Mongolia - Episode 11 (AnimePahe_Jaadugar_11_720p.mp4)"
        res3 = parse_backlog_entry(
            "https://vault-99.owocdn.top/mp4/743c1081 | D:/Anime",
            last_comment=comment,
        )
        self.assertEqual(res3.filename, "AnimePahe_Jaadugar_11_720p.mp4")
        self.assertEqual(res3.save_path, "D:/Anime")
        # Auto-referer applied for owocdn
        self.assertEqual(res3.headers.get("Referer"), "https://kwik.cx/")

        # 4. Fallback to query parameter file= when path has no extension
        res4 = parse_backlog_entry("https://vault-99.owocdn.top/mp4/abc?file=Video_720p.mp4")
        self.assertEqual(res4.filename, "Video_720p.mp4")

    def test_load_backlog_with_filename_and_referer(self):
        from my_idm.utils import normalize_path
        dest = normalize_path(Path(self.tmp_dir.name) / "anime_test")
        backlog_text = f"""# Test Backlog
# Episode 11 (AnimePahe_Ep11.mp4)
https://vault-99.owocdn.top/mp4/hash123?file=Raw_Hash.mp4 | {dest} | referer=https://kwik.cx/
"""
        bf = Path(self.tmp_dir.name) / "anime_backlog.txt"
        bf.write_text(backlog_text, encoding="utf-8")

        added = self.manager.load_backlog(str(bf))
        self.assertEqual(added, 1)

        entry = self.db.find_by_url("https://vault-99.owocdn.top/mp4/hash123?file=Raw_Hash.mp4")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.save_path, dest)
        self.assertEqual(entry.filename, "AnimePahe_Ep11.mp4")
        self.assertEqual(entry.metadata.get("headers", {}).get("Referer"), "https://kwik.cx/")
        self.assertTrue(entry.metadata.get("explicit_filename"))

    def test_explicit_filename_preserved_over_website_headers(self):
        """HTTPEngine must preserve explicit_filename rather than overwriting with website header filename."""
        entry = DownloadEntry(
            id="test-explicit-fn",
            url="https://vault-99.owocdn.top/stream/ep1",
            filename="Custom_Frieren_01.mp4",
            save_path=self.tmp_dir.name,
            status="queued",
            download_type="http",
            metadata_json='{"explicit_filename": true}',
        )
        self.db.add_download(entry)

        # Probe discovered a different website generated filename (e.g. from Content-Disposition)
        website_filename = "AnimePahe_Frieren_-_01_720p.mp4"
        meta = entry.metadata
        has_explicit_fn = meta.get("explicit_filename", False)
        self.assertTrue(has_explicit_fn)

        if has_explicit_fn and entry.filename:
            candidate = entry.filename
        else:
            candidate = website_filename

        self.assertEqual(candidate, "Custom_Frieren_01.mp4")

    def test_rename_download_http_completed_file_on_disk(self):
        """Renaming a completed HTTP download renames disk file and updates DB entry."""
        file_path = Path(self.tmp_dir.name) / "old_file.txt"
        file_path.write_text("Hello IDM", encoding="utf-8")

        entry = DownloadEntry(
            id="d_rename_http",
            url="https://example.com/old_file.txt",
            filename="old_file.txt",
            save_path=self.tmp_dir.name,
            file_path=str(file_path),
            status="completed",
            download_type="http",
        )
        self.db.add_download(entry)

        renamed_signals = []
        self.manager.download_renamed.connect(lambda did, name: renamed_signals.append((did, name)))

        ok, msg = self.manager.rename_download("d_rename_http", "new_file.txt")
        self.assertTrue(ok)
        self.assertEqual(msg, "")

        # Disk verification
        self.assertFalse(file_path.exists())
        new_path = Path(self.tmp_dir.name) / "new_file.txt"
        self.assertTrue(new_path.exists())
        self.assertEqual(new_path.read_text(encoding="utf-8"), "Hello IDM")

        # DB verification
        updated = self.db.get_download("d_rename_http")
        self.assertEqual(updated.filename, "new_file.txt")
        from my_idm.utils import normalize_path
        self.assertEqual(updated.file_path, normalize_path(new_path))
        self.assertTrue(updated.metadata.get("explicit_filename"))

        # Signal verification
        self.assertEqual(renamed_signals, [("d_rename_http", "new_file.txt")])

    def test_rename_download_invalid_characters_and_collision(self):
        """Renaming validates filename and prevents collisions with existing files."""
        entry = DownloadEntry(
            id="d_rename_val",
            url="https://example.com/test.bin",
            filename="test.bin",
            save_path=self.tmp_dir.name,
            status="completed",
            download_type="http",
        )
        self.db.add_download(entry)

        # Invalid characters
        ok, msg = self.manager.rename_download("d_rename_val", "invalid/name:?.bin")
        self.assertFalse(ok)
        self.assertIn("invalid characters", msg)

        # Collision with existing file
        existing = Path(self.tmp_dir.name) / "already_exists.bin"
        existing.write_text("Existing", encoding="utf-8")
        ok, msg = self.manager.rename_download("d_rename_val", "already_exists.bin")
        self.assertFalse(ok)
        self.assertIn("already exists", msg)

    def test_rename_download_torrent(self):
        """Renaming a torrent download delegates to TorrentEngine.rename_root and updates DB."""
        entry = DownloadEntry(
            id="d_rename_tor",
            url="magnet:?xt=urn:btih:fedcba9876543210&dn=TorrentRoot",
            filename="TorrentRoot",
            save_path=self.tmp_dir.name,
            status="seeding",
            download_type="torrent",
        )
        self.db.add_download(entry)

        with patch.object(self.manager._torrent, "rename_root", return_value=True) as mock_ren:
            ok, msg = self.manager.rename_download("d_rename_tor", "NewTorrentRoot")
            self.assertTrue(ok)
            mock_ren.assert_called_once_with("d_rename_tor", "NewTorrentRoot")

        updated = self.db.get_download("d_rename_tor")
        self.assertEqual(updated.filename, "NewTorrentRoot")
        self.assertTrue(updated.metadata.get("explicit_filename"))

    # -- Stop download -------------------------------------------------------

    def test_stop_download_sets_stopped_status_and_clears_queue(self):
        """stop_download() sets status to 'stopped' and queue_order to 0."""
        entry = DownloadEntry(
            id="d_stop_1",
            url="https://example.com/large.zip",
            filename="large.zip",
            save_path="C:/Downloads",
            status="downloading",
            queue_order=5,
        )
        self.db.add_download(entry)

        status_signals = []
        self.manager.status_changed.connect(lambda did, s, e: status_signals.append((did, s)))

        self.manager.stop_download("d_stop_1")

        updated = self.db.get_download("d_stop_1")
        self.assertEqual(updated.status, "stopped")
        self.assertEqual(updated.queue_order, 0)
        self.assertIn(("d_stop_1", "stopped"), status_signals)

    def test_stopped_download_not_auto_resumed_on_startup(self):
        """Stopped downloads should NOT be auto-resumed on startup."""
        e_stopped = DownloadEntry(
            id="d_stopped", url="http://example.com/stopped.zip",
            filename="stopped.zip", save_path="/tmp", status="stopped",
        )
        e_queued = DownloadEntry(
            id="d_queued", url="http://example.com/queued.zip",
            filename="queued.zip", save_path="/tmp", status="queued",
        )
        self.db.add_download(e_stopped)
        self.db.add_download(e_queued)

        resumed = []
        with patch.object(self.manager, "resume_download", side_effect=lambda did: resumed.append(did)):
            self.manager.start()

        self.assertNotIn("d_stopped", resumed)
        self.assertIn("d_queued", resumed)

    def test_resume_restarts_stopped_download(self):
        """Manually resuming a stopped download resets it to queued and restarts."""
        entry = DownloadEntry(
            id="d_stop_resume",
            url="https://example.com/resume.zip",
            filename="resume.zip",
            save_path="C:/Downloads",
            status="stopped",
            queue_order=0,
        )
        self.db.add_download(entry)

        with patch.object(self.manager._http, "is_active", return_value=False):
            self.manager.resume_download("d_stop_resume")

        updated = self.db.get_download("d_stop_resume")
        self.assertEqual(updated.status, "queued")
        self.assertEqual(updated.retry_count, 0)

    def test_add_download_dedup_resumes_stopped_entry(self):
        """Re-adding a URL that is in 'stopped' state should resume it."""
        entry = DownloadEntry(
            id="d_stop_dedup",
            url="https://example.com/dedup.zip",
            filename="dedup.zip",
            save_path="C:/Downloads",
            status="stopped",
        )
        self.db.add_download(entry)

        with patch.object(self.manager, "resume_download") as mock_resume:
            result = self.manager.add_download("https://example.com/dedup.zip")

        self.assertEqual(result, "d_stop_dedup")
        mock_resume.assert_called_once_with("d_stop_dedup")

    def test_retry_queue_skips_stopped_downloads(self):
        """The retry queue should not retry stopped downloads."""
        entry = DownloadEntry(
            id="d_stop_retry",
            url="https://example.com/retry.zip",
            filename="retry.zip",
            save_path="C:/Downloads",
            status="stopped",
            retry_count=1,
            max_retries=5,
        )
        self.db.add_download(entry)

        with patch.object(self.manager, "_start_entry") as mock_start:
            self.manager._process_retry_queue()

        mock_start.assert_not_called()

    def test_retry_queue_respects_exponential_backoff_window(self):
        """The retry queue should only restart downloads whose backoff window has elapsed."""
        import time
        # Entry in future backoff window
        future_entry = DownloadEntry(
            id="d_future_retry",
            url="https://example.com/future.zip",
            filename="future.zip",
            save_path="C:/Downloads",
            status="queued",
            retry_count=2,
            max_retries=5,
        )
        future_entry.metadata["next_retry_at"] = time.time() + 300
        self.db.add_download(future_entry)

        with patch.object(self.manager, "_start_entry") as mock_start:
            self.manager._process_retry_queue()
            mock_start.assert_not_called()

        # Update next_retry_at to past
        future_entry.metadata["next_retry_at"] = time.time() - 5
        self.db.update_download(future_entry)

        with patch.object(self.manager, "_start_entry") as mock_start:
            self.manager._process_retry_queue()
            mock_start.assert_called_once()
            self.assertEqual(mock_start.call_args[0][0].id, "d_future_retry")

    def test_completed_download_recheck_single_stream_reads_disk_size(self):
        """Recheck of single-stream HTTP download with no segment records reads disk file size."""
        test_file = Path(self.tmp_dir.name) / "video_single.mp4"
        test_file.write_bytes(b"V" * 50_000)

        entry = DownloadEntry(
            id="recheck-single",
            url="https://example.com/video_single.mp4",
            filename="video_single.mp4",
            file_path=str(test_file),
            save_path=self.tmp_dir.name,
            total_size=50_000,
            downloaded_size=0,
            status="completed",
            download_type="http",
        )
        self.db.add_download(entry)

        self.manager.recheck_download("recheck-single")
        updated = self.db.get_download("recheck-single")
        self.assertEqual(updated.downloaded_size, 50_000)
        self.assertEqual(updated.status, "completed")
        self.assertEqual(updated.progress, 100.0)

    def test_max_concurrent_downloads_limits_active_and_leaves_excess_queued(self):
        """When max_concurrent_downloads is set, extra downloads stay in queued state."""
        self.manager._general_config.max_concurrent_downloads = 2
        with patch.object(self.manager, "_start_entry") as mock_start:
            # Add 4 downloads
            id1 = self.manager.add_download("http://example.com/1.zip", save_path=self.tmp_dir.name, filename="1.zip")
            id2 = self.manager.add_download("http://example.com/2.zip", save_path=self.tmp_dir.name, filename="2.zip")
            # Simulate id1 and id2 transitioning to downloading
            self.db.update_status(id1, "downloading")
            self.db.update_status(id2, "downloading")
            id3 = self.manager.add_download("http://example.com/3.zip", save_path=self.tmp_dir.name, filename="3.zip")
            id4 = self.manager.add_download("http://example.com/4.zip", save_path=self.tmp_dir.name, filename="4.zip")

        # First 2 were started, 3rd and 4th stayed queued
        self.assertEqual(mock_start.call_count, 2)
        e3 = self.db.get_download(id3)
        e4 = self.db.get_download(id4)
        self.assertEqual(e3.status, "queued")
        self.assertEqual(e4.status, "queued")

    def test_queued_download_starts_when_active_finishes_paused_stopped_or_deleted(self):
        """Queued downloads start automatically when active slots are freed."""
        self.manager._general_config.max_concurrent_downloads = 1
        with patch.object(self.manager, "_start_entry"):
            id1 = self.manager.add_download("http://example.com/1.zip", save_path=self.tmp_dir.name, filename="1.zip")
            self.db.update_status(id1, "downloading")
            id2 = self.manager.add_download("http://example.com/2.zip", save_path=self.tmp_dir.name, filename="2.zip")
            self.assertEqual(self.db.get_download(id2).status, "queued")

        # When id1 finishes, id2 should automatically start
        self.db.update_status(id1, "completed")
        with patch.object(self.manager, "_start_entry") as mock_start, \
             patch.object(self.manager, "_handle_completed_scan"):
            self.manager._on_http_status(id1, "completed", "")
            mock_start.assert_called_once()
            self.assertEqual(mock_start.call_args[0][0].id, id2)

    def test_queue_priority_order_processing(self):
        """Queue processor starts downloads in ascending order (order 1 first, last added last)."""
        self.manager._general_config.max_concurrent_downloads = 1
        e_active = DownloadEntry(id="d_act", url="http://example.com/a.zip", status="downloading", queue_order=1)
        e_q2 = DownloadEntry(id="d_q2", url="http://example.com/2.zip", status="queued", queue_order=2, added_at="2026-01-01T10:00:00")
        e_q3 = DownloadEntry(id="d_q3", url="http://example.com/3.zip", status="queued", queue_order=3, added_at="2026-01-01T11:00:00")
        self.db.add_download(e_active)
        self.db.add_download(e_q3)
        self.db.add_download(e_q2)

        # Free slot by pausing active download
        started_ids = []
        with patch.object(self.manager, "_start_entry", side_effect=lambda e: started_ids.append(e.id)):
            self.manager.pause_download("d_act")

        # Highest priority (order 2) should start before order 3
        self.assertEqual(started_ids, ["d_q2"])

    def test_startup_resume_order_respects_queue_priority(self):
        """Startup auto-resume resumes order 1 before higher numbers, processing last added last."""
        e1 = DownloadEntry(id="d1", url="http://example.com/1.zip", status="queued", queue_order=1, added_at="2026-01-01T10:00:00")
        e2 = DownloadEntry(id="d2", url="http://example.com/2.zip", status="queued", queue_order=2, added_at="2026-01-01T11:00:00")
        e3 = DownloadEntry(id="d3", url="http://example.com/3.zip", status="queued", queue_order=3, added_at="2026-01-01T12:00:00")
        # Add in reverse to ensure sorting is tested
        self.db.add_download(e3)
        self.db.add_download(e1)
        self.db.add_download(e2)

        resumed_order = []
        with patch.object(self.manager, "resume_download", side_effect=lambda did: resumed_order.append(did)):
            self.manager.start()

        self.assertEqual(resumed_order, ["d1", "d2", "d3"])

    def test_progress_not_emitted_when_download_paused_stopped_or_suspended(self):
        """Progress updates must NOT be emitted when download is paused, stopped, or suspended."""
        e_paused = DownloadEntry(id="d_paused", url="http://example.com/p.zip", status="paused")
        e_stopped = DownloadEntry(id="d_stopped", url="http://example.com/s.zip", status="stopped")
        e_suspended = DownloadEntry(id="d_suspended", url="http://example.com/sus.zip", status="suspended")
        self.db.add_download(e_paused)
        self.db.add_download(e_stopped)
        self.db.add_download(e_suspended)

        emitted = []
        self.manager.progress_updated.connect(lambda *args: emitted.append(args))

        # HTTP progress callbacks
        self.manager._on_http_progress("d_paused", 500, 1000, 100.0, 5.0)
        self.manager._on_http_progress("d_stopped", 500, 1000, 100.0, 5.0)
        self.manager._on_http_progress("d_suspended", 500, 1000, 100.0, 5.0)

        # Torrent progress callbacks
        self.manager._on_torrent_progress("d_paused", 500, 1000, 100.0, 5.0, 1, 1, 0.0)
        self.manager._on_torrent_progress("d_stopped", 500, 1000, 100.0, 5.0, 1, 1, 0.0)
        self.manager._on_torrent_progress("d_suspended", 500, 1000, 100.0, 5.0, 1, 1, 0.0)

        self.assertEqual(len(emitted), 0)


if __name__ == "__main__":
    unittest.main()

