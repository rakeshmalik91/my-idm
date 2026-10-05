"""Tests for .torrent ingress: drag-and-drop payload parsing and the watched-folder scanner.

The pure helpers are the load-bearing part: `local_torrent_paths_from_mime` is the whole of the
drop decision, and `find_new_torrents` is the age rule that stops a watched downloads folder from
importing its own history. Both are tested without a window, a Qt event loop, or a network.
"""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QMimeData, QObject, QSettings, QUrl
from PySide6.QtWidgets import QApplication

from my_idm.torrent_sources import (
    MAX_TORRENT_AGE_DAYS,
    TorrentFolderWatcher,
    canonical_torrent_path,
    find_new_torrents,
    is_torrent_path,
    local_torrent_paths_from_mime,
)

app = QApplication.instance() or QApplication([])


def _age(path: Path, seconds: float) -> None:
    """Backdate a file so the age rule can be exercised without sleeping."""
    when = time.time() - seconds
    os.utime(path, (when, when))


class _FakeMime:
    """The two things `local_torrent_paths_from_mime` asks of a QMimeData."""

    def __init__(self, urls, has_urls=True, explode=False):
        self._urls = urls
        self._has_urls = has_urls
        self._explode = explode

    def hasUrls(self):
        if self._explode:
            raise RuntimeError("payload is malformed")
        return self._has_urls

    def urls(self):
        return self._urls


def _file_url(path) -> QUrl:
    return QUrl.fromLocalFile(str(path))


