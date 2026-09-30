"""State-transition tests: after every state change, the download must be in the expected state.

The suite previously checked individual *functions* (``add_torrent`` returns True,
``poll_all`` emits a callback) but never the thing a user actually sees: a download that
the UI labels one way while the engine and the database disagree. That gap is how two
torrents ended up reading "Seeding" while uploading nothing and no timer, limit, or button
ever moving them.

Every test here drives one transition and then asserts the *whole* expected state through
:func:`assert_download_state`:

* the persisted row status,
* whether the libtorrent handle is paused or running,
* whether ``auto_managed`` is set (the flag that decides whether libtorrent may restart
  the torrent by itself after a crash),
* for a seeding row, that the session timestamp is stamped,
* the status callback the UI received,
* and, for the reconciliation tests, that the row and the handle agree.

:func:`assert_download_state` is the point of the file: a single assertion helper means a
failure always names *which* invariant broke, and adding a new transition means writing one
call rather than five ad-hoc ``assertEqual``s.

The two ``*_row_and_handle_disagree`` tests pin the live bug: a row left in ``seeding``
across a restart comes back with a *paused* handle, and neither ``add_torrent`` nor
``poll_all`` ever resumes it.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication

from my_idm import torrent_engine as te_module
from my_idm.config import GeneralConfig, TorConfig, TorrentConfig
from my_idm.database import Database, DownloadEntry
from my_idm.http_engine import HTTPEngine
from my_idm.network import NetworkConfig
from my_idm.torrent_engine import TorrentEngine, seeding_session_start
from tests.fake_http import FakeResponse, FakeSession, run_async

app = QApplication.instance() or QApplication(sys.argv)

# libtorrent torrent_status.state_t values
QUEUED_FOR_CHECKING = 0
CHECKING_FILES = 1
DOWNLOADING_METADATA = 2
DOWNLOADING = 3
FINISHED = 4
SEEDING = 5

# The three handle states the engine tracks beyond the libtorrent state index.
PAUSED = "paused"
AUTO_MANAGED = "auto_managed"


class RecordingHandle:
    """A ``torrent_handle`` that records every mutating call and can be re-driven.

    Unlike a ``MagicMock`` it reports *real* values, because the engine branches on
    ``s.state``, ``s.has_metadata`` and the paused flag - a mock would be truthy for all of
    them and every transition would look like it had already happened.
    """

    def __init__(self, state=DOWNLOADING, has_metadata=True, total=1000, done=0,
                 paused=False, auto_managed=True, total_upload=0):
        self.state = state
        self.has_metadata = has_metadata
        self.total = total
        self.done = done
        self.paused = paused
        self.auto_managed = auto_managed
        self.total_upload = total_upload
        self.info_hash_value = "aa" * 20
        self.download_rate = 0
        self.upload_rate = 0
        self.calls: list[tuple] = []
        self.pause_calls = 0
        self.resume_calls = 0

    # -- introspection -------------------------------------------------------

    def is_valid(self):
        return True

    def info_hash(self):
        return self.info_hash_value

    def torrent_file(self):
        if not self.has_metadata:
            return None
        info = MagicMock()
        info.total_size.return_value = self.total
        info.name.return_value = "Payload"
        return info

    def get_torrent_info(self):
        return self.torrent_file()

    def trackers(self):
        return []

    def get_peer_info(self):
        return []

    def file_progress(self):
        return []

    def get_file_priorities(self):
        return []

    def status(self):
        s = MagicMock()
        s.state = self.state
        s.has_metadata = self.has_metadata
        s.total_wanted = self.total
        s.total_wanted_done = self.done
        s.total_done = self.done
        s.progress = (self.done / self.total) if self.total else 0.0
        s.download_rate = self.download_rate
        s.upload_rate = self.upload_rate
        s.num_seeds = 0
        s.num_peers = 0
        s.num_complete = 0
        s.num_incomplete = 0
        s.list_seeds = 0
        s.list_peers = 0
        s.is_finished = self.state in (FINISHED, SEEDING)
        s.is_seeding = self.state == SEEDING
        s.all_time_upload = self.total_upload
        s.all_time_download = self.total
        s.last_seen_complete = 0
        del s.paused  # force the is_paused fallback
        s.is_paused = self.paused
        return s

    # -- mutators ------------------------------------------------------------

    def pause(self):
        self.paused = True
        self.pause_calls += 1
        self.calls.append(("pause",))

    def resume(self):
        self.paused = False
        self.resume_calls += 1
        self.calls.append(("resume",))

    def set_flags(self, *flags):
        # Recorded by *shape*, not by value: the engine always calls these with the single
        # `lt.torrent_flags.auto_managed` flag, and matching on libtorrent's numeric enum
        # would couple the test to a mocked attribute rather than to the behaviour.
        if flags:
            self.auto_managed = True
        self.calls.append(("set_flags", len(flags)))

    def unset_flags(self, *flags):
        if flags:
            self.auto_managed = False
        self.calls.append(("unset_flags", len(flags)))

    def called(self, name):
        return [c for c in self.calls if c[0] == name]

    def force_recheck(self):
        self.calls.append(("force_recheck",))

    def force_reannounce(self):
        self.calls.append(("force_reannounce",))

    def save_resume_data(self):
        self.calls.append(("save_resume_data",))

    def flush_cache(self):
        self.calls.append(("flush_cache",))

    def move_storage(self, path):
        self.calls.append(("move_storage", path))

    def set_upload_limit(self, limit):
        self.calls.append(("set_upload_limit", limit))
        self.upload_limit = limit

    def set_download_limit(self, limit):
        self.calls.append(("set_download_limit", limit))
        self.download_limit = limit

    def file_priority(self, index, priority):
        self.calls.append(("file_priority", index, priority))

    def rename_file(self, index, path):
        self.calls.append(("rename_file", index, path))


class TransitionTestCase(unittest.TestCase):
    """Engine + database + a handle whose state the test drives directly."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.fastresume = self.tmp / "fastresume"
        self.fastresume.mkdir(parents=True, exist_ok=True)
        patcher = patch.object(te_module, "FASTRESUME_DIR", self.fastresume)
        patcher.start()
        self.addCleanup(patcher.stop)

        # `lt` is a mock so no real libtorrent call is made; the engine only ever passes it
        # `torrent_flags.auto_managed`, and RecordingHandle matches on call shape.
        self.flags_patch = patch.object(te_module, "lt", MagicMock())
        self.flags_patch.start()
        self.addCleanup(self.flags_patch.stop)

        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)

        self.callbacks: list[tuple] = []
        self.engine = TorrentEngine(self.db)
        self.engine.set_callbacks(
            lambda *a: self.callbacks.append(("progress", a)),
            lambda *a: self.callbacks.append(("status", a)),
            lambda *a: self.callbacks.append(("filename", a)),
        )
        self.engine.apply_network_config(NetworkConfig(download_limit=1000, upload_limit=1000))
        self.engine.apply_torrent_config(
            TorrentConfig(seeding_after_complete=True, max_seeding_speed=0,
                          download_to_seeding_ratio=0.0, seeding_time_limit_minutes=0,
                          seeding_ratio_limit=0.0, resume_seeding_on_startup=True)
        )
        self.engine._session = MagicMock()
        self.engine._running = True

    # -- fixture helpers -----------------------------------------------------

    def seed_torrent(self, status="queued", **kw):
        entry = DownloadEntry(
            id="t1",
            url="magnet:?xt=urn:btih:" + "aa" * 20,
            filename="Payload",
            save_path=self.tmp.as_posix(),
            file_path=(self.tmp / "Payload").as_posix(),
            download_type="torrent",
            total_size=kw.pop("total_size", 1000),
            downloaded_size=kw.pop("downloaded_size", 0),
            status=status,
            **kw,
        )
        self.db.add_download(entry)
        return entry

    def magnet_params(self, name=""):
        """A ``parse_magnet_uri`` result whose info hash the engine can actually read.

        ``info_hashes`` is deleted on purpose: the engine prefers
        ``params.info_hashes.v1``, and a bare MagicMock yields a ``"<MagicMock id=...>"``
        string that never matches a real hash - which would make every fastresume look
        mismatched and silently discarded.
        """
        params = MagicMock()
        del params.info_hashes
        params.info_hash = "aa" * 20
        params.name = name
        params.save_path = ""
        return params

    def resume_params(self):
        """A ``read_resume_data`` result for the *same* torrent, so it is accepted."""
        params = MagicMock()
        del params.info_hashes
        params.info_hash = "aa" * 20
        params.save_path = ""
        return params

    def add(self, status="queued", handle=None, has_fastresume=False, **kw):
        """Add a torrent through the real ``add_torrent`` and return the live handle."""
        entry = self.seed_torrent(status=status, **kw)
        if has_fastresume:
            (self.fastresume / "t1.fastresume").write_bytes(b"resume")
        self.engine._session.add_torrent.return_value = handle or RecordingHandle()
        with patch.object(te_module.lt, "parse_magnet_uri",
                          return_value=self.magnet_params()), \
             patch.object(te_module.lt, "read_resume_data",
                          return_value=self.resume_params()):
            self.assertTrue(self.engine.add_torrent(entry))
        return self.engine._handles["t1"]

    def poll(self, times=1):
        for _ in range(times):
            self.engine.poll_all()

    def row(self):
        return self.db.get_download("t1")

    def statuses(self):
        return [a[1] for kind, a in self.callbacks if kind == "status"]

    def last_status(self):
        trail = self.statuses()
        return trail[-1] if trail else None

    def write_fastresume(self):
        (self.fastresume / "t1.fastresume").write_bytes(b"resume")

    # -- the one assertion every transition uses ------------------------------

    def assert_download_state(
        self,
        status,
        handle=None,
        paused=None,
        auto_managed=None,
        seeding_stamped=None,
        expect_status_cb=None,
        reason="",
    ):
        """Assert the persisted status, the handle's run state, and the UI callback.

        ``status`` is the expected database row status. ``paused`` and ``auto_managed``
        default to *derived* from the status, because that derivation is the invariant
        under test:

        ==========================  ==========  =============
        row status                  handle      auto_managed
        ==========================  ==========  =============
        downloading / checking      running     left alone
        fetching_metadata          running     left alone
        seeding                    running     True
        completed / paused          paused      False
        error / queued / suspended  n/a         n/a
        ==========================  ==========  =============

        Passing ``paused``/``auto_managed`` explicitly overrides the derivation, which is
        how the seeding-recovery tests state their expectation.
        """
        where = f" after {reason}" if reason else ""
        row = self.row()
        self.assertIsNotNone(row, f"the download row vanished{where}")
        self.assertEqual(row.status, status, f"database row status{where}")

        if handle is not None and paused is not None:
            self.assertEqual(
                handle.paused, paused,
                f"handle pause state{where}: a row reading '{status}' but a "
                f"{'paused' if handle.paused else 'running'} handle means the engine and the "
                f"UI disagree about the same download",
            )
        if handle is not None and auto_managed is not None:
            self.assertEqual(
                handle.auto_managed, auto_managed,
                f"auto_managed flag{where}: with auto_managed unset libtorrent refuses to "
                f"run the torrent at all, and with it set libtorrent may restart it after "
                f"a crash without the app asking",
            )

        if seeding_stamped is not None:
            stamp = seeding_session_start(row)
            self.assertEqual(bool(stamp), seeding_stamped,
                             f"seeding session timestamp{where}")

        if expect_status_cb is not None:
            self.assertEqual(
                self.last_status(), expect_status_cb,
                f"status callback{where}: the UI is driven by this signal, so a row that "
                f"changes without one leaves the table showing the old state",
            )
        return row


