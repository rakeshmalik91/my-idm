"""Unit tests for DownloadManager lifecycle: startup auto-resume, queue ordering, error retries, and recheck verification."""

import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from unittest.mock import patch, MagicMock
from PySide6.QtWidgets import QApplication

import my_idm.config as config_module
from my_idm.database import Database, DownloadEntry, SegmentEntry
from my_idm.download_model import DownloadTableModel, Col
from my_idm.manager import DownloadManager
from my_idm.utils import normalize_path

app = QApplication.instance() or QApplication([])


class _FakeTorrentStatus:
    """A ``lt.torrent_status`` stand-in with real, comparable values.

    A bare ``MagicMock`` cannot be used here: ``get_status()`` wraps its body in a
    broad ``except Exception`` that returns ``None``, and ``poll_all()`` skips every
    handle whose status is falsy. The rename branch under test would then never
    run and the test would pass for the wrong reason.
    """

    def __init__(self, state=3, has_metadata=True, total_size=1000, done=400):
        self.state = state
        self.has_metadata = has_metadata
        self.total_wanted = total_size
        self.total_wanted_done = done
        self.total_done = done
        self.progress = (done / total_size) if total_size else 0.0
        self.download_rate = 1024
        self.upload_rate = 0
        self.num_seeds = 2
        self.num_peers = 1
        self.num_complete = 4
        self.list_seeds = 3
        self.num_incomplete = 2
        self.list_peers = 1
        self.all_time_upload = 0
        self.all_time_download = 0
        self.last_seen_complete = 0
        self.paused = False


class _FakeTorrentInfo:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name

    def total_size(self):
        return 1000


class _FakeTorrentHandle:
    """Only the surface ``get_status()`` / ``poll_all()`` actually touches."""

    def __init__(self, info_name):
        self._info = _FakeTorrentInfo(info_name) if info_name else None
        self.paused_calls = 0

    def status(self):
        return _FakeTorrentStatus()

    def torrent_file(self):
        return None

    def get_torrent_info(self):
        return self._info

    def pause(self):
        self.paused_calls += 1

    def is_valid(self):
        return True

    def save_resume_data(self):
        return None


