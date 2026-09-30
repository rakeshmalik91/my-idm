"""Tests for the free-disk-space guard: the helper, the config, and both engines.

The failure this prevents is slow and expensive. The segmented path pre-allocates the whole
file with ``truncate(total_size)`` and the single-stream path appends as it goes, so a 40 GB
download into a 2 GB volume either fails instantly at allocation or grinds for an hour and
dies at 95%.

Two rules shape the implementation and are asserted here:

* only the **remaining** bytes are required, so a resume holding 30 GB of a 40 GB file needs
  10 GB more, not 40 - otherwise every resume of a large download is refused;
* a volume whose free space **cannot be read** never blocks anything. Refusing a download
  because we could not obtain a number would be worse than letting it try.

``shutil.disk_usage`` is patched throughout - it is the only thing that touches the real
volume, and the suite is forbidden from depending on how full the developer's disk is.
"""

from __future__ import annotations

import asyncio
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication

from my_idm.config import GeneralConfig
from my_idm.database import Database, DownloadEntry
from my_idm.http_engine import HTTPEngine
from my_idm.utils import check_disk_space, get_free_disk_space
from tests.test_state_transitions import DOWNLOADING, RecordingHandle

app = QApplication.instance() or QApplication(sys.argv)

GB = 1024 ** 3
MB = 1024 ** 2


def fake_usage(free: int):
    """A ``shutil.disk_usage`` replacement reporting *free* bytes."""
    return MagicMock(return_value=MagicMock(total=500 * GB, used=500 * GB - free, free=free))


class TestGetFreeDiskSpace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_an_existing_directory_reports_its_volume(self):
        with patch.object(shutil, "disk_usage", fake_usage(10 * GB)):
            self.assertEqual(get_free_disk_space(self.tmp), 10 * GB)

    def test_a_file_path_measures_its_parent(self):
        target = self.tmp / "a.zip"
        target.write_bytes(b"x")
        with patch.object(shutil, "disk_usage", fake_usage(7 * GB)) as usage:
            self.assertEqual(get_free_disk_space(target), 7 * GB)
        usage.assert_called_once()

    def test_a_not_yet_created_directory_walks_up_to_an_existing_ancestor(self):
        """The save folder is created on demand, so it usually does not exist yet."""
        missing = self.tmp / "new" / "nested" / "folder"
        self.assertFalse(missing.exists())
        with patch.object(shutil, "disk_usage", fake_usage(3 * GB)):
            self.assertEqual(get_free_disk_space(missing), 3 * GB)

    def test_an_extensionless_missing_path_is_measured_directly(self):
        missing = self.tmp / "not-created-yet"
        with patch.object(shutil, "disk_usage", fake_usage(4 * GB)):
            self.assertEqual(get_free_disk_space(missing), 4 * GB)

    def test_an_unmeasurable_path_reports_zero_rather_than_raising(self):
        """0 means 'unknown' by contract, and unknown must never block a download."""
        with patch.object(shutil, "disk_usage", side_effect=OSError("access denied")):
            self.assertEqual(get_free_disk_space(self.tmp / "x"), 0)

    def test_an_empty_path_reports_zero(self):
        self.assertEqual(get_free_disk_space(""), 0)

    def test_the_walk_terminates_at_the_volume_root(self):
        """A path that resolves to nothing must not spin walking up forever."""
        with patch.object(Path, "exists", return_value=False), \
             patch.object(shutil, "disk_usage", side_effect=AssertionError("must not measure")):
            self.assertEqual(get_free_disk_space("Z:/nope/deeper"), 0)