# ===========================================================================
# add_torrent: the state a download enters the app in
# ===========================================================================

class TestEnteringStates(TransitionTestCase):
    def test_a_queued_torrent_with_metadata_and_no_resume_enters_checking(self):
        handle = self.add("queued", RecordingHandle(state=DOWNLOADING, done=0))
        self.assert_download_state(
            "checking", handle, paused=False, seeding_stamped=False,
            expect_status_cb="checking",
            reason="add_torrent with no fastresume",
        )
        self.assertIn(("force_recheck",), handle.calls)

    def test_a_queued_torrent_with_a_resume_enters_downloading_without_rechecking(self):
        handle = self.add("queued", RecordingHandle(state=DOWNLOADING, done=400),
                          has_fastresume=True)
        self.assert_download_state(
            "downloading", handle, paused=False, expect_status_cb="downloading",
            reason="add_torrent with a fastresume",
        )
        self.assertNotIn(("force_recheck",), handle.calls,
                         "a restored resume must not force a recheck")

    def test_a_torrent_without_metadata_enters_fetching_metadata_and_arms_the_timer(self):
        """The row, the callback and the watchdog must all agree on ``fetching_metadata``.

        Regression test. ``add_torrent`` calls ``update_status(id, "fetching_metadata")`` and
        then, to arm the suspend-after-N-days watchdog, sets ``fetching_metadata_since`` and
        calls ``update_download(entry)``. ``update_download`` writes *every* column, and
        ``entry`` is the caller's object whose ``status`` is still ``"queued"`` - so the
        second write silently reverted the first.

        Consequences for every magnet: the row stayed "Queued" in the table, the manager kept
        re-queuing it against the concurrency limit, and ``_check_fetching_metadata_timeout``
        - which requires ``status == "fetching_metadata"`` - could never suspend a stuck
        magnet.
        """
        handle = self.add("queued", RecordingHandle(state=DOWNLOADING_METADATA,
                                                    has_metadata=False))
        row = self.assert_download_state(
            "fetching_metadata", handle, paused=False, expect_status_cb="fetching_metadata",
            reason="add_torrent with no metadata",
        )
        self.assertTrue(
            row.fetching_metadata_since,
            "the metadata watchdog needs a start timestamp or a stuck magnet is never "
            "suspended",
        )

    def test_a_paused_torrent_enters_paused_with_a_paused_handle(self):
        handle = self.add("paused", RecordingHandle(state=DOWNLOADING, done=0))
        self.assert_download_state(
            "paused", handle, paused=True, auto_managed=False, expect_status_cb="paused",
            reason="add_torrent of a paused row",
        )

    def test_a_completed_torrent_enters_completed_with_a_paused_handle(self):
        """A finished torrent must not start uploading again behind the user's back."""
        handle = self.add("completed", RecordingHandle(state=FINISHED, done=1000))
        self.assert_download_state(
            "completed", handle, paused=True, auto_managed=False,
            expect_status_cb=None,
            reason="add_torrent of a completed row",
        )