class TestManagerLifecycle(unittest.TestCase):

    def setUp(self):
        # addCleanup is LIFO, so registering in this order tears the fixture down
        # in the reverse: temp dir last, once nothing still needs it.
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.db_path = Path(self.tmp_dir.name) / "test.db"
        self.db = Database(self.db_path)
        self.db.open()
        self.addCleanup(self.db.close)
        self.manager = DownloadManager(self.db)
        # Registered immediately after construction: DownloadManager owns four
        # QTimers, an HTTPEngine, a TorrentEngine, a BrowserServer and a
        # TorServiceManager whose data_dir is the user's ~/.my-idm/tor_data.
        self.addCleanup(self.manager.stop)
        self._block_config_persistence()
        self._isolate_backlog_scanner()

    # -- hermeticity guards (autouse for this whole class) ------------------

    def _block_config_persistence(self):
        """Stop every ``set_*_config`` call in this file from writing real settings.

        Each config dataclass's ``save()`` builds ``QSettings("MyIDM", "My-IDM")``
        when called with no argument, which on Windows resolves to the registry.
        ``test_periodic_backlog_polling_timer_and_tick`` therefore persisted
        ``backlog_poll_enabled=True, backlog_poll_interval=30`` and
        ``test_startup_resumes_seeding_torrents_when_configured`` persisted
        ``resume_seeding_on_startup=False`` -- flipping a real user preference on
        every test run, for good. The guard is applied to every config class the
        module exposes so a class added later is covered too.
        """
        patched = []
        for name in dir(config_module):
            obj = getattr(config_module, name)
            if not isinstance(obj, type) or not callable(getattr(obj, "save", None)):
                continue
            patcher = patch.object(obj, "save", lambda self, *a, **k: None)
            patcher.start()
            self.addCleanup(patcher.stop)
            patched.append(name)
        self.assertTrue(
            patched,
            "the config-persistence guard patched nothing; no config class "
            "in my_idm.config exposes save()",
        )

    def _isolate_backlog_scanner(self):
        """Point the backlog scanner at an empty dir and disarm its timer.

        ``_apply_backlog_timer_config()`` runs from ``DownloadManager.__init__``,
        so ``_backlog_timer`` is armed on *every* manager, even one whose
        ``start()`` was never called. A tick reaches the real
        ``process_backlogs()``, which scans ``Path.cwd()``, ``~/.my-idm`` and
        ``Path.home()`` and TRUNCATES every ``backlog.txt`` it finds
        (``clear_backlog_after_load`` defaults to True). This repository has a
        ``backlog.txt``, so a stray tick is a data-loss event on the user's
        machine -- and, while the scan is in flight, the 5 s
        ``stop()`` timer-slot bound is spent waiting for it.
        """
        empty = Path(self.tmp_dir.name) / "empty_backlog_root"
        empty.mkdir(parents=True, exist_ok=True)
        self.manager._general_config.backlog_locations = [str(empty)]
        self.manager._backlog_timer.stop()

    # -- Startup and shutdown ------------------------------------------------

    @unittest.skip("idm-async thread teardown race (see testing.md §6)")
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

        observed = {}

        async def fake_download(cancel_evt):
            """Mirror _run_download's cooperative polling of the cancel event.

            The previous version used ``asyncio.sleep(10)``, which the cancel
            event cannot interrupt: ``HTTPEngine.stop()`` awaits the task with
            ``asyncio.wait_for(task, timeout=3.0)`` and so burned the full 3 real
            seconds on every single run before falling through to ``task.cancel()``.
            """
            while not cancel_evt.is_set():
                await asyncio.sleep(0.01)
            observed["cancel_seen"] = True

        async def run_test():
            cancel_evt = asyncio.Event()
            self.manager._http._cancel_events["dl-active-1"] = cancel_evt
            self.manager._http._tasks["dl-active-1"] = asyncio.create_task(fake_download(cancel_evt))
            await self.manager._http.stop()

        started = time.monotonic()
        asyncio.run(run_test())
        elapsed = time.monotonic() - started

        self.assertTrue(
            observed.get("cancel_seen"),
            "stop() must set the cancel event the running transfer watches",
        )
        self.assertLess(
            elapsed, 1.0,
            f"stop() must release a cooperative transfer at once, took {elapsed:.2f}s "
            "(a 3.0s wait_for timeout was hit instead of the cancel event)",
        )
        self.assertNotIn("dl-active-1", self.manager._http._tasks)
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

    def test_resume_download_skips_when_truly_active_in_engine(self):
        """Resuming an actively downloading transfer skips redundant dispatch."""
        entry = DownloadEntry(
            id="dl-active-live",
            url="https://example.com/live.zip",
            filename="live.zip",
            save_path="C:/Downloads",
            status="downloading",
        )
        self.db.add_download(entry)
        with patch.object(self.manager, "is_download_active", return_value=True):
            with patch.object(self.manager, "_process_queue") as mock_pq:
                self.manager.resume_download("dl-active-live")
                mock_pq.assert_not_called()

    def test_recover_downloads_on_startup_auto_resumes_interrupted(self):
        """Startup recovery transitions interrupted items to queued and calls resume_download in priority order."""
        e1 = DownloadEntry(id="d-active", url="https://example.com/1.zip", filename="1.zip", save_path="C:/Downloads", status="downloading", queue_order=1)
        e2 = DownloadEntry(id="d-stalled", url="https://example.com/2.zip", filename="2.zip", save_path="C:/Downloads", status="stalled", queue_order=2)
        e3 = DownloadEntry(id="d-queued", url="https://example.com/3.zip", filename="3.zip", save_path="C:/Downloads", status="queued", queue_order=3)
        e4 = DownloadEntry(id="d-paused", url="https://example.com/4.zip", filename="4.zip", save_path="C:/Downloads", status="paused", queue_order=4)
        for e in (e1, e2, e3, e4):
            self.db.add_download(e)

        resumed = []
        with patch.object(self.manager, "resume_download", side_effect=lambda did: resumed.append(did)):
            self.manager._recover_downloads_on_startup()

        # d-active and d-stalled were reset to queued in DB so no phantom active counts block limits
        self.assertEqual(self.db.get_download("d-active").status, "queued")
        self.assertEqual(self.db.get_download("d-stalled").status, "queued")
        # And all three (interrupted + previously queued) were auto-resumed in priority order
        self.assertEqual(resumed, ["d-active", "d-stalled", "d-queued"])
        self.assertNotIn("d-paused", resumed)

    def test_recover_downloads_on_startup_pauses_interrupted_when_auto_resume_disabled(self):
        """Startup recovery sets interrupted downloads to paused when auto_resume_startup=False."""
        self.manager._general_config.auto_resume_startup = False
        e1 = DownloadEntry(id="d-active", url="https://example.com/1.zip", filename="1.zip", save_path="C:/Downloads", status="downloading")
        e2 = DownloadEntry(id="d-queued", url="https://example.com/2.zip", filename="2.zip", save_path="C:/Downloads", status="queued")
        self.db.add_download(e1)
        self.db.add_download(e2)

        resumed = []
        with patch.object(self.manager, "resume_download", side_effect=lambda did: resumed.append(did)):
            self.manager._recover_downloads_on_startup()

        self.assertEqual(self.db.get_download("d-active").status, "paused")
        self.assertEqual(resumed, ["d-queued"])

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
        # Exactly one emit: a duplicate would make every view rebuild twice, and
        # "at least one" would not notice a regression that emits on every call.
        self.assertEqual(signals, [True])
        self.assertEqual(self.db.get_download("d2").queue_order, 1)
        self.assertEqual(self.db.get_download("d1").queue_order, 2)

        self.manager.move_queue_down("d2")
        self.assertEqual(signals, [True, True])
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

    def test_verify_completed_downloads_missing_file_transitions_to_file_not_found(self):
        """verify_completed_downloads marks missing completed files as file_not_found."""
        missing_file = Path(self.tmp_dir.name) / "missing_file.zip"
        e = DownloadEntry(
            id="d_missing",
            url="http://example.com/missing.zip",
            filename="missing_file.zip",
            file_path=str(missing_file),
            save_path=self.tmp_dir.name,
            status="completed",
        )
        self.db.add_download(e)

        statuses = []
        self.manager.status_changed.connect(lambda did, st, err: statuses.append((did, st, err)))

        missing_count = self.manager.verify_completed_downloads()
        self.assertEqual(missing_count, 1)
        self.assertIn(("d_missing", "file_not_found", "File not found on disk"), statuses)
        self.assertEqual(self.db.get_download("d_missing").status, "file_not_found")

    def test_verify_completed_downloads_existing_file_preserved(self):
        """verify_completed_downloads leaves existing completed files intact."""
        real_file = Path(self.tmp_dir.name) / "real_file.zip"
        real_file.write_bytes(b"hello world")
        e = DownloadEntry(
            id="d_real",
            url="http://example.com/real.zip",
            filename="real_file.zip",
            file_path=str(real_file),
            save_path=self.tmp_dir.name,
            status="completed",
        )
        self.db.add_download(e)

        statuses = []
        self.manager.status_changed.connect(lambda did, st, err: statuses.append((did, st, err)))

        missing_count = self.manager.verify_completed_downloads()
        self.assertEqual(missing_count, 0)
        self.assertEqual(len(statuses), 0)
        self.assertEqual(self.db.get_download("d_real").status, "completed")

    def test_verify_completed_downloads_non_completed_ignored(self):
        """verify_completed_downloads does not touch queued/paused/downloading downloads."""
        missing_file = Path(self.tmp_dir.name) / "not_started.zip"
        e1 = DownloadEntry(
            id="d_queued",
            url="http://example.com/not_started.zip",
            filename="not_started.zip",
            file_path=str(missing_file),
            save_path=self.tmp_dir.name,
            status="queued",
        )
        e2 = DownloadEntry(
            id="d_paused",
            url="http://example.com/not_started2.zip",
            filename="not_started2.zip",
            file_path=str(missing_file),
            save_path=self.tmp_dir.name,
            status="paused",
        )
        self.db.add_download(e1)
        self.db.add_download(e2)

        missing_count = self.manager.verify_completed_downloads()
        self.assertEqual(missing_count, 0)
        self.assertEqual(self.db.get_download("d_queued").status, "queued")
        self.assertEqual(self.db.get_download("d_paused").status, "paused")

    def test_verify_completed_downloads_backfills_missing_file_path(self):
        """verify_completed_downloads backfills entry.file_path if it exists on disk."""
        real_file = Path(self.tmp_dir.name) / "backfill.zip"
        real_file.write_bytes(b"data")
        e = DownloadEntry(
            id="d_backfill",
            url="http://example.com/backfill.zip",
            filename="backfill.zip",
            file_path="",
            save_path=self.tmp_dir.name,
            status="completed",
        )
        self.db.add_download(e)

        missing_count = self.manager.verify_completed_downloads()
        self.assertEqual(missing_count, 0)
        updated = self.db.get_download("d_backfill")
        self.assertEqual(updated.status, "completed")
        self.assertEqual(updated.file_path, normalize_path(real_file))

    def test_verify_completed_timer_tick(self):
        """Periodic verification timer tick runs verify_completed_downloads."""
        missing_file = Path(self.tmp_dir.name) / "periodic_missing.zip"
        e = DownloadEntry(
            id="d_periodic",
            url="http://example.com/periodic.zip",
            filename="periodic_missing.zip",
            file_path=str(missing_file),
            save_path=self.tmp_dir.name,
            status="completed",
        )
        self.db.add_download(e)

        self.manager._on_verify_completed_timer_tick()
        self.assertEqual(self.db.get_download("d_periodic").status, "file_not_found")

    def test_startup_verifies_completed_downloads(self):
        """Startup automatically verifies completed downloads and transitions missing files."""
        missing_file = Path(self.tmp_dir.name) / "missing_startup.zip"
        e = DownloadEntry(
            id="d_startup_missing",
            url="http://example.com/startup.zip",
            filename="missing_startup.zip",
            file_path=str(missing_file),
            save_path=self.tmp_dir.name,
            status="completed",
        )
        self.db.add_download(e)

        self.manager.start()
        self.assertEqual(self.db.get_download("d_startup_missing").status, "file_not_found")
        self.assertTrue(self.manager._verify_completed_timer.isActive())

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
        """Rechecking a download that had file_not_found status resets it.

        The old assertion was `assertNotEqual(status, "file_not_found")`, which
        also accepts None, "" and "banana". The exact post-recheck contract is
        asserted instead, along with the signals the UI reacts to.
        """
        e = DownloadEntry(id="d1", url="http://example.com/f1.zip", filename="f1.zip", save_path="/tmp", status="file_not_found")
        self.db.add_download(e)

        status_events = []
        self.manager.status_changed.connect(lambda did, st, err: status_events.append((did, st, err)))

        with patch.object(self.manager, "resume_download") as mock_resume:
            self.manager.recheck_download("d1")
            # A recheck inspects the file; it must never kick off a second
            # transfer of its own.
            mock_resume.assert_not_called()

        updated = self.db.get_download("d1")
        self.assertEqual(updated.status, "queued")
        self.assertEqual(updated.downloaded_size, 0)
        self.assertEqual(
            status_events, [("d1", "queued", "File not found")],
            "the UI must be told why the entry was reset",
        )

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

    def test_detect_type_local_torrent_file_on_disk(self):
        """A .torrent path that really exists is a torrent; a missing one is not.

        ``_detect_type`` guards its local-file branch with ``os.path.isfile(url)``,
        which the URL-only test above never reaches, so that branch was untested.
        """
        real = Path(self.tmp_dir.name) / "ubuntu-22.04.iso.torrent"
        real.write_bytes(b"d8:announce4:teste")
        self.assertTrue(real.is_file())

        self.assertEqual(
            self.manager._detect_type(str(real)),
            "torrent",
            "an existing local .torrent file must be detected as a torrent",
        )

        missing = str(Path(self.tmp_dir.name) / "nope.torrent")
        self.assertFalse(Path(missing).exists())
        self.assertEqual(
            self.manager._detect_type(missing),
            "http",
            "a non-existent .torrent path has no scheme and no file, so it is http",
        )

    def test_add_download_of_local_torrent_file_uses_file_stem(self):
        """The local .torrent path is also what `add_download` derives the name from."""
        real = Path(self.tmp_dir.name) / "Frieren.S01E01.torrent"
        real.write_bytes(b"d8:announce4:teste")

        with patch.object(self.manager, "_start_entry") as mock_start:
            download_id = self.manager.add_download(str(real), save_path=self.tmp_dir.name)

        self.assertIsNotNone(download_id)
        mock_start.assert_called_once()
        entry = mock_start.call_args[0][0]
        self.assertEqual(entry.download_type, "torrent")
        self.assertEqual(entry.filename, "Frieren.S01E01")

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

    def test_parse_backlog_entry_extracts_anime_url_and_title(self):
        """animepahe-downloader writes anime_url= and anime_title= as trailing pipe columns.

        Without these being parsed into .headers, load_backlog never populates
        entry.metadata, so the details panel shows "—" for Anime Title and Anime URL
        even on freshly queued downloads.
        """
        from my_idm.manager import parse_backlog_entry
        line = (
            "https://vault-123.owocdn.top/stream/anime_ep1.mp4"
            " | D:/Anime/Series | Ep01.mp4"
            " | anime_url=https://animepahe.pw/anime/123"
            " | anime_title=Frieren: Beyond Journey's End"
            " | queue=AnimePahe"
        )
        p = parse_backlog_entry(line)
        self.assertEqual(p[0], "https://vault-123.owocdn.top/stream/anime_ep1.mp4")
        self.assertEqual(p.filename, "Ep01.mp4")
        self.assertEqual(p.queue, "AnimePahe")
        self.assertEqual(p.headers.get("anime_url"), "https://animepahe.pw/anime/123")
        self.assertEqual(p.headers.get("anime_title"), "Frieren: Beyond Journey's End")

        # The values must survive a round-trip through load_backlog -> add_download
        # into entry.metadata, which is what the details panel reads.
        bf = Path(self.tmp_dir.name) / "anime_backlog.txt"
        bf.write_text(
            "# Frieren: Beyond Journey's End - Episode 1 (Frieren_01_1080p.mp4)\n"
            f"{line}\n",
            encoding="utf-8",
        )
        added = self.manager.load_backlog(str(bf))
        self.assertEqual(added, 1)
        entry = self.db.find_by_url("https://vault-123.owocdn.top/stream/anime_ep1.mp4")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.metadata.get("anime_url"), "https://animepahe.pw/anime/123")
        self.assertEqual(entry.metadata.get("anime_title"), "Frieren: Beyond Journey's End")

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
        """A line that cannot be queued stays in the file; a queued one is removed."""
        self.manager._general_config.clear_backlog_after_load = True
        bf = Path(self.tmp_dir.name) / "partial_backlog.txt"

        bf.write_text(
            "https://example.com/good.zip\nhttp://malware.testing.example.com/evil.exe\n",
            encoding="utf-8",
        )

        # Simulate add_download failing for evil.exe. (The old test also set
        # `block_dangerous_urls=True`, which had no effect at all here because
        # add_download was mocked -- the real blocking policy is exercised
        # separately by test_dangerous_url_blocked_below.)
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

    def test_dangerous_url_blocked_by_security_config(self):
        """A blocked URL is refused by the real add_download, with nothing queued."""
        from my_idm.security import SecurityConfig

        self.manager._security_config = SecurityConfig(block_dangerous_urls=True)
        self.assertTrue(self.manager._security_config.block_dangerous_urls)

        dangerous = "http://malware.testing.example.com/payload.exe"

        with patch.object(self.manager, "_start_entry") as mock_start:
            result = self.manager.add_download(dangerous, save_path=self.tmp_dir.name)

        self.assertIsNone(result, "a dangerous URL must not produce a download id")
        mock_start.assert_not_called()
        self.assertIsNone(
            self.db.find_by_url(dangerous),
            "a blocked URL must never reach the database",
        )

        # A safe URL through the same policy is still accepted, so the assertion
        # above is about the policy and not about add_download being broken.
        with patch.object(self.manager, "_start_entry"):
            safe_id = self.manager.add_download(
                "https://example.com/safe.zip", save_path=self.tmp_dir.name
            )
        self.assertIsNotNone(safe_id)

    def test_add_download_returns_none_when_adding_raises(self):
        """load_backlog treats an exception from add_download as a failed line."""
        self.manager._general_config.clear_backlog_after_load = True
        bf = Path(self.tmp_dir.name) / "raising_backlog.txt"
        bf.write_text(
            "https://example.com/raises.zip\nhttps://example.com/ok.zip\n",
            encoding="utf-8",
        )

        orig_add = self.manager.add_download

        def raising_add(url, **kwargs):
            if "raises.zip" in url:
                raise RuntimeError("simulated engine failure")
            return orig_add(url, **kwargs)

        with patch.object(self.manager, "add_download", side_effect=raising_add):
            added = self.manager.load_backlog(str(bf))

        self.assertEqual(added, 1)
        remaining = bf.read_text(encoding="utf-8")
        self.assertIn("raises.zip", remaining, "the line that raised must be kept")
        self.assertNotIn("ok.zip", remaining, "the line that succeeded must be cleared")

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
        from my_idm.utils import normalize_path

        # Check default timer interval is 60_000 ms (60 seconds)
        self.assertEqual(self.manager._backlog_timer.interval(), 60_000)

        # set_general_config() REPLACES the whole config object, so the empty
        # backlog root installed by _isolate_backlog_scanner has to travel with
        # the new one. Without it, a tick would scan Path.cwd() and Path.home().
        isolated_root = Path(self.tmp_dir.name) / "tick_backlog_root"
        isolated_root.mkdir(parents=True, exist_ok=True)
        new_cfg = GeneralConfig(backlog_poll_interval=30, backlog_poll_enabled=True)
        new_cfg.backlog_locations = [str(isolated_root)]

        # process_backlogs is stubbed for the whole call, not just the tick: it is
        # the real scanner that truncates backlog.txt, and set_general_config
        # re-arms the timer that reaches it. _process_queue is stubbed because it
        # is an unrelated side effect that would otherwise start queued entries.
        with patch.object(self.manager, "process_backlogs", return_value=0) as mock_proc, \
             patch.object(self.manager, "_process_queue") as mock_queue:
            self.manager.set_general_config(new_cfg)

            # Updating general config updates timer interval
            self.assertEqual(self.manager._backlog_timer.interval(), 30_000)
            self.assertEqual(
                self.manager._general_config.get_effective_backlog_locations(),
                [normalize_path(str(isolated_root))],
                "set_general_config must not restore the real cwd/home scan roots",
            )
            mock_proc.assert_not_called()
            mock_queue.assert_called_once_with()

            # Test tick handler calls process_backlogs
            mock_proc.return_value = 3
            self.manager._on_backlog_timer_tick()
            mock_proc.assert_called_once_with()

    def test_backlog_timer_is_not_armed_before_start(self):
        """Regression guard for the data-loss hazard: no tick can fire pre-start.

        `_apply_backlog_timer_config()` runs from `DownloadManager.__init__`, so
        `_backlog_timer` is configured on every manager. It is only *started* for
        a manager whose asyncio thread is alive, which is what keeps a tick from
        reaching the real `process_backlogs()` scanner in tests that never call
        `start()`. If that invariant is ever broken, the scanner truncates the
        user's `backlog.txt`.
        """
        from my_idm.config import GeneralConfig

        isolated_root = Path(self.tmp_dir.name) / "armed_backlog_root"
        isolated_root.mkdir(parents=True, exist_ok=True)
        cfg = GeneralConfig(backlog_poll_interval=1, backlog_poll_enabled=True)
        cfg.backlog_locations = [str(isolated_root)]

        with patch.object(self.manager, "process_backlogs", return_value=0) as mock_proc:
            self.manager.set_general_config(cfg)
            self.assertEqual(self.manager._backlog_timer.interval(), 1_000)
            self.assertFalse(
                self.manager._backlog_timer.isActive(),
                "the backlog poll timer must stay disarmed until start() runs the "
                "asyncio loop, otherwise it scans the user's home directory",
            )
            mock_proc.assert_not_called()

    def test_config_changes_never_reach_real_qsettings(self):
        """The class-level guard really does stop every config save from persisting.

        Without this, `set_general_config` / `set_torrent_config` /
        `set_security_config` wrote the developer's real preferences from a test
        run (config.py's `save()` builds `QSettings("MyIDM", "My-IDM")`, which on
        Windows is the registry).
        """
        from my_idm.config import GeneralConfig, TorrentConfig
        from my_idm.security import SecurityConfig

        with patch.object(config_module, "QSettings") as mock_qsettings:
            self.manager.set_general_config(GeneralConfig(default_segments=17))
            self.manager.set_torrent_config(TorrentConfig(resume_seeding_on_startup=False))
            self.manager.set_security_config(SecurityConfig(block_dangerous_urls=True))

        mock_qsettings.assert_not_called()

        # ...and the values are still applied in memory, i.e. the guard does not
        # turn set_*_config into a no-op.
        self.assertEqual(self.manager.general_config.default_segments, 17)
        self.assertFalse(self.manager.torrent_config.resume_seeding_on_startup)
        self.assertTrue(self.manager._security_config.block_dangerous_urls)

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

    def test_append_to_backlog_backup_deduplication(self):
        """Verify _append_to_backlog_backup skips duplicates and respects the test guard."""
        from unittest.mock import patch
        from pathlib import Path

        backup_file = Path(self.tmp_dir.name) / "backlog.backup.txt"
        with patch("my_idm.paths.data_dir", return_value=Path(self.tmp_dir.name)):
            # 1. When PYTEST_CURRENT_TEST is present, it returns without writing
            self.manager._append_to_backlog_backup(["https://example.com/file1.mp4 | D:/Anime\n"])
            self.assertFalse(backup_file.exists())

            # 2. When PYTEST_CURRENT_TEST is temporarily masked, writing occurs
            with patch.dict("os.environ"):
                import os
                os.environ.pop("PYTEST_CURRENT_TEST", None)
                self.manager._append_to_backlog_backup(["https://example.com/file1.mp4 | D:/Anime\n"])
                self.assertTrue(backup_file.exists())
                content1 = backup_file.read_text(encoding="utf-8")
                self.assertIn("https://example.com/file1.mp4", content1)

                # 3. Adding the exact same URL again should deduplicate and not append
                self.manager._append_to_backlog_backup(["https://example.com/file1.mp4 | D:/Anime\n"])
                content2 = backup_file.read_text(encoding="utf-8")
                self.assertEqual(content1, content2)

                # 4. Adding a new URL prepends the new block
                self.manager._append_to_backlog_backup(["https://example.com/file2.mp4 | D:/Anime\n"])
                content3 = backup_file.read_text(encoding="utf-8")
                self.assertIn("https://example.com/file2.mp4", content3)
                self.assertGreater(len(content3), len(content1))

    def test_explicit_filename_preserved_over_website_headers(self):
        """A torrent rename must not overwrite a user-set explicit filename.

        Driven through the REAL `TorrentEngine.poll_all()`, where the decision
        lives (torrent_engine.py): a website-generated name coming back from
        `get_torrent_info().name()` replaces `entry.filename` only when the entry
        is not flagged `explicit_filename`. The previous version of this test
        re-implemented that `if` inside the test body and asserted on its own
        local variable, so no production code ran at all.
        """
        entry = DownloadEntry(
            id="test-explicit-fn",
            url="magnet:?xt=urn:btih:aaaa1111bbbb2222cccc3333dddd4444eeee5555",
            filename="Custom_Frieren_01.mp4",
            save_path=self.tmp_dir.name,
            status="downloading",
            download_type="torrent",
            total_size=1000,
            downloaded_size=400,
        )
        self.db.add_download(entry)

        # The rename guard: the flag the manager sets for a user-supplied name.
        entry.metadata["explicit_filename"] = True
        self.db.update_download(entry)

        engine = self.manager._torrent
        # has_metadata=True is what makes get_status() read the name at all;
        # state 3 == "downloading", so poll_all() stays off the seeding branch
        # (which needs seeding_after_complete and is covered elsewhere).
        engine._running = True
        engine._session = object()
        engine._handles[entry.id] = _FakeTorrentHandle("AnimePahe_Frieren_-_01_720p.mp4")
        engine._torrent_config.seeding_after_complete = False
        for name in ("get_torrent_files", "get_torrent_trackers", "get_torrent_peers"):
            setattr(engine, name, lambda *a, **k: [])

        resolved = []
        engine._filename_cb = lambda did, name: resolved.append((did, name))

        engine.poll_all()

        after = self.db.get_download(entry.id)
        self.assertEqual(
            after.filename, "Custom_Frieren_01.mp4",
            "poll_all() must not overwrite an explicit filename with the "
            f"website-reported name; got {after.filename!r}",
        )
        self.assertEqual(resolved, [], "no rename callback may fire for an explicit name")
        # NOTE: `original_name` records the *website* name, not the pre-rename
        # name, even when the rename is refused. `rename_download()` uses the same
        # key for the opposite meaning (the name before a user rename), so the
        # two producers disagree about what the field means. Asserted as-is.
        self.assertEqual(after.metadata.get("original_name"), "AnimePahe_Frieren_-_01_720p.mp4")
        # The status path really ran (a bare MagicMock handle would make
        # get_status() return None and skip all of this silently).
        self.assertEqual(after.downloaded_size, 400)

    def test_website_filename_applied_when_no_explicit_filename(self):
        """The negative control: without the flag, the website name does take over.

        Without this, the test above would also pass if the rename branch were
        deleted outright.
        """
        entry = DownloadEntry(
            id="test-implicit-fn",
            url="magnet:?xt=urn:btih:cccc1111dddd2222eeee3333ffff4444aaaa5555",
            filename="temp_name",
            save_path=self.tmp_dir.name,
            status="downloading",
            download_type="torrent",
            total_size=1000,
            downloaded_size=400,
        )
        self.db.add_download(entry)
        self.assertFalse(entry.metadata.get("explicit_filename"))

        engine = self.manager._torrent
        engine._running = True
        engine._session = object()
        engine._handles[entry.id] = _FakeTorrentHandle("AnimePahe_Frieren_-_01_720p.mp4")
        engine._torrent_config.seeding_after_complete = False
        for name in ("get_torrent_files", "get_torrent_trackers", "get_torrent_peers"):
            setattr(engine, name, lambda *a, **k: [])

        resolved = []
        engine._filename_cb = lambda did, name: resolved.append((did, name))

        engine.poll_all()

        after = self.db.get_download(entry.id)
        self.assertEqual(after.filename, "AnimePahe_Frieren_-_01_720p.mp4")
        self.assertEqual(after.metadata.get("original_name"), "AnimePahe_Frieren_-_01_720p.mp4")
        self.assertEqual(
            resolved, [(entry.id, "AnimePahe_Frieren_-_01_720p.mp4")],
            "the rename callback must fire exactly once for an implicit name",
        )

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
        self.assertEqual(updated.metadata.get("original_name"), "old_file.txt")

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
        self.assertEqual(updated.metadata.get("original_name"), "TorrentRoot")

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

    @unittest.skip("idm-async thread teardown race (see testing.md §6)")
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
        # The manager reads the wall clock through `my_idm.manager.time.time()`.
        # `manager.time` *is* the stdlib `time` module, so the only honest way to
        # freeze it is to patch the attribute and restore it; the test therefore
        # pins absolute values instead of `time.time() +/- 300` margins, which
        # would silently drift into the "elapsed" case on a slow machine.
        frozen_now = 1_800_000_000.0
        with patch.object(time, "time", return_value=frozen_now):
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
            future_entry.metadata["next_retry_at"] = frozen_now + 300
            self.db.add_download(future_entry)

            with patch.object(self.manager, "_start_entry") as mock_start:
                self.manager._process_retry_queue()
                mock_start.assert_not_called()

            # A window that has just elapsed by one second must start the retry;
            # the old test used `now - 5`, which is the same idea.
            future_entry.metadata["next_retry_at"] = frozen_now - 1
            self.db.update_download(future_entry)

            with patch.object(self.manager, "_start_entry") as mock_start:
                self.manager._process_retry_queue()
                mock_start.assert_called_once()
                self.assertEqual(mock_start.call_args[0][0].id, "d_future_retry")

    def test_retry_queue_starts_entry_with_no_backoff_record(self):
        """A retried download with no next_retry_at is eligible immediately."""
        frozen_now = 1_800_000_000.0
        entry = DownloadEntry(
            id="d_no_backoff",
            url="https://example.com/nobackoff.zip",
            filename="nobackoff.zip",
            save_path="C:/Downloads",
            status="queued",
            retry_count=1,
            max_retries=5,
        )
        self.db.add_download(entry)

        with patch.object(time, "time", return_value=frozen_now), \
             patch.object(self.manager, "_start_entry") as mock_start:
            self.manager._process_retry_queue()

        mock_start.assert_called_once()
        self.assertEqual(mock_start.call_args[0][0].id, "d_no_backoff")

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

    @unittest.skip("idm-async thread teardown race (see testing.md §6)")
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
        # A live entry proves the suppression is status-based, not a blanket mute.
        e_active = DownloadEntry(id="d_active", url="http://example.com/a.zip", status="downloading")
        for entry in (e_paused, e_stopped, e_suspended, e_active):
            self.db.add_download(entry)

        emitted = []
        self.manager.progress_updated.connect(lambda *args: emitted.append(args))

        # HTTP progress callbacks
        self.manager._on_http_progress("d_paused", 500, 1000, 100.0, 5.0)
        self.manager._on_http_progress("d_stopped", 500, 1000, 100.0, 5.0)
        self.manager._on_http_progress("d_suspended", 500, 1000, 100.0, 5.0)
        self.manager._on_http_progress("d_active", 500, 1000, 100.0, 5.0)

        # Torrent progress callbacks
        self.manager._on_torrent_progress("d_paused", 500, 1000, 100.0, 5.0, 1, 1, 0.0)
        self.manager._on_torrent_progress("d_stopped", 500, 1000, 100.0, 5.0, 1, 1, 0.0)
        self.manager._on_torrent_progress("d_suspended", 500, 1000, 100.0, 5.0, 1, 1, 0.0)
        self.manager._on_torrent_progress("d_active", 500, 1000, 100.0, 5.0, 1, 1, 0.0)

        # Only the active download may report progress.
        ids = [args[0] for args in emitted]
        self.assertEqual(
            ids, ["d_active", "d_active"],
            f"progress must be suppressed for paused/stopped/suspended, got {ids}",
        )

    def test_progress_emitted_for_unknown_download_id(self):
        """An entry that has vanished must not break the progress relay."""
        emitted = []
        self.manager.progress_updated.connect(lambda *args: emitted.append(args))
        self.manager._on_http_progress("does-not-exist", 1, 2, 3.0, 4.0)
        # Still relayed: the handler guards on status, and there is no entry to read.
        self.assertEqual([a[0] for a in emitted], ["does-not-exist"])

    @unittest.skip("idm-async thread teardown race (see testing.md §6)")
    def test_startup_resumes_seeding_torrents_when_configured(self):
        """Torrents in seeding status are resumed on startup when resume_seeding_on_startup is enabled."""
        from my_idm.config import TorrentConfig
        e_seeding = DownloadEntry(
            id="d_seeding_startup",
            url="magnet:?xt=urn:btih:1111222233334444555566667777888899990003",
            filename="SeedingStartupTorrent",
            download_type="torrent",
            status="seeding",
            total_size=5000,
            downloaded_size=5000,
        )
        self.db.add_download(e_seeding)

        # 1. Enabled: add_torrent is called for seeding entry
        self.manager.set_torrent_config(TorrentConfig(resume_seeding_on_startup=True))
        with patch.object(self.manager._torrent, "add_torrent") as mock_add:
            self.manager.start()
            mock_add.assert_called_once()
            called_entry = mock_add.call_args[0][0]
            self.assertEqual(called_entry.id, "d_seeding_startup")

        # 2. Disabled: add_torrent is NOT called for seeding entry
        self.manager.set_torrent_config(TorrentConfig(resume_seeding_on_startup=False))
        with patch.object(self.manager._torrent, "add_torrent") as mock_add2:
            self.manager.start()
            mock_add2.assert_not_called()

        # The in-memory config really flipped; only persistence is blocked
        # (see _block_config_persistence).
        self.assertFalse(self.manager.torrent_config.resume_seeding_on_startup)

    def test_pause_and_stop_at_completed_are_no_ops(self):
        """Pause and stop on a completed download are no-ops and preserve completed state."""
        e_comp = DownloadEntry(
            id="d_comp_noop",
            url="http://example.com/file.zip",
            download_type="http",
            status="completed",
        )
        self.db.add_download(e_comp)

        emitted = []
        self.manager.status_changed.connect(lambda did, st, err: emitted.append((did, st)))

        self.manager.pause_download("d_comp_noop")
        self.assertEqual(self.db.get_download("d_comp_noop").status, "completed")
        self.assertEqual(emitted, [])

        self.manager.stop_download("d_comp_noop")
        self.assertEqual(self.db.get_download("d_comp_noop").status, "completed")
        self.assertEqual(emitted, [])

    def test_pause_and_stop_at_seeding_move_to_completed(self):
        """Pause or stop on a seeding download transitions state to completed."""
        e_seed1 = DownloadEntry(
            id="d_seed_pause",
            url="magnet:?xt=urn:btih:3333444455556666777788889999000011112222",
            download_type="torrent",
            status="seeding",
        )
        e_seed2 = DownloadEntry(
            id="d_seed_stop",
            url="magnet:?xt=urn:btih:4444555566667777888899990000111122223333",
            download_type="torrent",
            status="seeding",
        )
        self.db.add_download(e_seed1)
        self.db.add_download(e_seed2)

        emitted = []
        self.manager.status_changed.connect(lambda did, st, err: emitted.append((did, st)))

        with patch.object(self.manager._torrent, "pause") as mock_pause:
            # 1. Pause seeding -> completed
            self.manager.pause_download("d_seed_pause")
            mock_pause.assert_called_with("d_seed_pause")
            self.assertEqual(self.db.get_download("d_seed_pause").status, "completed")
            self.assertIn(("d_seed_pause", "completed"), emitted)

            # 2. Stop seeding -> completed
            self.manager.stop_download("d_seed_stop")
            mock_pause.assert_called_with("d_seed_stop")
            self.assertEqual(self.db.get_download("d_seed_stop").status, "completed")
            self.assertIn(("d_seed_stop", "completed"), emitted)

    def test_start_seeding(self):
        """start_seeding starts seeding engine and emits seeding status."""
        e_comp_tor = DownloadEntry(
            id="d_start_seed",
            url="magnet:?xt=urn:btih:5555666677778888999900001111222233334444",
            download_type="torrent",
            status="completed",
        )
        self.db.add_download(e_comp_tor)

        emitted = []
        self.manager.status_changed.connect(lambda did, st, err: emitted.append((did, st)))

        with patch.object(self.manager._torrent, "start_seeding", return_value=True) as mock_ss:
            self.manager.start_seeding("d_start_seed")
            mock_ss.assert_called_with("d_start_seed")
            self.assertIn(("d_start_seed", "seeding"), emitted)

    def test_robust_move_download_files_partial_move_recovery(self):
        """robust_move_download_files merges partially moved directories cleanly."""
        from my_idm.utils import robust_move_download_files

        src_dir = Path(self.tmp_dir.name) / "src_torrent"
        dst_dir = Path(self.tmp_dir.name) / "dst_torrent"
        src_dir.mkdir(parents=True, exist_ok=True)
        dst_dir.mkdir(parents=True, exist_ok=True)

        # File 1 was already moved to dst in a previous partial attempt
        (dst_dir / "file1.txt").write_text("Hello from file 1")
        (src_dir / "file1.txt").write_text("Hello from file 1")

        # File 2 was not yet moved and only exists in src
        (src_dir / "file2.txt").write_text("Hello from file 2")

        success, err = robust_move_download_files(src_dir, dst_dir)
        self.assertTrue(success, f"Move failed: {err}")
        self.assertTrue((dst_dir / "file1.txt").exists())
        self.assertTrue((dst_dir / "file2.txt").exists())
        self.assertEqual((dst_dir / "file1.txt").read_text(), "Hello from file 1")
        self.assertEqual((dst_dir / "file2.txt").read_text(), "Hello from file 2")
        # Source directory cleaned up
        self.assertFalse(src_dir.exists())

    def test_stop_all_seeding(self):
        """stop_all_seeding stops all active seeding torrents and returns the count."""
        e1 = DownloadEntry(
            id="seed_all_1",
            url="magnet:?xt=urn:btih:1111111111111111111111111111111111111111",
            download_type="torrent",
            status="seeding",
        )
        e2 = DownloadEntry(
            id="seed_all_2",
            url="magnet:?xt=urn:btih:2222222222222222222222222222222222222222",
            download_type="torrent",
            status="seeding",
        )
        e3 = DownloadEntry(
            id="http_dl",
            url="http://example.com/test.zip",
            download_type="http",
            status="downloading",
        )
        self.db.add_download(e1)
        self.db.add_download(e2)
        self.db.add_download(e3)

        with patch.object(self.manager._torrent, "pause") as mock_pause:
            stopped_count = self.manager.stop_all_seeding()
            self.assertEqual(stopped_count, 2)
            self.assertEqual(self.db.get_download("seed_all_1").status, "completed")
            self.assertEqual(self.db.get_download("seed_all_2").status, "completed")
            self.assertEqual(self.db.get_download("http_dl").status, "downloading")
            self.assertEqual(mock_pause.call_count, 2)

    def test_pause_all_downloads(self):
        """pause_all_downloads pauses downloading, queued, fetching, and stalled transfers."""
        e1 = DownloadEntry(id="p_dl_1", url="http://example.com/1.zip", download_type="http", status="downloading")
        e2 = DownloadEntry(id="p_dl_2", url="magnet:?xt=urn:btih:3333333333333333333333333333333333333333", download_type="torrent", status="queued")
        e3 = DownloadEntry(id="p_comp", url="http://example.com/3.zip", download_type="http", status="completed")
        self.db.add_download(e1)
        self.db.add_download(e2)
        self.db.add_download(e3)

        count = self.manager.pause_all_downloads()
        self.assertEqual(count, 2)
        self.assertEqual(self.db.get_download("p_dl_1").status, "paused")
        self.assertEqual(self.db.get_download("p_dl_2").status, "paused")
        self.assertEqual(self.db.get_download("p_comp").status, "completed")

    def test_pause_download_preserves_progress_in_model_and_db(self):
        """Pausing an active download retains downloaded_size in database and model."""
        test_file = Path(self.tmp_dir.name) / "progress_test.bin"
        test_file.write_bytes(b"x" * 500_000)
        entry = DownloadEntry(
            id="pause-prog-test",
            url="https://example.com/progress_test.bin",
            filename="progress_test.bin",
            file_path=str(test_file),
            save_path=self.tmp_dir.name,
            total_size=1_000_000,
            downloaded_size=0,
            status="downloading",
            download_type="http",
        )
        self.db.add_download(entry)

        model = DownloadTableModel()
        model.load_entries([entry])
        self.manager.status_changed.connect(lambda did, st, err: model.update_status(did, st, err))
        self.manager.progress_updated.connect(
            lambda did, dl, total, spd, eta, s, p, up: model.update_progress(did, dl, total, spd, eta, s, p, up)
        )

        # Simulate live progress reaching 500,000 bytes
        model.update_progress("pause-prog-test", 500_000, 1_000_000, 100_000.0, 5.0)
        row = model._id_to_row["pause-prog-test"]
        prog_before = model.data(model.index(row, Col.PROGRESS))
        self.assertAlmostEqual(prog_before["progress"], 50.0)

        # Pause download
        self.manager.pause_download("pause-prog-test")

        prog_after = model.data(model.index(row, Col.PROGRESS))
        self.assertAlmostEqual(prog_after["progress"], 50.0)
        self.assertEqual(model.get_entry_by_id("pause-prog-test").downloaded_size, 500_000)

        # Check database entry
        db_entry = self.db.get_download("pause-prog-test")
        self.assertEqual(db_entry.status, "paused")
        self.assertEqual(db_entry.downloaded_size, 500_000)

    def test_pause_segmented_download_persists_progress(self):
        """Pausing a segmented download sums segment progress and updates downloaded_size."""
        entry = DownloadEntry(
            id="seg-pause-test",
            url="https://example.com/seg_test.bin",
            filename="seg_test.bin",
            save_path=self.tmp_dir.name,
            total_size=1_000_000,
            downloaded_size=0,
            status="downloading",
            download_type="http",
        )
        self.db.add_download(entry)

        from my_idm.database import SegmentEntry
        seg1 = SegmentEntry(id="s1", download_id="seg-pause-test", index=0, start_byte=0, end_byte=499_999, downloaded_bytes=300_000, status="downloading")
        seg2 = SegmentEntry(id="s2", download_id="seg-pause-test", index=1, start_byte=500_000, end_byte=999_999, downloaded_bytes=200_000, status="downloading")
        self.db.add_segments([seg1, seg2])

        model = DownloadTableModel()
        model.load_entries([entry])
        self.manager.status_changed.connect(lambda did, st, err: model.update_status(did, st, err))
        self.manager.progress_updated.connect(
            lambda did, dl, total, spd, eta, s, p, up: model.update_progress(did, dl, total, spd, eta, s, p, up)
        )

        self.manager.pause_download("seg-pause-test")

        row = model._id_to_row["seg-pause-test"]
        prog = model.data(model.index(row, Col.PROGRESS))
        self.assertAlmostEqual(prog["progress"], 50.0)
        self.assertEqual(model.get_entry_by_id("seg-pause-test").downloaded_size, 500_000)

        db_entry = self.db.get_download("seg-pause-test")
        self.assertEqual(db_entry.status, "paused")
        self.assertEqual(db_entry.downloaded_size, 500_000)

    def test_database_update_download_url(self):
        """Database.update_download_url updates the URL column in SQLite."""
        entry = DownloadEntry(
            id="db-url-test",
            url="https://old.example.com/file.zip",
            filename="file.zip",
            save_path=self.tmp_dir.name,
            total_size=10_000,
            downloaded_size=2_000,
            status="paused",
            download_type="http",
        )
        self.db.add_download(entry)

        success = self.db.update_download_url("db-url-test", "https://new.example.com/file.zip")
        self.assertTrue(success)

        refreshed = self.db.get_download("db-url-test")
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.url, "https://new.example.com/file.zip")

        # Non-existent ID returns False
        self.assertFalse(self.db.update_download_url("non-existent-id", "https://new.example.com/file.zip"))

    def test_manager_update_download_url_paused_state(self):
        """Updating URL on paused download updates cache and sets explicit_filename."""
        entry = DownloadEntry(
            id="mgr-url-test",
            url="https://old.cdn.com/expired-token/data.tar",
            filename="data.tar",
            save_path=self.tmp_dir.name,
            total_size=50_000,
            downloaded_size=15_000,
            status="paused",
            download_type="http",
            metadata_json='{"source": "direct"}',
        )
        self.db.add_download(entry)

        # Attach segments to verify they are preserved
        seg = SegmentEntry(
            id="s1",
            download_id="mgr-url-test",
            index=0,
            start_byte=0,
            end_byte=49_999,
            downloaded_bytes=15_000,
            status="paused",
        )
        self.db.add_segments([seg])

        url_signal_received = []
        self.manager.download_url_updated.connect(
            lambda did, nurl: url_signal_received.append((did, nurl))
        )

        new_url = "https://new.cdn.com/fresh-token/data.tar"
        ok = self.manager.update_download_url("mgr-url-test", new_url)
        self.assertTrue(ok)

        # Verify signal
        self.assertEqual(len(url_signal_received), 1)
        self.assertEqual(url_signal_received[0], ("mgr-url-test", new_url))

        # Verify entry in manager & DB
        updated_entry = self.manager.get_entry("mgr-url-test")
        self.assertIsNotNone(updated_entry)
        self.assertEqual(updated_entry.url, new_url)
        self.assertTrue(updated_entry.metadata.get("explicit_filename"))
        self.assertEqual(updated_entry.downloaded_size, 15_000)

        # Segments preserved
        segs = self.db.get_segments("mgr-url-test")
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].downloaded_bytes, 15_000)

    def test_manager_update_download_url_recovers_from_error_state(self):
        """Updating URL on a failed download clears error message, resets retries, sets paused."""
        entry = DownloadEntry(
            id="mgr-err-test",
            url="https://expired.cdn.com/stream.mp4",
            filename="stream.mp4",
            save_path=self.tmp_dir.name,
            total_size=100_000,
            downloaded_size=40_000,
            status="error",
            error_message="HTTP Error 403: Forbidden (expired token)",
            retry_count=5,
            download_type="http",
        )
        self.db.add_download(entry)

        new_url = "https://fresh.cdn.com/stream.mp4"
        ok = self.manager.update_download_url("mgr-err-test", new_url, resume=False)
        self.assertTrue(ok)

        updated_entry = self.manager.get_entry("mgr-err-test")
        self.assertEqual(updated_entry.url, new_url)
        self.assertEqual(updated_entry.status, "paused")
        self.assertEqual(updated_entry.error_message, "")
        self.assertEqual(updated_entry.retry_count, 0)

    def test_manager_update_download_url_validation_and_active_rejection(self):
        """Rejects non-HTTP URLs, empty URLs, and currently active downloads."""
        entry = DownloadEntry(
            id="active-dl-test",
            url="https://valid.com/video.mp4",
            filename="video.mp4",
            save_path=self.tmp_dir.name,
            total_size=100_000,
            downloaded_size=10_000,
            status="downloading",
            download_type="http",
        )
        self.db.add_download(entry)

        # Active download must be paused first
        self.assertFalse(
            self.manager.update_download_url("active-dl-test", "https://new.com/video.mp4")
        )

        # Change to paused
        self.db.update_status("active-dl-test", "paused")

        # Invalid schemes
        self.assertFalse(self.manager.update_download_url("active-dl-test", "ftp://new.com/video.mp4"))
        self.assertFalse(self.manager.update_download_url("active-dl-test", "magnet:?xt=urn:btih:abc"))
        self.assertFalse(self.manager.update_download_url("active-dl-test", "not_a_url"))
        self.assertFalse(self.manager.update_download_url("active-dl-test", ""))

        # Non-existent download
        self.assertFalse(self.manager.update_download_url("ghost-id", "https://new.com/video.mp4"))

    def test_download_table_model_update_url(self):
        """DownloadTableModel.update_url updates entry.url and emits dataChanged."""
        entry = DownloadEntry(
            id="model-url-test",
            url="https://old.com/file.pkg",
            filename="file.pkg",
            save_path=self.tmp_dir.name,
            total_size=5_000,
            downloaded_size=1_000,
            status="paused",
            download_type="http",
        )
        model = DownloadTableModel()
        model.load_entries([entry])

        signals = []
        model.dataChanged.connect(lambda top_left, bottom_right: signals.append((top_left, bottom_right)))

        new_url = "https://new.com/file.pkg"
        ok = model.update_url("model-url-test", new_url)
        self.assertTrue(ok)

        m_entry = model.get_entry_by_id("model-url-test")
        self.assertIsNotNone(m_entry)
        self.assertEqual(m_entry.url, new_url)
        self.assertGreaterEqual(len(signals), 1)


if __name__ == "__main__":
    unittest.main()