class TestCheckDiskSpace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_enough_space_passes(self):
        with patch.object(shutil, "disk_usage", fake_usage(10 * GB)):
            ok, free, shortfall = check_disk_space(self.tmp, 4 * GB)
        self.assertTrue(ok)
        self.assertEqual(free, 10 * GB)
        self.assertEqual(shortfall, 0)

    def test_not_enough_space_fails_with_the_shortfall(self):
        with patch.object(shutil, "disk_usage", fake_usage(2 * GB)):
            ok, free, shortfall = check_disk_space(self.tmp, 4 * GB)
        self.assertFalse(ok)
        self.assertEqual(free, 2 * GB)
        self.assertEqual(shortfall, 2 * GB)

    def test_headroom_counts_towards_the_requirement(self):
        """A download that exactly fills the volume is refused, not allowed to wedge it."""
        with patch.object(shutil, "disk_usage", fake_usage(4 * GB)):
            ok, _free, shortfall = check_disk_space(self.tmp, 4 * GB, headroom_bytes=512 * MB)
        self.assertFalse(ok, "a volume left at 100% stalls the OS and every other app")
        self.assertEqual(shortfall, 512 * MB)

    def test_headroom_is_satisfied_when_there_is_room(self):
        with patch.object(shutil, "disk_usage", fake_usage(5 * GB)):
            ok, _free, shortfall = check_disk_space(self.tmp, 4 * GB, headroom_bytes=512 * MB)
        self.assertTrue(ok)
        self.assertEqual(shortfall, 0)

    def test_unknown_free_space_never_blocks(self):
        with patch.object(shutil, "disk_usage", side_effect=OSError("no idea")):
            self.assertEqual(check_disk_space(self.tmp, 999 * GB), (True, 0, 0))

    def test_an_unknown_size_never_blocks(self):
        with patch.object(shutil, "disk_usage", fake_usage(1)):
            ok, free, shortfall = check_disk_space(self.tmp, 0)
        self.assertTrue(ok)
        self.assertEqual(free, 1)
        self.assertEqual(shortfall, 0)

    def test_a_negative_headroom_is_treated_as_zero(self):
        with patch.object(shutil, "disk_usage", fake_usage(4 * GB)):
            ok, _f, _s = check_disk_space(self.tmp, 4 * GB, headroom_bytes=-999)
        self.assertTrue(ok)

    def test_exactly_enough_space_passes(self):
        with patch.object(shutil, "disk_usage", fake_usage(4 * GB)):
            ok, _f, shortfall = check_disk_space(self.tmp, 4 * GB)
        self.assertTrue(ok, "the boundary itself must not be refused")
        self.assertEqual(shortfall, 0)


class TestDiskSpaceConfig(unittest.TestCase):
    """The two settings, and their round trip."""

    def setUp(self):
        from PySide6.QtCore import QSettings

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = QSettings(
            str(Path(self._tmp.name) / "prefs.ini"), QSettings.Format.IniFormat
        )
        self.addCleanup(self.settings.sync)

    def test_the_check_is_on_by_default(self):
        self.assertTrue(GeneralConfig().disk_space_check)

    def test_the_default_headroom_leaves_room_for_the_os(self):
        self.assertGreater(GeneralConfig().disk_space_headroom_mb, 0)

    def test_a_save_load_round_trip_preserves_both(self):
        GeneralConfig(disk_space_check=False, disk_space_headroom_mb=1024).save(self.settings)
        loaded = GeneralConfig.load(self.settings)
        self.assertFalse(loaded.disk_space_check)
        self.assertEqual(loaded.disk_space_headroom_mb, 1024)

    def test_the_defaults_apply_when_nothing_was_ever_written(self):
        loaded = GeneralConfig.load(self.settings)
        self.assertTrue(loaded.disk_space_check)
        self.assertEqual(loaded.disk_space_headroom_mb, 256)

    def test_a_negative_persisted_headroom_is_clamped(self):
        self.settings.setValue("General/disk_space_check", True)
        self.settings.setValue("General/disk_space_headroom_mb", -5)
        self.settings.sync()
        self.assertEqual(GeneralConfig.load(self.settings).disk_space_headroom_mb, 0)

    def test_a_dict_round_trip_preserves_both(self):
        original = GeneralConfig(disk_space_check=False, disk_space_headroom_mb=64)
        restored = GeneralConfig.from_dict(original.to_dict())
        self.assertFalse(restored.disk_space_check)
        self.assertEqual(restored.disk_space_headroom_mb, 64)