class TestIsTorrentPath(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.good = self.tmp / "x.torrent"
        self.good.write_bytes(b"d1:ae")

    def test_an_existing_torrent_file_is_accepted(self):
        self.assertTrue(is_torrent_path(str(self.good)))

    def test_the_extension_test_is_case_insensitive(self):
        upper = self.tmp / "X.TORRENT"
        upper.write_bytes(b"d1:ae")
        # `.TORRENT` is a torrent; a case-sensitive test would silently drop it and the user
        # would see their drop do nothing.
        self.assertTrue(is_torrent_path(str(upper)))

    def test_a_non_torrent_file_is_rejected(self):
        other = self.tmp / "x.txt"
        other.write_text("hi")
        self.assertFalse(is_torrent_path(str(other)))

    def test_a_torrent_that_does_not_exist_is_rejected(self):
        # `_detect_type` only calls a path a torrent when os.path.isfile agrees, so reporting a
        # missing file as a candidate would be a lie the manager acts on — it would be added as
        # an HTTP download instead.
        self.assertFalse(is_torrent_path(str(self.tmp / "gone.torrent")))

    def test_a_directory_named_like_a_torrent_is_rejected(self):
        folder = self.tmp / "dir.torrent"
        folder.mkdir()
        self.assertFalse(is_torrent_path(str(folder)))

    def test_junk_input_never_raises(self):
        for value in ("", None, 42, "C:\\bad\x00path.torrent"):
            with self.subTest(value=value):
                self.assertFalse(is_torrent_path(value))


class TestCanonicalTorrentPath(unittest.TestCase):
    def test_separators_and_relative_segments_are_normalised(self):
        # The same file reaches us spelled two ways: QUrl.toLocalFile gives backslashes on
        # Windows, os.scandir gives whatever the filesystem stores. `add_download` de-duplicates
        # on an exact string match, so un-normalised they become two rows and two downloads.
        if os.name == "nt":
            sample = "C:\\torrents\\..\\torrents\\a.torrent"
            expected = "C:\\torrents\\a.torrent"
            self.assertEqual(
                canonical_torrent_path("C:/torrents/../torrents/a.torrent"),
                os.path.normpath(os.path.abspath(expected)),
            )
        else:
            sample = "/torrents/../torrents/a.torrent"
            expected = "/torrents/a.torrent"
        self.assertEqual(
            canonical_torrent_path(sample),
            os.path.normpath(os.path.abspath(expected)),
        )

    def test_case_is_preserved(self):
        # normcase would lowercase the whole path on Windows, and that string is shown in the UI
        # and stored as the row's url.
        sample = "C:\\Torrents\\A.torrent" if os.name == "nt" else "/Torrents/A.torrent"
        self.assertTrue(canonical_torrent_path(sample).endswith("A.torrent"))

    def test_junk_input_yields_an_empty_string_not_an_exception(self):
        self.assertEqual(canonical_torrent_path(None), "")
        self.assertEqual(canonical_torrent_path(12345), "")


class TestLocalTorrentPathsFromMime(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.a = self.tmp / "a.torrent"
        self.a.write_bytes(b"d1:ae")
        self.b = self.tmp / "b.torrent"
        self.b.write_bytes(b"d1:ae")
        self.txt = self.tmp / "notes.txt"
        self.txt.write_text("hi")

    def test_a_torrent_file_url_is_returned(self):
        got = local_torrent_paths_from_mime(_FakeMime([_file_url(self.a)]))
        self.assertEqual(got, [canonical_torrent_path(str(self.a))])

    def test_several_torrents_keep_their_order(self):
        got = local_torrent_paths_from_mime(
            _FakeMime([_file_url(self.a), _file_url(self.b)])
        )
        self.assertEqual(got, [canonical_torrent_path(str(self.a)),
                               canonical_torrent_path(str(self.b))])

    def test_non_torrents_in_the_payload_are_skipped(self):
        # A drop carrying one torrent among other files should still work, and the others should
        # not be reported as if they were.
        got = local_torrent_paths_from_mime(
            _FakeMime([_file_url(self.txt), _file_url(self.a)])
        )
        self.assertEqual(got, [canonical_torrent_path(str(self.a))])

    def test_a_remote_torrent_url_is_skipped(self):
        # A .torrent fetched over HTTP is already reachable through Add Download, and its URL
        # form would not survive _detect_type as a local path.
        got = local_torrent_paths_from_mime(_FakeMime([QUrl("https://e.com/x.torrent")]))
        self.assertEqual(got, [])

    def test_duplicates_of_one_file_are_collapsed(self):
        same = _file_url(self.a)
        got = local_torrent_paths_from_mime(_FakeMime([same, same]))
        self.assertEqual(got, [canonical_torrent_path(str(self.a))])

    def test_a_payload_with_no_urls_is_empty(self):
        self.assertEqual(local_torrent_paths_from_mime(_FakeMime([], has_urls=False)), [])

    def test_none_and_a_malformed_payload_are_empty_rather_than_raising(self):
        # This runs inside a Qt drag handler; an exception there propagates into the event loop.
        self.assertEqual(local_torrent_paths_from_mime(None), [])
        self.assertEqual(local_torrent_paths_from_mime(_FakeMime([], explode=True)), [])

    def test_a_real_qmime_data_works_too(self):
        # Guards against the fake having a more forgiving contract than the real thing.
        mime = QMimeData()
        mime.setUrls([_file_url(self.a), _file_url(self.txt)])
        self.assertEqual(
            local_torrent_paths_from_mime(mime), [canonical_torrent_path(str(self.a))]
        )


class TestFindNewTorrents(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fresh = self.tmp / "fresh.torrent"
        self.fresh.write_bytes(b"d1:ae")
        self.stale = self.tmp / "old.torrent"
        self.stale.write_bytes(b"d1:ae")
        _age(self.stale, (MAX_TORRENT_AGE_DAYS + 2) * 86400)
        self.txt = self.tmp / "notes.txt"
        self.txt.write_text("hi")

    def test_only_recent_torrents_are_returned(self):
        # The whole reason the feature is safe: pointing the watcher at a downloads folder that
        # already holds a hundred torrents must not import its history.
        got = find_new_torrents(str(self.tmp))
        self.assertEqual(got, [canonical_torrent_path(str(self.fresh))])

    def test_the_age_window_is_configurable(self):
        got = find_new_torrents(str(self.tmp), max_age_days=MAX_TORRENT_AGE_DAYS + 5)
        self.assertEqual(len(got), 2)

    def test_zero_days_admits_nothing(self):
        self.assertEqual(find_new_torrents(str(self.tmp), max_age_days=0), [])

    def test_known_paths_are_skipped(self):
        known = [canonical_torrent_path(str(self.fresh))]
        self.assertEqual(find_new_torrents(str(self.tmp), known), [])

    def test_results_are_sorted_so_a_scan_is_deterministic(self):
        for name in ("b.torrent", "c.torrent", "d.torrent"):
            (self.tmp / name).write_bytes(b"d1:ae")
        got = find_new_torrents(str(self.tmp))
        self.assertEqual(got, sorted(got))

    def test_a_missing_folder_is_empty_rather_than_an_error(self):
        self.assertEqual(find_new_torrents(str(self.tmp / "nope")), [])
        self.assertEqual(find_new_torrents(""), [])

    def test_the_scan_does_not_recurse(self):
        # A watch folder is one folder. Recursing would reach the payload directories a finished
        # torrent leaves behind, which is a way to import the wrong thing.
        nested = self.tmp / "payload"
        nested.mkdir()
        (nested / "inner.torrent").write_bytes(b"d1:ae")
        self.assertNotIn(
            canonical_torrent_path(str(nested / "inner.torrent")),
            find_new_torrents(str(self.tmp)),
        )

    def test_the_per_scan_limit_bounds_one_tick(self):
        for i in range(10):
            (self.tmp / f"f{i}.torrent").write_bytes(b"d1:ae")
        self.assertEqual(len(find_new_torrents(str(self.tmp), limit=4)), 4)

    def test_a_scan_error_is_swallowed(self):
        with patch.object(os, "scandir", side_effect=OSError("permission denied")):
            self.assertEqual(find_new_torrents(str(self.tmp)), [])


class TestTorrentFolderWatcher(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.watcher = TorrentFolderWatcher()
        self.addCleanup(self.watcher.stop)

    def _torrent(self, name="a.torrent") -> Path:
        path = self.tmp / name
        path.write_bytes(b"d1:ae")
        return path

    def _arm(self, **kwargs):
        """A watcher armed on the temp folder, with the periodic timer taken back off.

        Built per call rather than reusing ``self.watcher`` so per-test constructor arguments —
        a zero ``stability_ms`` above all — actually take effect.
        """
        watcher = TorrentFolderWatcher(**kwargs)
        self.addCleanup(watcher.stop)
        watcher.set_folder(str(self.tmp))
        watcher.set_enabled(True)
        watcher._rescan.stop()  # keep the test off the periodic timer
        return watcher

    def test_a_disabled_watcher_scans_nothing(self):
        self.watcher.set_folder(str(self.tmp))
        self._torrent()
        # The invariant the backlog timer also protects: a manager that is built but never
        # started must not go looking at the user's disk.
        self.assertEqual(self.watcher.scan(), [])

    def test_watcher_max_age_days_can_be_updated(self):
        self.assertEqual(self.watcher.max_age_days(), MAX_TORRENT_AGE_DAYS)
        self.watcher.set_max_age_days(10)
        self.assertEqual(self.watcher.max_age_days(), 10)

    def test_a_folder_is_required_to_arm(self):
        self.watcher.set_folder("")
        self.watcher.set_enabled(True)
        self.assertFalse(self.watcher.is_enabled())

    def test_a_file_is_only_reported_once_its_size_settles(self):
        watcher = self._arm(stability_ms=0)
        path = self._torrent()
        seen = []
        watcher.torrents_found.connect(seen.append)
        # First sighting records the size; it must not be reported yet, because a .torrent
        # dropped in is usually still being copied and a truncated one parses into an error row.
        self.assertEqual(watcher.scan(), [])
        # A second, larger write must also not be reported: the copy is still running.
        path.write_bytes(b"d1:ae" + b"0" * 500)
        self.assertEqual(watcher.scan(), [])
        self.assertEqual(watcher.scan(), [canonical_torrent_path(str(path))])
        self.assertEqual(seen, [[canonical_torrent_path(str(path))]])

    def test_a_stable_file_is_reported_on_the_second_scan(self):
        watcher = self._arm(stability_ms=0)
        self._torrent()
        self.assertEqual(watcher.scan(), [])
        self.assertEqual(len(watcher.scan()), 1)

    def test_a_file_inside_the_stability_window_is_not_reported(self):
        # Driven through a real (non-zero) threshold with an injected clock, because the tests
        # above set stability_ms=0 and would pass with the window check deleted entirely. This is
        # the guard against reading a .torrent that is still being copied, which parses into an
        # error row the user has to delete.
        watcher = self._arm(stability_ms=1000)
        path = self._torrent()
        expected = [canonical_torrent_path(str(path))]

        # First sighting only records the size.
        self.assertEqual(watcher._stable_candidates(now=100.0), [])
        # Unchanged, but only half the window has passed: the copy may still be in flight.
        self.assertEqual(watcher._stable_candidates(now=100.5), [])
        # Past the window with the size steady across it.
        self.assertEqual(watcher._stable_candidates(now=101.5), expected)

    def test_a_file_that_is_still_growing_never_counts_as_stable(self):
        watcher = self._arm(stability_ms=1000)
        path = self._torrent()
        self.assertEqual(watcher._stable_candidates(now=100.0), [])
        path.write_bytes(b"d1:ae" + b"0" * 900)
        # A growing file must reset the clock, or a slow copy is declared stable halfway through.
        self.assertEqual(watcher._stable_candidates(now=101.5), [])
        self.assertEqual(watcher._stable_candidates(now=102.0), [])
        self.assertEqual(
            watcher._stable_candidates(now=103.0), [canonical_torrent_path(str(path))]
        )

    def test_a_reported_file_is_never_reported_again(self):
        watcher = self._arm(stability_ms=0)
        self._torrent()
        watcher.scan()
        watcher.scan()
        for _ in range(3):
            self.assertEqual(watcher.scan(), [])

    def test_changing_the_folder_forgets_the_previous_history(self):
        watcher = self._arm(stability_ms=0)
        other = Path(tempfile.mkdtemp())
        other_t = other / "b.torrent"
        other_t.write_bytes(b"d1:ae")
        watcher.set_folder(str(other))
        watcher.set_enabled(True)
        watcher._rescan.stop()
        # The new folder's file is judged on its own merits, not skipped because it shares a
        # name with something the old folder produced.
        self.assertEqual(watcher.scan(), [])
        self.assertEqual(watcher.scan(), [canonical_torrent_path(str(other_t))])

    def test_a_file_that_vanishes_is_forgotten(self):
        watcher = self._arm(stability_ms=0)
        path = self._torrent()
        watcher.scan()
        path.unlink()
        self.assertEqual(watcher.scan(), [])

    def test_the_ledger_does_not_grow_without_bound(self):
        watcher = self._arm(stability_ms=0)
        self._torrent("a.torrent")
        watcher.scan()
        for i in range(5):
            self._torrent(f"f{i}.torrent")
            watcher.scan()
        (self.tmp / "f0.torrent").unlink()
        watcher.scan()
        # Every pending entry except the still-present ones has been dropped.
        self.assertLessEqual(len(watcher._pending), 6)

    def test_a_scan_error_is_contained(self):
        watcher = self._arm()
        self._torrent()
        with patch.object(
            TorrentFolderWatcher, "_stable_candidates", side_effect=RuntimeError("boom")
        ):
            # A QTimer slot that raises loses its connection and the watcher silently never
            # fires again, so the whole body is guarded.
            self.assertEqual(watcher.scan(), [])

    def test_stop_is_safe_to_call_twice(self):
        watcher = self._arm()
        watcher.stop()
        watcher.stop()
        self.assertFalse(watcher.is_enabled())
        self.assertEqual(watcher.scan(), [])


class TestManagerIngestionPolicy(unittest.TestCase):
    """`add_torrent_files` is the single policy for every .torrent ingress route."""

    def setUp(self):
        from my_idm.database import Database
        from my_idm.manager import DownloadManager

        self.tmp = Path(tempfile.mkdtemp())
        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)
        self.manager = DownloadManager(self.db)
        self.addCleanup(self.manager.stop)
        self.torrent = self.tmp / "a.torrent"
        self.torrent.write_bytes(b"d1:ae")

    def test_a_torrent_file_is_added(self):
        added = self.manager.add_torrent_files([str(self.torrent)])
        self.assertEqual(added, [canonical_torrent_path(str(self.torrent))])
        rows = self.db.get_all_downloads()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].download_type, "torrent")

    def test_two_spellings_of_one_path_add_one_row(self):
        # The reason paths are canonicalised here rather than at each call site.
        spelled = os.path.join(str(self.tmp), ".", "a.torrent")
        self.manager.add_torrent_files([str(self.torrent)])
        self.manager.add_torrent_files([spelled])
        self.assertEqual(len(self.db.get_all_downloads()), 1)

    def test_a_non_torrent_path_is_ignored(self):
        other = self.tmp / "notes.txt"
        other.write_text("hi")
        self.assertEqual(self.manager.add_torrent_files([str(other)]), [])

    def test_only_new_never_resumes_a_paused_torrent(self):
        # The reason the watched folder passes only_new: add_download treats a re-add of a
        # paused row as a request to resume it, so a periodic watcher would restart anything
        # the user paused. This is the behaviour that would be impossible to notice and
        # impossible to live with.
        path = canonical_torrent_path(str(self.torrent))
        self.manager.add_torrent_files([str(self.torrent)])
        row = self.db.get_all_downloads()[0]
        self.manager.pause_download(row.id)
        self.assertEqual(self.db.get_download(row.id).status, "paused")

        self.assertEqual(
            self.manager.add_torrent_files([str(self.torrent)], only_new=True), []
        )
        self.assertEqual(self.db.get_download(row.id).status, "paused")

    def test_the_source_is_recorded_on_the_row(self):
        self.manager.add_torrent_files([str(self.torrent)], source="drop")
        row = self.db.get_all_downloads()[0]
        self.assertEqual(row.metadata.get("capture_source"), "torrent_drop")

    def test_one_bad_path_does_not_cost_the_rest_of_the_batch(self):
        missing = self.tmp / "gone.torrent"
        added = self.manager.add_torrent_files([str(missing), str(self.torrent)])
        self.assertEqual(added, [canonical_torrent_path(str(self.torrent))])


class TestWatchedFolderIngestion(unittest.TestCase):
    """Manager-side wiring: what the watcher reports ends up in the table and on a signal."""

    def setUp(self):
        from my_idm.database import Database
        from my_idm.manager import DownloadManager

        self.tmp = Path(tempfile.mkdtemp())
        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)
        self.manager = DownloadManager(self.db)
        self.addCleanup(self.manager.stop)

    def test_reported_files_are_added_and_signalled(self):
        captured = []
        self.manager.torrent_folder_captured.connect(captured.append)
        path = self.tmp / "a.torrent"
        path.write_bytes(b"d1:ae")
        canonical = canonical_torrent_path(str(path))

        self.manager._on_watched_folder_torrents([canonical])

        self.assertEqual(captured, [[canonical]])
        self.assertEqual(len(self.db.get_all_downloads()), 1)

    def test_the_same_file_is_never_added_twice(self):
        captured = []
        self.manager.torrent_folder_captured.connect(captured.append)
        path = self.tmp / "a.torrent"
        path.write_bytes(b"d1:ae")
        canonical = canonical_torrent_path(str(path))

        self.manager._on_watched_folder_torrents([canonical])
        self.manager._on_watched_folder_torrents([canonical])

        self.assertEqual(len(captured), 1)
        self.assertEqual(len(self.db.get_all_downloads()), 1)

    def test_a_rescan_never_restarts_a_torrent_the_user_paused(self):
        # The property the session ledger alone does not provide, and the reason the watched
        # folder passes only_new. add_download treats a re-add of a paused row as a request to
        # resume it, so a watcher that rescans every minute would restart anything the user
        # paused — silently, minutes after they paused it.
        path = self.tmp / "a.torrent"
        path.write_bytes(b"d1:ae")
        canonical = canonical_torrent_path(str(path))

        self.manager._on_watched_folder_torrents([canonical])
        row = self.db.get_all_downloads()[0]
        self.manager.pause_download(row.id)
        self.assertEqual(self.db.get_download(row.id).status, "paused")

        # A restart forgets the ledger; the database is the boundary that still holds.
        self.manager._torrent_watch_seen.clear()
        self.manager._on_watched_folder_torrents([canonical])

        self.assertEqual(self.db.get_download(row.id).status, "paused")
        self.assertEqual(len(self.db.get_all_downloads()), 1)

    def test_nothing_is_signalled_when_everything_was_known(self):
        captured = []
        self.manager.torrent_folder_captured.connect(captured.append)
        self.assertIsNone(self.manager._on_watched_folder_torrents([]))
        self.assertEqual(captured, [])

    def test_watched_folder_files_are_kept_when_cleanup_is_disabled(self):
        from unittest.mock import patch
        from my_idm.config import TorrentConfig

        self.manager.set_torrent_config(TorrentConfig(clean_watched_torrent_files=False))
        path = self.tmp / "keep.torrent"
        path.write_bytes(b"d1:ae")
        canonical = canonical_torrent_path(str(path))

        with patch("my_idm.manager.send_to_trash") as mock_trash:
            self.manager._on_watched_folder_torrents([canonical])
            mock_trash.assert_not_called()
        self.assertTrue(path.exists())

    def test_watched_folder_files_are_cleaned_up_when_configured(self):
        from unittest.mock import patch
        from my_idm.config import TorrentConfig

        self.manager.set_torrent_config(TorrentConfig(clean_watched_torrent_files=True))
        path = self.tmp / "clean.torrent"
        path.write_bytes(b"d1:ae")
        canonical = canonical_torrent_path(str(path))

        with patch("my_idm.manager.send_to_trash", return_value=True) as mock_trash, \
             patch("my_idm.manager.unlock_path") as mock_unlock:
            self.manager._on_watched_folder_torrents([canonical])
            mock_unlock.assert_called_once_with(canonical)
            mock_trash.assert_called_once_with(canonical)

    def test_watched_folder_cleanup_ignores_files_not_added(self):
        from unittest.mock import patch
        from my_idm.config import TorrentConfig

        self.manager.set_torrent_config(TorrentConfig(clean_watched_torrent_files=True))
        path = self.tmp / "existing.torrent"
        path.write_bytes(b"d1:ae")
        canonical = canonical_torrent_path(str(path))

        # First add:
        self.manager._on_watched_folder_torrents([canonical])

        # Second attempt (already known in DB):
        with patch("my_idm.manager.send_to_trash") as mock_trash:
            self.manager._torrent_watch_seen.clear()
            self.manager._on_watched_folder_torrents([canonical])
            mock_trash.assert_not_called()

    def test_local_torrent_is_cached_in_fastresume_dir(self):
        from unittest.mock import patch

        path = self.tmp / "cache_me.torrent"
        path.write_bytes(b"d1:ad4:testee")
        canonical = canonical_torrent_path(str(path))

        with tempfile.TemporaryDirectory() as fr_tmp:
            fr_dir = Path(fr_tmp)
            with patch("my_idm.torrent_engine.FASTRESUME_DIR", fr_dir):
                added_id = self.manager.add_download(canonical)
                self.assertIsNotNone(added_id)
                cached = fr_dir / f"{added_id}.torrent"
                self.assertTrue(cached.exists())
                self.assertEqual(cached.read_bytes(), b"d1:ad4:testee")

    def test_the_watcher_is_not_armed_before_the_manager_starts(self):
        from my_idm.config import TorrentConfig

        cfg = TorrentConfig(watch_torrent_folder=True)
        self.manager.set_torrent_config(cfg)
        # Same invariant as the backlog poll: a manager built but never started must not scan
        # the user's downloads directory.
        self.assertFalse(self.manager._torrent_watcher.is_enabled())
        self.assertTrue(self.manager._torrent_watcher.folder())

    def test_manager_applies_torrent_watch_max_age_days(self):
        from my_idm.config import TorrentConfig

        self.manager.set_torrent_config(TorrentConfig(torrent_watch_max_age_days=14))
        self.assertEqual(self.manager._torrent_watcher.max_age_days(), 14)