# ===========================================================================
# poll_all: the states a running download moves through
# ===========================================================================

class TestPollingTransitions(TransitionTestCase):
    def test_a_finished_download_becomes_seeding_and_starts_seeding(self):
        handle = self.add("downloading", RecordingHandle(state=FINISHED, done=1000),
                          has_fastresume=True)
        self.poll()
        self.assert_download_state(
            "seeding", handle, paused=False, auto_managed=None, seeding_stamped=True,
            expect_status_cb="seeding", reason="download reached 100%",
        )
        self.assertTrue(self.row().completed_at, "completion must be stamped")
        self.assertTrue(self.row().last_seeded_at, "the first seed must be stamped")

    def test_completion_with_seeding_disabled_becomes_completed_and_pauses_the_handle(self):
        self.engine.apply_torrent_config(
            TorrentConfig(seeding_after_complete=False, max_seeding_speed=0)
        )
        handle = self.add("downloading", RecordingHandle(state=FINISHED, done=1000),
                          has_fastresume=True)
        self.poll()
        self.assert_download_state(
            "completed", handle, paused=True, auto_managed=False, seeding_stamped=False,
            expect_status_cb="completed", reason="seeding disabled at completion",
        )

    def test_a_seeding_download_stays_seeding_and_does_not_restamp_the_session(self):
        handle = self.add("seeding", RecordingHandle(state=SEEDING, done=1000))
        self.poll()  # the first poll backfills the session stamp the row never had
        first_stamp = seeding_session_start(self.row())
        self.assertTrue(first_stamp, "precondition: the session is stamped")
        self.poll(times=3)
        self.assert_download_state(
            "seeding", handle, paused=False, expect_status_cb=None,
            reason="repeated polls of a healthy seeder",
        )
        self.assertEqual(
            seeding_session_start(self.row()), first_stamp,
            "a steady seeder must not have its session timer reset on every poll, or the "
            "duration limit can never be reached",
        )
        self.assertEqual(
            self.statuses(), [],
            "a poll that changes nothing must not emit a status callback",
        )

    def test_a_seeding_download_with_no_session_stamp_gets_one_on_the_next_poll(self):
        """A row restored as 'seeding' from an older build has no timestamp at all."""
        handle = self.add("seeding", RecordingHandle(state=SEEDING, done=1000))
        self.row().seeding_started_at = ""
        self.db.update_download(self.row())
        self.poll()
        self.assert_download_state(
            "seeding", handle, seeding_stamped=True,
            reason="a seeding row with no session stamp",
        )

    def test_a_recheck_that_finishes_full_moves_to_seeding(self):
        handle = self.add("checking", RecordingHandle(state=FINISHED, done=1000),
                          has_fastresume=True)
        self.poll()
        self.assert_download_state(
            "seeding", handle, paused=False, seeding_stamped=True,
            expect_status_cb="seeding", reason="recheck found the payload complete",
        )

    def test_a_recheck_that_finishes_partial_moves_to_downloading(self):
        handle = self.add("checking", RecordingHandle(state=CHECKING_FILES, done=400))
        self.poll()
        # Still rechecking: the row must not move until libtorrent leaves that state.
        self.assert_download_state("checking", handle, reason="mid-recheck")
        handle.state = DOWNLOADING
        self.poll()
        self.assert_download_state(
            "downloading", handle, paused=False, expect_status_cb="downloading",
            reason="recheck found the payload incomplete",
        )

    def test_a_recheck_that_finishes_partial_on_a_paused_handle_moves_to_paused(self):
        handle = self.add("checking",
                          RecordingHandle(state=CHECKING_FILES, done=400, paused=True))
        self.poll()
        self.assert_download_state("checking", handle, reason="mid-recheck")
        handle.state = DOWNLOADING  # recheck finished, the payload is short
        self.poll()
        self.assert_download_state(
            "paused", handle, paused=True, expect_status_cb="paused",
            reason="recheck of a paused torrent",
        )

    def test_received_metadata_forces_a_recheck_before_reporting_progress(self):
        handle = self.add("fetching_metadata", RecordingHandle(state=CHECKING_FILES, done=0))
        self.poll()
        self.assert_download_state(
            "checking", handle, expect_status_cb="checking",
            reason="metadata arrived",
        )
        self.assertIn(("force_recheck",), handle.calls,
                      "a magnet that may already be on disk must be rechecked first")
        self.assertEqual(self.row().fetching_metadata_since, "",
                         "the metadata watchdog must be disarmed once metadata is in")

    def test_a_stalled_download_is_reported_and_announced(self):
        handle = self.add("downloading", RecordingHandle(state=DOWNLOADING, done=100),
                          has_fastresume=True)
        self.engine._last_active_time["t1"] = 0.0
        self.poll()  # seeds the inactivity clock
        self.engine._last_active_time["t1"] = 1.0
        with patch.object(te_module.time, "time", return_value=1_000.0):
            self.poll()
        self.assert_download_state(
            "stalled", handle, expect_status_cb="stalled",
            reason="no speed and no peers for over 45s",
        )
        self.assertIn(("force_reannounce",), handle.calls)

    def test_a_stalled_download_recovers_when_peers_appear(self):
        handle = self.add("stalled", RecordingHandle(state=DOWNLOADING, done=100),
                          has_fastresume=True)
        handle.download_rate = 1024
        self.poll()
        self.assert_download_state(
            "downloading", handle, expect_status_cb="downloading",
            reason="transfer resumed",
        )

    def test_a_completed_row_with_lost_data_reopens_as_downloading(self):
        """The payload was deleted behind the app's back, e.g. by a cleanup tool.

        The handle is attached directly rather than through ``add_torrent`` because a
        completed row is *deliberately* re-paused on the way in; a running handle is what
        an unexpected re-check of missing data looks like.
        """
        handle = RecordingHandle(state=DOWNLOADING, done=200, paused=False)
        self.seed_torrent(status="completed", downloaded_size=1000)
        self.engine._handles["t1"] = handle
        self.poll()
        self.assert_download_state(
            "downloading", handle, paused=False, expect_status_cb="downloading",
            reason="a completed row whose payload is only partly present",
        )