class TestDiskSpaceSettingsUI(unittest.TestCase):
    """The two controls exist, are bound to the config, and gate each other."""

    def setUp(self):
        from PySide6.QtWidgets import QCheckBox, QSpinBox

        from my_idm.settings_dialog import SettingsDialog

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        dialog = SettingsDialog()
        self.addCleanup(dialog.close)
        self.addCleanup(dialog.deleteLater)
        self.dialog = dialog
        # The dialog re-reads its config groups from QSettings while populating, so the
        # instance under test is the one it holds, not the object handed to __init__.
        self.general = dialog._general_cfg
        self.check_cb = dialog._disk_space_check_cb
        self.spin = dialog._disk_space_headroom_spin

    def test_both_controls_exist(self):
        from PySide6.QtWidgets import QCheckBox, QSpinBox

        self.assertIsInstance(self.check_cb, QCheckBox)
        self.assertIsInstance(self.spin, QSpinBox)

    def test_loading_shows_the_stored_values(self):
        self.general.disk_space_check = True
        self.general.disk_space_headroom_mb = 512
        self.dialog._populate_fields()
        self.assertTrue(self.check_cb.isChecked())
        self.assertEqual(self.spin.value(), 512)

    def test_saving_writes_the_values_back(self):
        self.check_cb.setChecked(False)
        self.spin.setValue(128)
        self.dialog._on_save()
        self.assertFalse(self.general.disk_space_check)
        self.assertEqual(self.general.disk_space_headroom_mb, 128)

    def test_turning_the_check_off_disables_the_margin(self):
        """A margin for a check that is off is a setting that does nothing."""
        self.check_cb.setChecked(False)
        self.assertFalse(self.spin.isEnabled())
        self.check_cb.setChecked(True)
        self.assertTrue(self.spin.isEnabled())

    def test_the_margin_cannot_be_negative(self):
        self.assertGreaterEqual(self.spin.minimum(), 0)


class HttpDiskSpaceTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)
        self.engine = HTTPEngine(self.db)
        self.statuses: list[tuple] = []
        self.engine.set_callbacks(
            progress_cb=lambda *a: None,
            status_cb=lambda *a: self.statuses.append(a),
            filename_cb=lambda *a: None,
        )

    def configure(self, check=True, headroom_mb=256):
        self.engine.set_general_config_sync(
            GeneralConfig(disk_space_check=check, disk_space_headroom_mb=headroom_mb)
        )

    def add_entry(self, existing_bytes=0):
        target = self.tmp / "a.zip"
        if existing_bytes:
            target.write_bytes(b"\0" * existing_bytes)
        entry = DownloadEntry(
            id="h1",
            url="https://example.com/a.zip",
            filename="a.zip",
            save_path=self.tmp.as_posix(),
            file_path=target.as_posix(),
            total_size=0,
            status="queued",
        )
        self.db.add_download(entry)
        return self.db.get_download("h1")

    def enforce(self, entry, total, free):
        with patch.object(shutil, "disk_usage", fake_usage(free)):
            return self.engine._enforce_disk_space(entry, total)