class TestWatchFolderResolution(unittest.TestCase):
    def setUp(self):
        from my_idm.database import Database
        from my_idm.manager import DownloadManager

        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)
        self.manager = DownloadManager(self.db)
        self.addCleanup(self.manager.stop)

    def test_a_blank_setting_means_the_default_download_folder(self):
        from my_idm.config import TorrentConfig

        self.manager.set_torrent_config(TorrentConfig(torrent_watch_folder=""))
        self.assertEqual(
            self.manager.torrent_watch_folder(),
            self.manager.general_config.get_effective_save_path(),
        )

    def test_an_explicit_folder_wins(self):
        from my_idm.config import TorrentConfig

        self.manager.set_torrent_config(TorrentConfig(torrent_watch_folder="D:/t"))
        self.assertEqual(self.manager.torrent_watch_folder(), "D:/t")

    def test_the_folder_is_resolved_live_so_a_download_path_change_is_followed(self):
        from pathlib import Path

        from my_idm.config import GeneralConfig, TorrentConfig

        other = tempfile.mkdtemp()
        self.manager.set_torrent_config(TorrentConfig(torrent_watch_folder=""))
        self.manager.set_general_config(GeneralConfig(default_save_path=other))
        # Cached at configure-time it would keep watching the folder that was current when the
        # preference was first saved.
        self.assertEqual(
            self.manager.torrent_watch_folder(),
            self.manager.general_config.get_effective_save_path(),
        )
        self.assertTrue(os.path.samefile(other, self.manager.torrent_watch_folder()))
        self.assertEqual(
            self.manager._torrent_watcher.folder(),
            self.manager.general_config.get_effective_save_path(),
        )
        del Path


if __name__ == "__main__":
    unittest.main()