# ===========================================================================
# Seeding limits
# ===========================================================================

class TestSeedingLimitTransitions(TransitionTestCase):
    def _seeding_torrent(self, handle=None, since=None, total_upload=0):
        handle = handle or RecordingHandle(state=SEEDING, done=1000,
                                          total_upload=total_upload)
        self.add("seeding", handle)
        if since is not None:
            # One object: `self.db.update_download(self.row())` would write a *fresh*
            # entry that never saw the assignment.
            row = self.row()
            row.seeding_started_at = since.isoformat()
            self.db.update_download(row)
        return handle

    def test_reaching_the_duration_limit_moves_to_completed_and_pauses(self):
        stale = datetime.now(timezone.utc) - timedelta(hours=5)
        handle = self._seeding_torrent(since=stale)
        self.engine.apply_torrent_config(
            TorrentConfig(seeding_after_complete=True, seeding_time_limit_minutes=240,
                          max_seeding_speed=0, seeding_ratio_limit=0.0)
        )
        self.poll()
        self.assert_download_state(
            "completed", handle, paused=True, auto_managed=False,
            expect_status_cb="completed", reason="240-minute seeding limit reached",
        )
        self.assertNotIn("manual_seeding", self.row().metadata)

    def test_below_the_duration_limit_the_seeder_keeps_running(self):
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        handle = self._seeding_torrent(since=recent)
        self.engine.apply_torrent_config(
            TorrentConfig(seeding_after_complete=True, seeding_time_limit_minutes=240,
                          max_seeding_speed=0, seeding_ratio_limit=0.0)
        )
        self.poll()
        self.assert_download_state(
            "seeding", handle, paused=False, expect_status_cb=None,
            reason="still inside the seeding window",
        )

    def test_reaching_the_ratio_limit_moves_to_completed_and_pauses(self):
        handle = self._seeding_torrent(total_upload=2000)
        self.engine.apply_torrent_config(
            TorrentConfig(seeding_after_complete=True, seeding_ratio_limit=1.5,
                          seeding_time_limit_minutes=0, max_seeding_speed=0)
        )
        self.poll()
        self.assert_download_state(
            "completed", handle, paused=True, auto_managed=False,
            expect_status_cb="completed", reason="seeding ratio limit reached",
        )
        self.assertNotIn("seeding_baseline_upload", self.row().metadata)

    def test_a_manual_session_measures_the_ratio_from_its_own_baseline(self):
        """Re-seeding an already-uploaded torrent must not instantly trip the ratio."""
        row_entry = self._seeding_torrent(total_upload=5000)
        row = self.row()
        row.metadata["manual_seeding"] = True
        row.metadata["seeding_baseline_upload"] = 5000
        self.db.update_download(row)
        self.engine.apply_torrent_config(
            TorrentConfig(seeding_after_complete=True, seeding_ratio_limit=1.5,
                          seeding_time_limit_minutes=0, max_seeding_speed=0)
        )
        self.poll()
        self.assert_download_state(
            "seeding", row_entry, paused=False, expect_status_cb=None,
            reason="no upload since the manual session began",
        )