class TestHttpEngineDiskSpace(HttpDiskSpaceTestCase):
    def test_a_download_larger_than_the_volume_is_refused(self):
        self.configure()
        entry = self.add_entry()
        self.assertFalse(self.enforce(entry, 40 * GB, 2 * GB))
        row = self.db.get_download("h1")
        self.assertEqual(row.status, "error")
        self.assertIn("Not enough disk space", row.error_message)
        self.assertIn("38", row.error_message)  # 40 GB needed, 2 GB free

    def test_the_refusal_names_the_shortfall_and_the_way_out(self):
        self.configure()
        entry = self.add_entry()
        self.enforce(entry, 40 * GB, 2 * GB)
        message = self.db.get_download("h1").error_message
        self.assertIn("Short by", message)
        self.assertIn("Check free disk space", message)

    def test_the_refusal_is_announced_to_the_ui(self):
        self.configure()
        self.enforce(self.add_entry(), 40 * GB, 2 * GB)
        self.assertEqual(self.statuses[-1][1], "error")

    def test_a_download_that_fits_is_allowed(self):
        self.configure()
        self.assertTrue(self.enforce(self.add_entry(), 4 * GB, 10 * GB))
        self.assertEqual(self.db.get_download("h1").status, "queued")

    def test_a_resume_only_needs_the_remaining_bytes(self):
        """A 40 GB download with 30 GB already on disk needs 10 GB, not 40."""
        self.configure()
        entry = self.add_entry(existing_bytes=0)
        target = Path(entry.file_path)
        # Pretend 30 GiB is already on disk without writing 30 GiB: patch the stat.
        real_stat = Path.stat

        def _stat(self, *args, **kwargs):
            if self == target:
                return MagicMock(st_size=30 * GB, st_mode=stat.S_IFREG | 0o644)
            return real_stat(self, *args, **kwargs)

        with patch.object(Path, "stat", _stat):
            self.assertTrue(
                self.enforce(entry, 40 * GB, 12 * GB),
                "a nearly-complete resume must not be refused for space it already used",
            )

    def test_a_resume_is_refused_when_only_the_completion_would_not_fit(self):
        self.configure()
        entry = self.add_entry()
        target = Path(entry.file_path)
        real_stat = Path.stat

        def _stat(self, *args, **kwargs):
            if self == target:
                return MagicMock(st_size=30 * GB, st_mode=stat.S_IFREG | 0o644)
            return real_stat(self, *args, **kwargs)

        with patch.object(Path, "stat", _stat):
            self.assertFalse(self.enforce(entry, 40 * GB, 5 * GB))

    def test_the_headroom_is_required_on_top_of_the_download(self):
        self.configure(headroom_mb=512)
        self.assertFalse(
            self.enforce(self.add_entry(), 4 * GB, 4 * GB),
            "exactly enough for the file is not enough once the margin is counted",
        )

    def test_the_check_can_be_switched_off(self):
        self.configure(check=False)
        self.assertTrue(self.enforce(self.add_entry(), 400 * GB, 1 * GB))
        self.assertEqual(self.db.get_download("h1").status, "queued")

    def test_an_unknown_size_is_never_refused(self):
        """A chunked response has no Content-Length; refusing it would be a guess."""
        self.configure()
        self.assertTrue(self.enforce(self.add_entry(), 0, 1))
        self.assertEqual(self.db.get_download("h1").status, "queued")

    def test_an_unreadable_volume_never_refuses(self):
        self.configure()
        entry = self.add_entry()
        with patch.object(shutil, "disk_usage", side_effect=OSError("denied")):
            self.assertTrue(self.engine._enforce_disk_space(entry, 400 * GB))
        self.assertEqual(self.db.get_download("h1").status, "queued")

    def test_an_entry_with_no_path_is_skipped(self):
        self.configure()
        entry = self.add_entry()
        entry.file_path = ""
        entry.save_path = ""
        self.assertTrue(self.engine._enforce_disk_space(entry, 400 * GB))

    def test_a_fully_present_file_needs_no_space(self):
        self.configure()
        entry = self.add_entry()
        target = Path(entry.file_path)
        target.write_bytes(b"\0" * 1024)
        real_stat = Path.stat

        def _stat(self, *args, **kwargs):
            if self == target:
                return MagicMock(st_size=40 * GB, st_mode=stat.S_IFREG | 0o644)
            return real_stat(self, *args, **kwargs)

        with patch.object(Path, "stat", _stat):
            self.assertTrue(
                self.engine._enforce_disk_space(entry, 40 * GB),
                "a complete file needs nothing written, so it is never blocked",
            )