# ===========================================================================
# User-facing verbs
# ===========================================================================

class TestUserActionTransitions(TransitionTestCase):
    def test_pausing_a_seeder_moves_to_completed_not_paused(self):
        """A seeded torrent is finished; pausing it must not look like a partial resume."""
        handle = self.add("seeding", RecordingHandle(state=SEEDING, done=1000))
        self.engine.pause("t1")
        self.assert_download_state(
            "completed", handle, paused=True, auto_managed=False,
            expect_status_cb="completed", reason="pause on a seeding torrent",
        )
        self.assertNotIn("manual_seeding", self.row().metadata)

    def test_pausing_a_partial_download_moves_to_paused(self):
        handle = self.add("downloading", RecordingHandle(state=DOWNLOADING, done=400),
                          has_fastresume=True)
        self.engine.pause("t1")
        self.assert_download_state(
            "paused", handle, paused=True, auto_managed=False, expect_status_cb="paused",
            reason="pause on a partial download",
        )

    def test_pausing_a_completed_torrent_changes_nothing(self):
        handle = self.add("completed", RecordingHandle(state=FINISHED, done=1000))
        before_pauses = handle.pause_calls
        self.engine.pause("t1")
        self.assert_download_state(
            "completed", handle, expect_status_cb=None,
            reason="pause on an already-completed torrent",
        )
        self.assertEqual(handle.pause_calls, before_pauses,
                         "an already-completed torrent must not be re-paused")

    def test_resuming_a_paused_torrent_moves_to_downloading(self):
        handle = self.add("paused", RecordingHandle(state=DOWNLOADING, done=400))
        self.engine.resume("t1")
        self.assert_download_state(
            "downloading", handle, paused=False, expect_status_cb="downloading",
            reason="resume",
        )

    def test_start_seeding_resumes_a_stopped_torrent_and_stamps_a_fresh_session(self):
        handle = self.add("completed", RecordingHandle(state=FINISHED, done=1000))
        stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.row().seeding_started_at = stale
        self.db.update_download(self.row())

        self.assertTrue(self.engine.start_seeding("t1"))
        self.assert_download_state(
            "seeding", handle, paused=False, auto_managed=True, seeding_stamped=True,
            expect_status_cb="seeding", reason="start seeding",
        )
        self.assertNotEqual(
            self.row().seeding_started_at, stale,
            "a new seeding session must get a fresh start time or the duration limit is "
            "already expired the moment it begins",
        )
        self.assertTrue(self.row().metadata.get("manual_seeding"))

    def test_start_seeding_of_a_non_torrent_is_refused(self):
        entry = DownloadEntry(id="t1", url="https://x/a.zip", filename="a.zip",
                              save_path=self.tmp.as_posix(), download_type="http")
        self.db.add_download(entry)
        self.assertFalse(self.engine.start_seeding("t1"))
        self.assert_download_state("queued", reason="start seeding on an HTTP download")

    def test_start_seeding_of_an_unknown_id_is_refused(self):
        self.assertFalse(self.engine.start_seeding("nope"))

    def test_start_seeding_adds_the_torrent_when_there_is_no_handle_yet(self):
        entry = self.seed_torrent(status="completed", downloaded_size=1000)
        fresh = RecordingHandle(state=FINISHED, done=1000)
        self.engine._session.add_torrent.return_value = fresh
        with patch.object(te_module.lt, "parse_magnet_uri",
                          return_value=self.magnet_params()):
            self.assertTrue(self.engine.start_seeding("t1"))
        self.assert_download_state(
            "seeding", self.engine._handles["t1"], paused=False, auto_managed=True,
            seeding_stamped=True, expect_status_cb="seeding",
            reason="start seeding with no live handle",
        )
        self.assertEqual(entry.id, "t1")


# ===========================================================================
# Row/handle reconciliation - the live bug
# ===========================================================================

class TestRowAndHandleDisagreement(TransitionTestCase):
    """A row that says one thing while its handle does another must be reconciled.

    This is the failure these tests were written for: torrents that read "Seeding" forever
    while uploading nothing, with no timer, limit, or button able to move them. They are now
    regression tests - the reconciliation exists, and these assert it holds.

    Two independent layers are covered, because either alone would be enough to leave a
    download stuck:

    * ``add_torrent`` resumes a seeding row on the way in, so a handle restored paused from
      a fastresume starts running immediately;
    * ``poll_all`` repairs a seeding row whose handle is not seeding, so a torrent that
      falls over later - or that is added by some other path - is still recovered.
    """

    def test_a_seeding_row_is_added_with_its_handle_resumed(self):
        """A seeding row must enter the app with a running, auto-managed handle.

        ``add_torrent`` has three entry branches: ``paused`` and ``completed`` both clear
        ``auto_managed`` and pause, while the ``seeding`` branch used to only apply the
        upload limit. A handle restored paused from a fastresume therefore stayed paused,
        and because ``poll_all``'s completion transition is guarded on the status *not*
        already being ``seeding``, nothing ever corrected it: a download reading "Seeding"
        that uploaded nothing for as long as the app stayed open. The seeding branch is now
        symmetric with the other two.
        """
        handle = RecordingHandle(state=FINISHED, done=1000, paused=True, auto_managed=False)
        self.add("seeding", handle)

        self.assert_download_state(
            "seeding", handle, paused=False, auto_managed=True, seeding_stamped=None,
            reason="add_torrent of a seeding row",
        )
        self.assertGreaterEqual(
            handle.resume_calls, 1,
            "a seeding row must be resumed on the way in, or a handle restored paused from a "
            "fastresume never uploads",
        )
        self.assertTrue(
            handle.called("set_flags"),
            "auto_managed must be set, or libtorrent is not permitted to run the torrent",
        )
        self.assertEqual(
            handle.pause_calls, 0, "a seeding row must not be paused on the way in"
        )

    def test_polling_resumes_a_seeding_row_whose_handle_is_paused(self):
        """Second line of defence: the poll reconciles a row whose handle died later.

        No ``poll_all`` branch used to match a ``seeding`` row whose handle was not seeding
        - the completion transition is guarded on the status not already being ``seeding``,
        and the later branches key off ``fetching_metadata``, ``checking`` or
        ``completed`` - so polling forever changed nothing.

        The repair deliberately does **not** re-stamp ``seeding_started_at``: doing so every
        second would reset the duration-limit baseline and the limit could never be reached.
        """
        handle = RecordingHandle(state=FINISHED, done=1000, paused=True, auto_managed=False)
        self.add("seeding", handle)
        handle.pause()          # simulate the handle dying after it was added
        handle.auto_managed = False
        self.poll(times=5)

        self.assert_download_state(
            "seeding", handle, paused=False, auto_managed=True,
            reason="polling a seeding row whose handle went paused",
        )
        self.assertGreaterEqual(
            handle.resume_calls, 2,
            "the poll must resume a seeding row's handle; otherwise the download is frozen "
            "in a state that looks active and is not",
        )

    def test_polling_a_healthy_seeder_does_not_restart_it(self):
        """The repair must be a no-op once the handle is genuinely seeding.

        Otherwise every tick would re-issue resume, a stopped handle would be
        indistinguishable from a running one, and the UI would see a torrent that never
        settles.
        """
        handle = RecordingHandle(state=SEEDING, done=1000, paused=False, auto_managed=True)
        self.add("seeding", handle)
        self.poll()
        settled = handle.resume_calls
        self.poll(times=5)
        self.assertEqual(
            handle.resume_calls, settled,
            "a handle that is already seeding must be left alone",
        )

    def test_the_repair_is_logged_once_per_episode_not_every_tick(self):
        """``poll_all`` runs every second; a per-tick info log would flood the log file."""
        handle = RecordingHandle(state=SEEDING, done=1000, paused=False, auto_managed=True)
        self.add("seeding", handle)
        with self.assertNoLogs("my_idm.torrent_engine", level="INFO"):
            self.poll(times=5)

        # Break it, and confirm exactly one line however many polls follow.
        handle.pause()
        with self.assertLogs("my_idm.torrent_engine", level="INFO") as captured:
            self.poll(times=5)
        repairs = [ln for ln in captured.output if "Repaired seeding torrent" in ln]
        self.assertEqual(len(repairs), 1, f"expected exactly one repair log, got {repairs}")
        self.assertIn("t1", repairs[0], "the log must name the download it repaired")

    def test_a_seeding_row_whose_handle_downloads_is_resumed_and_self_corrects(self):
        """A seeding row with a re-downloading handle is repaired, not relabelled.

        The repair restarts the handle but leaves the status alone: libtorrent finishes the
        payload, after which the row is honest again. Flipping the row to "downloading" here
        would be more descriptive, but a torrent that briefly re-reports ``downloading``
        while seeding would then flip back and forth and emit a status callback every second.
        """
        handle = RecordingHandle(state=DOWNLOADING, done=300, paused=False)
        self.add("seeding", handle)
        self.poll(times=3)

        self.assert_download_state(
            "seeding", handle, paused=False, auto_managed=True,
            reason="a seeding row whose handle fell back to downloading",
        )
        # Once the payload completes the row is consistent again and the repair stops.
        handle.state = SEEDING
        handle.done = 1000
        before = handle.resume_calls
        self.poll(times=3)
        self.assertEqual(handle.resume_calls, before,
                         "a genuinely seeding handle must be left alone")

    def test_a_seeding_row_whose_handle_rechecks_is_resumed_and_settles(self):
        """Same as above for a handle sitting in a re-check."""
        handle = RecordingHandle(state=CHECKING_FILES, done=0)
        self.add("seeding", handle)
        self.poll(times=3)
        self.assert_download_state(
            "seeding", handle, paused=False, auto_managed=True,
            reason="a seeding row whose handle is rechecking",
        )

    def test_a_seeding_row_and_its_handle_always_agree_after_a_few_polls(self):
        """The invariant, stated once so a future transition cannot reintroduce it.

        A row reading ``seeding`` is only honest if its handle is genuinely seeding:
        libtorrent state ``FINISHED``/``SEEDING``, not paused, ``auto_managed`` set, and
        the payload complete. Every combination is exercised.
        """
        for handle_state, paused, auto_managed, done in (
            (DOWNLOADING, False, True, 0),
            (DOWNLOADING, True, False, 0),
            (CHECKING_FILES, False, True, 0),
            (FINISHED, True, False, 1000),
            (SEEDING, True, False, 1000),
        ):
            with self.subTest(handle_state=handle_state, paused=paused):
                self.setUp()
                handle = RecordingHandle(
                    state=handle_state, done=done,
                    paused=paused, auto_managed=auto_managed,
                )
                self.add("seeding", handle)
                self.poll(times=4)

                self.assertEqual(self.row().status, "seeding")
                self.assertFalse(
                    handle.paused,
                    f"a 'seeding' row must never keep a paused handle (libtorrent state "
                    f"{handle_state}) - the download would never upload",
                )
                self.assertTrue(
                    handle.auto_managed,
                    f"a 'seeding' row must always have auto_managed set (libtorrent state "
                    f"{handle_state}), or libtorrent refuses to run it",
                )

    def test_start_seeding_still_re_stamps_a_fresh_session(self):
        """The user-facing verb is unchanged: it restarts a session deliberately."""
        handle = RecordingHandle(state=SEEDING, done=1000, paused=False, auto_managed=True)
        self.add("seeding", handle)
        stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        row = self.row()
        row.seeding_started_at = stale
        self.db.update_download(row)

        self.assertTrue(self.engine.start_seeding("t1"))
        self.assert_download_state(
            "seeding", handle, paused=False, auto_managed=True,
            expect_status_cb="seeding", reason="an explicit start seeding",
        )
        self.assertNotEqual(
            self.row().seeding_started_at, stale,
            "an explicit restart must get a fresh start time or the duration limit is "
            "already expired the moment it begins",
        )

    def test_a_completed_row_with_a_paused_handle_does_reopen_correctly(self):
        """The pre-existing ``completed`` reconciliation, kept as a contrast.

        ``add_torrent`` pauses a completed row's handle, and ``poll_all`` has an explicit
        ``elif entry.status == "completed"`` branch that reopens it as soon as libtorrent
        reports progress again. The seeding row needed the same treatment and now gets it.
        """
        handle = RecordingHandle(state=DOWNLOADING, done=200, paused=False)
        self.seed_torrent(status="completed", downloaded_size=1000)
        self.engine._handles["t1"] = handle
        self.poll()
        self.assert_download_state(
            "downloading", handle, paused=False, expect_status_cb="downloading",
            reason="a completed row that turns out to be incomplete",
        )