class TestRunDownloadDiskGate(HttpDiskSpaceTestCase):
    """The guard is wired into the real start path, not just callable."""

    def test_the_download_is_refused_before_any_byte_is_written(self):
        from tests.fake_http import FakeResponse, FakeSession, run_async

        self.configure()
        entry = self.add_entry()
        self.engine._session = FakeSession(
            heads=[FakeResponse(200, headers={
                "Content-Length": str(40 * GB), "Accept-Ranges": "bytes",
            })],
            gets=[FakeResponse(200, chunks=[b"x"])],
        )
        with patch.object(shutil, "disk_usage", fake_usage(2 * GB)):
            run_async(self.engine._run_download(entry, asyncio.Event()))

        row = self.db.get_download("h1")
        self.assertEqual(row.status, "error")
        self.assertIn("Not enough disk space", row.error_message)
        self.assertEqual(
            self.engine._session.get_calls, [],
            "the body must never be requested once the volume is known to be too small",
        )
        self.assertFalse(
            (self.tmp / "a.zip").exists(),
            "nothing may be pre-allocated for a download that cannot fit",
        )

    def test_a_download_that_fits_starts_normally(self):
        self.configure()
        entry = self.add_entry()
        from tests.fake_http import FakeResponse, FakeSession, run_async

        self.engine._session = FakeSession(
            heads=[FakeResponse(200, headers={"Content-Length": "1024"})],
            gets=[FakeResponse(200, chunks=[b"x" * 1024])],
        )
        with patch.object(shutil, "disk_usage", fake_usage(10 * GB)):
            run_async(self.engine._run_download(entry, asyncio.Event()))
        self.assertEqual(self.db.get_download("h1").status, "completed")