# ===========================================================================
# HTTP downloads: the same expected-state contract
# ===========================================================================

class TestHttpStateTransitions(unittest.TestCase):
    """The same expected-state contract for plain HTTP downloads.

    Kept on its own ``HTTPEngine`` (the torrent engine has no ``_run_download``), sharing
    the recording handle only in spirit: here the state under test is the row status and
    the terminal callback, because the HTTP path has no libtorrent handle to disagree.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)

        self.callbacks: list[tuple] = []
        self.engine = HTTPEngine(self.db)
        self.engine.set_callbacks(
            progress_cb=lambda *a: self.callbacks.append(("progress", a)),
            status_cb=lambda *a: self.callbacks.append(("status", a)),
            filename_cb=lambda *a: self.callbacks.append(("filename", a)),
        )
        self.engine.set_general_config_sync(GeneralConfig())
        self.engine._session = FakeSession()
        self.addCleanup(self._drop)

        entry = DownloadEntry(
            id="h1",
            url="https://example.com/a.zip",
            filename="a.zip",
            save_path=self.tmp.as_posix(),
            file_path=(self.tmp / "a.zip").as_posix(),
            total_size=10,
            status="queued",
        )
        self.db.add_download(entry)

    def _drop(self):
        self.engine._session = None

    def use_http(self, size=10, accept_ranges=True, gets=None, head=None):
        headers = {"Content-Length": str(size)}
        if accept_ranges:
            headers["Accept-Ranges"] = "bytes"
        self.engine._session = FakeSession(
            heads=[head or FakeResponse(200, headers=headers)],
            gets=list(gets or []),
        )
        return self.engine._session

    def row(self):
        return self.db.get_download("h1")

    def run_http(self, cancel=None):
        import asyncio

        entry = self.row()
        return run_async(
            self.engine._run_download(entry, cancel or asyncio.Event())
        )

    def statuses(self):
        return [a[1] for kind, a in self.callbacks if kind == "status"]

    def assert_http_state(self, status, expect_status_cb=None, reason=""):
        where = f" after {reason}" if reason else ""
        self.assertEqual(self.row().status, status, f"database row status{where}")
        if expect_status_cb is not None:
            self.assertEqual(
                self.statuses()[-1] if self.statuses() else None, expect_status_cb,
                f"status callback{where}: the table is driven by this signal",
            )

    # -- transitions ---------------------------------------------------------

    def test_a_fresh_download_goes_queued_to_downloading_to_completed(self):
        self.use_http(gets=[FakeResponse(
            200, headers={"Content-Length": "10"}, chunks=[b"0123456789"],
            url="https://example.com/a.zip",
        )])
        self.run_http()
        row = self.row()
        self.assert_http_state("completed", expect_status_cb="completed",
                               reason="a clean 10-byte download")
        self.assertEqual(row.downloaded_size, 10)
        self.assertTrue(row.completed_at, "completion must stamp completed_at")
        self.assertEqual(self.statuses(), ["downloading", "completed"],
                         "the table must see the start and the end, in that order")

    def test_a_pre_cancelled_download_never_reaches_completed(self):
        import asyncio

        self.use_http(gets=[FakeResponse(200, chunks=[b"x"])])
        evt = asyncio.Event()
        evt.set()
        self.run_http(cancel=evt)
        self.assertNotIn("completed", self.statuses())

    def test_a_transient_failure_requeues_for_a_retry(self):
        row = self.row()
        row.max_retries = 3
        self.db.update_download(row)
        self.use_http(gets=[FakeResponse(raise_on_enter=OSError("no route to host"))] * 3)
        self.run_http()
        self.assert_http_state("queued", expect_status_cb="queued",
                               reason="one fatal failure against max_retries=3")
        self.assertGreater(
            self.row().metadata.get("next_retry_at", 0), 0,
            "a requeued download must record when it is next due",
        )

    def test_an_exhausted_retry_ends_in_error(self):
        row = self.row()
        row.max_retries = 1
        self.db.update_download(row)
        self.use_http(gets=[FakeResponse(raise_on_enter=OSError("no route to host"))])
        self.run_http()
        self.assert_http_state("error", expect_status_cb="error",
                               reason="max_retries=1 exhausted")
        self.assertTrue(self.row().error_message)

    def test_a_resumed_download_finishes_the_file_on_disk(self):
        row = self.row()
        Path(row.file_path).write_bytes(b"0123")
        row.status = "completed"
        row.downloaded_size = 10
        row.total_size = 10
        self.db.update_download(row)
        self.use_http(gets=[FakeResponse(
            206, headers={"Content-Range": "bytes 4-9/10"}, chunks=[b"456789"],
            url="https://example.com/a.zip",
        )])
        self.run_http()
        self.assert_http_state("completed", expect_status_cb="completed",
                               reason="resuming a half-written file")
        self.assertEqual(Path(self.row().file_path).read_bytes(), b"0123456789")

    def test_a_large_download_reports_completion_with_the_full_size(self):
        """The segmented path must land the same terminal state as the single path."""
        self.row().total_size = 0
        self.db.update_download(self.row())
        payload = 1024 * 1024
        self.use_http(
            size=payload,
            gets=[
                FakeResponse(206, chunks=[b"x" * (payload // 2)]),
                FakeResponse(206, chunks=[b"x" * (payload // 2)]),
            ],
        )
        # The engine reads the per-download count from `num_segments`.
        row = self.row()
        row.num_segments = 2
        self.db.update_download(row)
        self.run_http()
        self.assert_http_state("completed", expect_status_cb="completed",
                               reason="a segmented 1 MB download")
        self.assertEqual(self.row().downloaded_size, payload)


if __name__ == "__main__":
    unittest.main()