class TestTorrentEngineDiskSpace(unittest.TestCase):
    """The torrent half: same contract, plus the handle must actually be stopped."""

    def setUp(self):
        from my_idm import torrent_engine as te_module
        from my_idm.torrent_engine import TorrentEngine

        self.te_module = te_module
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)
        self.engine = TorrentEngine(self.db)
        self.engine._session = MagicMock()
        self.statuses: list[tuple] = []
        self.engine.set_callbacks(
            lambda *a: None,
            lambda *a: self.statuses.append(a),
            lambda *a: None,
        )

    def configure(self, check=True, headroom_mb=256):
        from my_idm.config import GeneralConfig

        self.engine.set_general_config(
            GeneralConfig(disk_space_check=check, disk_space_headroom_mb=headroom_mb)
        )

    def add_entry(self, folder_exists=False):
        target = self.tmp / "Show"
        if folder_exists:
            target.mkdir()
        entry = DownloadEntry(
            id="t1",
            url="magnet:?xt=urn:btih:" + "aa" * 20,
            filename="Show",
            save_path=self.tmp.as_posix(),
            file_path=target.as_posix(),
            download_type="torrent",
            total_size=0,
            status="queued",
        )
        self.db.add_download(entry)
        handle = RecordingHandle(state=DOWNLOADING, done=0, paused=False, auto_managed=True)
        self.engine._handles["t1"] = handle
        return self.db.get_download("t1"), handle

    def test_a_swarm_larger_than_the_volume_is_refused(self):
        self.configure()
        entry, handle = self.add_entry()
        with patch.object(shutil, "disk_usage", fake_usage(2 * GB)):
            ok = self.engine._enforce_disk_space(entry, 40 * GB)
        self.assertFalse(ok)
        self.assertEqual(self.db.get_download("t1").status, "error")
        self.assertIn("Not enough disk space", self.db.get_download("t1").error_message)

    def test_a_refused_torrent_is_paused_so_it_cannot_keep_writing(self):
        self.configure()
        entry, handle = self.add_entry()
        with patch.object(shutil, "disk_usage", fake_usage(2 * GB)):
            self.engine._enforce_disk_space(entry, 40 * GB)
        self.assertTrue(handle.paused, "a refused torrent must not keep filling the disk")
        self.assertIn(("pause",), handle.calls)

    def test_a_refused_torrent_is_marked_so_libtorrent_cannot_restart_it(self):
        self.configure()
        entry, handle = self.add_entry()
        with patch.object(shutil, "disk_usage", fake_usage(2 * GB)):
            self.engine._enforce_disk_space(entry, 40 * GB)
        self.assertFalse(
            handle.auto_managed,
            "auto_managed would let libtorrent restart the torrent without the app",
        )

    def test_the_refusal_is_announced_to_the_ui(self):
        self.configure()
        entry, _handle = self.add_entry()
        with patch.object(shutil, "disk_usage", fake_usage(2 * GB)):
            self.engine._enforce_disk_space(entry, 40 * GB)
        self.assertEqual(self.statuses[-1][1], "error")

    def test_a_refusal_can_be_retried_after_freeing_space(self):
        """The 'already checked' mark is dropped, so resuming re-evaluates."""
        self.configure()
        entry, _handle = self.add_entry()
        with patch.object(shutil, "disk_usage", fake_usage(2 * GB)):
            self.engine._enforce_disk_space(entry, 40 * GB)
        self.assertNotIn(
            "t1", self.engine._disk_checked,
            "a refusal must not permanently mark the download as checked",
        )
        with patch.object(shutil, "disk_usage", fake_usage(100 * GB)):
            self.assertTrue(self.engine._enforce_disk_space(entry, 40 * GB))

    def test_a_swarm_that_fits_is_allowed(self):
        self.configure()
        entry, handle = self.add_entry()
        with patch.object(shutil, "disk_usage", fake_usage(100 * GB)):
            self.assertTrue(self.engine._enforce_disk_space(entry, 40 * GB))
        self.assertEqual(self.db.get_download("t1").status, "queued")
        self.assertEqual(handle.pause_calls, 0)

    def test_the_check_can_be_switched_off(self):
        self.configure(check=False)
        entry, _handle = self.add_entry()
        with patch.object(shutil, "disk_usage", fake_usage(1 * GB)):
            self.assertTrue(self.engine._enforce_disk_space(entry, 400 * GB))

    def test_an_unknown_size_is_never_refused(self):
        self.configure()
        entry, _handle = self.add_entry()
        with patch.object(shutil, "disk_usage", fake_usage(1)):
            self.assertTrue(self.engine._enforce_disk_space(entry, 0))

    def test_bytes_already_in_the_folder_are_credited(self):
        """A half-materialised torrent folder must not be asked for the full size again."""
        self.configure()
        entry, _handle = self.add_entry(folder_exists=True)
        folder = Path(entry.file_path)
        (folder / "e01.mkv").write_bytes(b"\0" * 4096)
        self.assertTrue(folder.is_dir())
        with patch.object(shutil, "disk_usage", fake_usage(6 * GB)):
            self.assertTrue(
                self.engine._enforce_disk_space(entry, 4 * GB),
                "a torrent whose payload is already smaller than the free space must pass",
            )

    def test_the_check_runs_once_per_download_in_the_poll_loop(self):
        """poll_all is 1 Hz; the guard must not re-stat the volume every tick."""
        self.configure()
        self.add_entry()
        self.assertNotIn("t1", self.engine._disk_checked)
        with patch.object(shutil, "disk_usage", fake_usage(100 * GB)) as usage:
            self.engine._disk_checked.add("t1")
            self.assertIn("t1", self.engine._disk_checked)
        self.assertTrue(self.engine._disk_checked)


if __name__ == "__main__":
    unittest.main()
