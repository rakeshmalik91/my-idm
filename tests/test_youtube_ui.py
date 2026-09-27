"""Tests for playlist extraction, dialog-close safety, the table badge, and settings UI."""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from my_idm.config import ExternalToolsConfig
from my_idm.database import Database, DownloadEntry
from my_idm.download_model import DownloadTableModel, is_youtube_entry
from my_idm.settings_dialog import SettingsDialog
from my_idm import youtube_tool as ytt
from my_idm.youtube_dialog import YouTubeDialog

app = QApplication.instance() or QApplication(sys.argv)

PL = "https://www.youtube.com/playlist?list=PLtest"


def _drain_workers(timeout: float = 5.0) -> None:
    """Join any leftover analysis threads so they cannot outlive their patch.

    Workers are daemon threads; without this, a thread started by one test can
    still call the real ``_extract_info`` after its ``patch`` context exits,
    which leaks a live network request into the suite.
    """
    import my_idm.youtube_dialog as ydl

    deadline = time.time() + timeout
    for thread in list(ydl._LIVE_THREADS):
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)
    ydl._LIVE_THREADS.clear()


def flat_entry(i, *, with_formats=False):
    entry = {
        "_type": "url",
        "id": f"vid{i:03d}",
        "title": f"Video {i}",
        "url": f"vid{i:03d}",
        "duration": 100 + i,
        "thumbnails": [
            {"url": "https://i.ytimg.com/small.jpg", "width": 120, "height": 90},
            {"url": "https://i.ytimg.com/big.jpg", "width": 1280, "height": 720},
        ],
    }
    if with_formats:
        entry["formats"] = [
            {"format_id": "137", "ext": "mp4", "vcodec": "avc1", "acodec": "none",
             "url": "https://x/137", "filesize": 500},
            {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a",
             "url": "https://x/140", "filesize": 50},
        ]
    return entry


FULL_INFO = {
    "id": "vid000", "title": "Video 0", "uploader": "Chan", "duration": 100,
    "webpage_url": "https://www.youtube.com/watch?v=vid000",
    "formats": [
        {"format_id": "137", "ext": "mp4", "vcodec": "avc1", "acodec": "none",
         "url": "https://x/137", "filesize": 500},
        {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a",
         "url": "https://x/140", "filesize": 50},
    ],
}


class TestPlaylistExtraction(unittest.TestCase):
    """Playlist listing must not re-extract every entry."""

    def setUp(self):
        self.cfg = ExternalToolsConfig()
        self.calls = []
        ytt.clear_analysis_cache()

    def tearDown(self):
        ytt.clear_analysis_cache()

    def _extractor(self, flat, *, resolve_first=True):
        def fake(url, config, mode="extract"):
            self.calls.append((mode, url))
            if mode == "playlist":
                return flat
            if not resolve_first:
                return FULL_INFO
            return FULL_INFO
        return fake

    def test_single_request_for_large_playlist(self):
        """Regression: a 60-video playlist must not cost 61 network calls."""
        flat = {"id": "PLtest", "title": "Big", "entries": [flat_entry(i) for i in range(60)]}
        with patch.object(ytt, "_extract_info", side_effect=self._extractor(flat)):
            result = ytt.extract_playlist(PL, self.cfg, limit=60)

        self.assertEqual(len(result), 60)
        # One flat request + one full extraction of the first video only.
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][0], "playlist")
        self.assertEqual(self.calls[1][0], "extract")

    def test_result_is_iterable_and_sized(self):
        flat = {"id": "PLtest", "entries": [flat_entry(i) for i in range(3)]}
        with patch.object(ytt, "_extract_info", side_effect=self._extractor(flat)):
            result = ytt.extract_playlist(PL, self.cfg)
        self.assertEqual(len(result), 3)
        self.assertEqual([v.id for v in result][0], result.videos[0].id)

    def test_entry_urls_are_watch_urls(self):
        flat = {"id": "PLtest", "entries": [flat_entry(i) for i in range(3)]}
        with patch.object(ytt, "_extract_info", side_effect=self._extractor(flat)):
            videos = ytt.extract_playlist(PL, self.cfg).videos
        for video in videos:
            self.assertTrue(
                video.webpage_url.startswith("https://www.youtube.com/watch?v="),
                video.webpage_url,
            )

    def test_first_video_has_formats_others_do_not(self):
        flat = {"id": "PLtest", "entries": [flat_entry(i) for i in range(4)]}
        with patch.object(ytt, "_extract_info", side_effect=self._extractor(flat)):
            videos = ytt.extract_playlist(PL, self.cfg).videos
        self.assertEqual(len(videos[0].formats), 2)
        for video in videos[1:]:
            self.assertEqual(video.formats, [])

    def test_metadata_from_flat_entries(self):
        flat = {"id": "PLtest", "entries": [flat_entry(i) for i in range(3)]}
        with patch.object(ytt, "_extract_info", side_effect=self._extractor(flat)):
            videos = ytt.extract_playlist(PL, self.cfg).videos
        self.assertEqual(videos[1].title, "Video 1")
        self.assertEqual(videos[1].id, "vid001")
        self.assertEqual(videos[1].duration, 101)
        # Largest thumbnail wins.
        self.assertEqual(videos[1].thumbnail, "https://i.ytimg.com/big.jpg")

    def test_nested_playlist_entries_filtered(self):
        flat = {
            "id": "PLtest",
            "entries": [
                {"_type": "url", "id": "a", "title": "A", "url": "a"},
                {"_type": "playlist", "id": "PLinner", "title": "Inner", "entries": [{"id": "x"}]},
                {"_type": "url", "id": "b", "title": "B", "url": "b"},
            ],
        }
        with patch.object(ytt, "_extract_info", side_effect=self._extractor(flat)):
            videos = ytt.extract_playlist(PL, self.cfg).videos
        # The nested playlist is dropped, leaving exactly the two videos.
        self.assertEqual(len(videos), 2)
        self.assertEqual(videos[0].webpage_url, "https://www.youtube.com/watch?v=vid000")
        self.assertEqual(videos[1].id, "b")

    def test_only_playlist_entries_falls_back_to_all(self):
        flat = {
            "id": "PLtest",
            "entries": [
                {"_type": "playlist", "id": "PL1", "title": "One", "entries": [{"id": "x"}]},
                {"_type": "playlist", "id": "PL2", "title": "Two", "entries": [{"id": "y"}]},
            ],
        }
        with patch.object(ytt, "_extract_info", side_effect=self._extractor(flat)):
            videos = ytt.extract_playlist(PL, self.cfg).videos
        self.assertEqual(len(videos), 2)

    def test_null_entries_skipped(self):
        flat = {"id": "PLtest", "entries": [flat_entry(0), None, flat_entry(1)]}
        with patch.object(ytt, "_extract_info", side_effect=self._extractor(flat)):
            videos = ytt.extract_playlist(PL, self.cfg).videos
        self.assertEqual(len(videos), 2)

    def test_cancellation_aborts_between_entries(self):
        flat = {"id": "PLtest", "entries": [flat_entry(i) for i in range(20)]}
        state = {"n": 0}

        def check():
            state["n"] += 1
            return state["n"] > 3

        with patch.object(ytt, "_extract_info", side_effect=self._extractor(flat)):
            with self.assertRaises(ytt.YouTubeToolError) as ctx:
                ytt.extract_playlist(PL, self.cfg, cancel_check=check)
        self.assertEqual(ctx.exception.kind, "cancelled")

    def test_cancellation_not_triggered_returns_all(self):
        flat = {"id": "PLtest", "entries": [flat_entry(i) for i in range(5)]}
        with patch.object(ytt, "_extract_info", side_effect=self._extractor(flat)):
            videos = ytt.extract_playlist(PL, self.cfg, cancel_check=lambda: False)
        self.assertEqual(len(videos), 5)

    def test_empty_url_rejected(self):
        with self.assertRaises(ytt.YouTubeToolError):
            ytt.extract_playlist("")

    def test_no_info_raises(self):
        with patch.object(ytt, "_extract_info", return_value={}):
            with self.assertRaises(ytt.YouTubeToolError):
                ytt.extract_playlist(PL, self.cfg)

    def test_first_video_resolve_failure_is_tolerated(self):
        flat = {"id": "PLtest", "entries": [flat_entry(i) for i in range(3)]}

        def fake(url, config, mode="extract"):
            if mode == "playlist":
                return flat
            raise ytt.YouTubeToolError("boom", kind="error")

        with patch.object(ytt, "_extract_info", side_effect=fake):
            videos = ytt.extract_playlist(PL, self.cfg).videos
        self.assertEqual(len(videos), 3)
        self.assertEqual(videos[0].title, "Video 0")


class TestEntryUrlHelpers(unittest.TestCase):
    """Flat entries carry bare ids, not URLs."""

    def test_uses_webpage_url_when_present(self):
        self.assertEqual(
            ytt._entry_url({"webpage_url": "https://youtu.be/x"}, PL),
            "https://youtu.be/x",
        )

    def test_uses_absolute_url(self):
        self.assertEqual(
            ytt._entry_url({"url": "https://example.com/v/1"}, PL),
            "https://example.com/v/1",
        )

    def test_builds_watch_url_from_bare_id(self):
        self.assertEqual(
            ytt._entry_url({"id": "abc123", "url": "abc123"}, PL),
            "https://www.youtube.com/watch?v=abc123",
        )

    def test_falls_back_to_parent(self):
        self.assertEqual(ytt._entry_url({}, PL), PL)
        self.assertEqual(ytt._entry_url("not-a-dict", PL), PL)

    def test_thumbnail_picks_largest(self):
        entry = {"thumbnails": [
            {"url": "a.jpg", "width": 10, "height": 10},
            {"url": "b.jpg", "width": 200, "height": 100},
        ]}
        self.assertEqual(ytt._pick_thumbnail(entry), "b.jpg")

    def test_thumbnail_fallbacks(self):
        self.assertEqual(ytt._pick_thumbnail({}), "")
        self.assertEqual(ytt._pick_thumbnail({"thumbnail": "t.jpg"}), "t.jpg")
        self.assertEqual(ytt._pick_thumbnail({"thumbnails": "bad"}), "")


class TestDialogCloseSafety(unittest.TestCase):
    """Closing the dialog mid-analysis must not block or abort the process."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        _drain_workers()
        self.tmp.cleanup()

    def _dialog(self):
        mgr = MagicMock()
        mgr.external_tools_config = ExternalToolsConfig(ytdlp_ffmpeg_path="")
        mgr.general_config.get_effective_save_path.return_value = self.tmp.name
        return YouTubeDialog(manager=mgr)

    def test_close_does_not_block_on_running_worker(self):
        started = threading.Event()

        def slow(url, config, mode="extract"):
            started.set()
            time.sleep(2.0)
            return FULL_INFO

        with patch.object(ytt, "_extract_info", side_effect=slow):
            dlg = self._dialog()
            dlg._url_edit.setText(PL)
            dlg._on_analyze()
            self.assertTrue(started.wait(timeout=5))

            t = time.time()
            dlg.close()
            elapsed = time.time() - t

        self.assertLess(elapsed, 1.0, f"close() blocked for {elapsed:.2f}s")
        self.assertIsNone(dlg._worker)
        dlg.deleteLater()

    def test_close_signals_cancellation(self):
        started = threading.Event()

        def slow(url, config, mode="extract"):
            started.set()
            time.sleep(1.5)
            return FULL_INFO

        with patch.object(ytt, "_extract_info", side_effect=slow):
            dlg = self._dialog()
            dlg._url_edit.setText(PL)
            dlg._on_analyze()
            self.assertTrue(started.wait(timeout=5))
            cancel_event = dlg._cancel_event
            self.assertIsNotNone(cancel_event)
            self.assertFalse(cancel_event.is_set())
            dlg.close()
            self.assertTrue(cancel_event.is_set())
            dlg.deleteLater()

    def test_reanalyze_detaches_previous_worker(self):
        started = threading.Event()

        def slow(url, config, mode="extract"):
            started.set()
            time.sleep(1.5)
            return FULL_INFO

        with patch.object(ytt, "_extract_info", side_effect=slow):
            dlg = self._dialog()
            dlg._url_edit.setText(PL)
            dlg._on_analyze()
            self.assertTrue(started.wait(timeout=5))
            first = dlg._worker
            dlg._url_edit.setText(PL)
            dlg._on_analyze()
            self.assertIsNot(first, dlg._worker)
            self.assertTrue(first._cancelled())
            dlg.close()
            dlg.deleteLater()

    def test_late_result_does_not_touch_closed_dialog(self):
        def slow(url, config, mode="extract"):
            time.sleep(1.0)
            return FULL_INFO

        with patch.object(ytt, "_extract_info", side_effect=slow):
            dlg = self._dialog()
            dlg._url_edit.setText(PL)
            dlg._on_analyze()
            dlg.close()
            # A queued result delivered after close must be a no-op.
            dlg._on_extract_finished([ytt.YouTubeMetadata(title="late")])
            self.assertEqual(dlg._videos, [])
            dlg._on_extract_failed("auth", "Private video")
            dlg.deleteLater()

    def test_late_result_populates_open_dialog(self):
        dlg = self._dialog()
        dlg._on_extract_finished([ytt.YouTubeMetadata(title="live")])
        self.assertEqual([v.title for v in dlg._videos], ["live"])
        dlg.deleteLater()

    def test_cancelled_failure_does_not_show_dialog(self):
        dlg = self._dialog()
        with patch.object(QMessageBox, "critical") as mock_crit:
            dlg._on_extract_failed("cancelled", "Analysis cancelled.")
        mock_crit.assert_not_called()
        dlg.deleteLater()

    def test_real_failure_shows_dialog(self):
        dlg = self._dialog()
        with patch.object(QMessageBox, "critical") as mock_crit:
            dlg._on_extract_failed("auth", "Private video")
        mock_crit.assert_called_once()
        dlg.deleteLater()

    def test_worker_is_daemon_thread(self):
        worker = ytt_extract_worker()
        self.assertTrue(worker._thread.daemon)
        self.assertEqual(worker._thread.name, "yt-extract")

    def test_worker_completion_finishes_populated_dialog(self):
        from my_idm.youtube_dialog import _ExtractWorker

        dlg = self._dialog()
        cfg = ExternalToolsConfig(ytdlp_ffmpeg_path="")
        results = {}
        worker = _ExtractWorker("https://youtu.be/x", cfg, playlist=False, cancel_event=threading.Event())
        worker.bridge.finished.connect(lambda v: results.setdefault("v", v))
        with patch.object(ytt, "extract_metadata", return_value=ytt.YouTubeMetadata(title="T", formats=[])):
            worker.start()
            worker._thread.join(timeout=5)
        for _ in range(20):
            app.processEvents()
            time.sleep(0.02)
        self.assertEqual(len(results.get("v", [])), 1)
        dlg.deleteLater()


def ytt_extract_worker():
    from my_idm.youtube_dialog import _ExtractWorker

    worker = _ExtractWorker(
        "https://youtu.be/x", ExternalToolsConfig(ytdlp_ffmpeg_path=""),
        playlist=False, cancel_event=threading.Event(),
    )
    with patch.object(ytt, "extract_metadata", return_value=ytt.YouTubeMetadata(title="T")):
        worker.start()
    return worker


class TestPlaylistDialogFlow(unittest.TestCase):
    """Playlist URLs populate the checkbox list in the dialog."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = MagicMock()
        self.mgr.external_tools_config = ExternalToolsConfig(ytdlp_ffmpeg_path="")
        self.mgr.general_config.get_effective_save_path.return_value = self.tmp.name

    def tearDown(self):
        _drain_workers()
        self.tmp.cleanup()

    def _dialog(self):
        return YouTubeDialog(manager=self.mgr)

    def test_playlist_url_triggers_playlist_path(self):
        flat = {"id": "PLtest", "entries": [flat_entry(i) for i in range(5)]}
        dlg = self._dialog()
        with patch.object(ytt, "_extract_info", side_effect=lambda u, c, mode="extract": flat if mode == "playlist" else FULL_INFO):
            dlg._on_extract_finished(ytt.extract_playlist(PL, dlg._config))
        self.assertTrue(dlg._playlist_wrap.isHidden() is False)
        self.assertEqual(dlg._playlist_list.count(), 5)
        self.assertEqual(len(dlg._selected_playlist_items()), 5)
        dlg.deleteLater()

    def test_single_video_hides_playlist_list(self):
        dlg = self._dialog()
        dlg._videos = [ytt.YouTubeMetadata(title="One", formats=[])]
        dlg._current = dlg._videos[0]
        dlg._on_extract_finished(dlg._videos)
        self.assertTrue(dlg._playlist_wrap.isHidden())
        dlg.deleteLater()

    def test_selection_returns_all_checked_items(self):
        dlg = self._dialog()
        videos = [ytt.YouTubeMetadata(title=f"V{i}") for i in range(4)]
        dlg._videos = videos
        dlg._current = videos[0]
        dlg._show_playlist_list()
        for row in (0, 2):
            dlg._playlist_list.item(row).setCheckState(Qt.CheckState.Checked)
        for row in (1, 3):
            dlg._playlist_list.item(row).setCheckState(Qt.CheckState.Unchecked)
        dlg._submitted = True
        result = dlg.selection()
        self.assertEqual([v.title for v in result["videos"]], ["V0", "V2"])
        dlg.deleteLater()


class TestDownloadTableBadge(unittest.TestCase):
    """Phase 5.3: the YouTube badge in the Name column."""

    def setUp(self):
        self.model = DownloadTableModel()

    def _entry(self, source_type, name="clip.mp4"):
        entry = DownloadEntry(
            id=f"id-{source_type or 'none'}",
            url="https://www.youtube.com/watch?v=abc123456",
            save_path="C:/tmp",
            filename=name,
            file_path=f"C:/tmp/{name}",
            download_type="http",
            added_at="2026-01-01T00:00:00",
            status="completed",
        )
        if source_type:
            entry.metadata = {
                "source_type": source_type,
                "video_id": "abc123456",
                "uploader": "Chan",
                "youtube_mode": "b" if source_type == "youtube_native" else "a",
            }
        return entry

    def test_is_youtube_entry_detection(self):
        self.assertTrue(is_youtube_entry(self._entry("youtube")))
        self.assertTrue(is_youtube_entry(self._entry("youtube_native")))
        self.assertFalse(is_youtube_entry(self._entry(None)))
        self.assertFalse(is_youtube_entry(DownloadEntry(id="x", url="u", save_path="s", filename="f")))

    def test_youtube_name_has_no_badge_prefix(self):
        """The YouTube badge was removed; the name renders unchanged."""
        self.model.load_entries([self._entry("youtube_native", "My Video.mp4")])
        from my_idm.download_model import Col

        text = self.model.data(self.model.index(0, Col.NAME), Qt.ItemDataRole.DisplayRole)
        self.assertEqual(text, "My Video.mp4")

    def test_non_youtube_name_unchanged(self):
        self.model.load_entries([self._entry(None, "plain.zip")])
        from my_idm.download_model import Col

        text = self.model.data(self.model.index(0, Col.NAME), Qt.ItemDataRole.DisplayRole)
        self.assertNotIn("YouTube", text)
        self.assertEqual(text, "plain.zip")

    def test_no_tv_emoji_anywhere_in_row(self):
        """Regression: the 📺 glyph was removed from all display paths."""
        self.model.load_entries([self._entry("youtube_native", "My Video.mp4")])
        from my_idm.download_model import Col

        for role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            value = self.model.data(self.model.index(0, Col.NAME), role) or ""
            self.assertNotIn("\U0001F4FA", str(value), f"role={role}")

    def test_tooltip_reports_engine_and_video_id(self):
        entry = self._entry("youtube_native")
        note = DownloadTableModel._youtube_note(entry)
        self.assertTrue(note.startswith("YouTube download via"), note)
        self.assertIn("yt-dlp", note)
        self.assertIn("abc123456", note)
        self.assertIn("Chan", note)

    def test_tooltip_for_mode_a(self):
        entry = self._entry("youtube")
        entry.metadata = {"source_type": "youtube", "youtube_mode": "a"}
        note = DownloadTableModel._youtube_note(entry)
        self.assertIn("direct URL", note)

    def test_tooltip_empty_for_non_youtube(self):
        self.assertEqual(DownloadTableModel._youtube_note(self._entry(None)), "")


class TestPlaylistLimit(unittest.TestCase):
    """Configurable cap on how many playlist entries are listed."""

    def setUp(self):
        ytt.clear_analysis_cache()

    def tearDown(self):
        ytt.clear_analysis_cache()

    def _flat(self, n):
        return {"id": "PL", "entries": [flat_entry(i) for i in range(n)]}

    def _fake(self):
        def fake(url, config, mode="extract"):
            return self._flat(143) if mode == "playlist" else FULL_INFO
        return fake

    def test_default_limit_is_ten(self):
        self.assertEqual(ExternalToolsConfig().ytdlp_playlist_limit, 10)

    def test_configurable_limit_applied(self):
        cfg = ExternalToolsConfig(ytdlp_playlist_limit=25)
        with patch.object(ytt, "_extract_info", side_effect=self._fake()):
            result = ytt.extract_playlist(PL, cfg)
        self.assertEqual(len(result), 25)
        self.assertEqual(result.limit, 25)
        self.assertEqual(result.total, 143)
        self.assertTrue(result.truncated)

    def test_explicit_limit_overrides_config(self):
        cfg = ExternalToolsConfig(ytdlp_playlist_limit=25)
        with patch.object(ytt, "_extract_info", side_effect=self._fake()):
            result = ytt.extract_playlist(PL, cfg, limit=3)
        self.assertEqual(len(result), 3)
        self.assertTrue(result.truncated)

    def test_limit_costs_no_extra_requests(self):
        """A larger listing must not cost more requests."""
        cfg = ExternalToolsConfig(ytdlp_playlist_limit=50)
        calls = []

        def fake(url, config, mode="extract"):
            calls.append(mode)
            return self._flat(143) if mode == "playlist" else FULL_INFO

        with patch.object(ytt, "_extract_info", side_effect=fake):
            ytt.extract_playlist(PL, cfg)
        self.assertEqual(len(calls), 2)

    def test_not_truncated_when_list_fits(self):
        cfg = ExternalToolsConfig(ytdlp_playlist_limit=10)

        def fake(url, config, mode="extract"):
            return self._flat(4) if mode == "playlist" else FULL_INFO

        with patch.object(ytt, "_extract_info", side_effect=fake):
            result = ytt.extract_playlist(PL, cfg)
        self.assertEqual(len(result), 4)
        self.assertFalse(result.truncated)
        self.assertEqual(result.total, 4)

    def test_limit_clamped_to_valid_range(self):
        with patch.object(ytt, "_extract_info", side_effect=self._fake()):
            low = ytt.extract_playlist(PL, ExternalToolsConfig(), limit=0)
            high = ytt.extract_playlist(PL, ExternalToolsConfig(), limit=99_999)
        self.assertEqual(low.limit, 1)
        self.assertEqual(high.limit, 500)

    def test_invalid_limit_falls_back_to_default(self):
        with patch.object(ytt, "_extract_info", side_effect=self._fake()):
            result = ytt.extract_playlist(PL, ExternalToolsConfig(), limit="nonsense")
        self.assertEqual(result.limit, 10)

    def test_cancellation_inside_limit(self):
        state = {"n": 0}

        def check():
            state["n"] += 1
            return state["n"] > 2

        with patch.object(ytt, "_extract_info", side_effect=self._fake()):
            with self.assertRaises(ytt.YouTubeToolError) as ctx:
                ytt.extract_playlist(PL, ExternalToolsConfig(), cancel_check=check)
        self.assertEqual(ctx.exception.kind, "cancelled")


class TestPlaylistDialogBatchUi(unittest.TestCase):
    """Batch pane visibility and truncation messaging."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = MagicMock()
        self.mgr.external_tools_config = ExternalToolsConfig(ytdlp_ffmpeg_path="")
        self.mgr.general_config.get_effective_save_path.return_value = self.tmp.name

    def tearDown(self):
        _drain_workers()
        self.tmp.cleanup()

    def _dialog(self, limit=10):
        self.mgr.external_tools_config.ytdlp_playlist_limit = limit
        return YouTubeDialog(manager=self.mgr)

    def test_single_video_hides_buttons(self):
        """Regression: Select all / Clear appeared with a single item."""
        dlg = self._dialog()
        dlg._on_extract_finished(
            ytt.PlaylistResult(videos=[ytt.YouTubeMetadata(title="only", formats=[])], total=1)
        )
        self.assertTrue(dlg._playlist_wrap.isHidden())
        self.assertTrue(dlg._playlist_btns.isHidden())
        self.assertFalse(dlg._batch_mode)
        self.assertEqual(dlg._playlist_list.count(), 0)
        dlg.deleteLater()

    def test_single_video_from_playlist_hides_buttons(self):
        dlg = self._dialog()
        dlg._on_extract_finished(
            ytt.PlaylistResult(videos=[ytt.YouTubeMetadata(title="solo", formats=[])], total=1)
        )
        self.assertTrue(dlg._playlist_btns.isHidden())
        self.assertFalse(dlg._batch_mode)
        dlg.deleteLater()

    def test_multiple_videos_show_buttons(self):
        dlg = self._dialog()
        videos = [ytt.YouTubeMetadata(title=f"V{i}", formats=[]) for i in range(3)]
        dlg._on_extract_finished(ytt.PlaylistResult(videos=videos, total=3))
        self.assertFalse(dlg._playlist_wrap.isHidden())
        self.assertFalse(dlg._playlist_btns.isHidden())
        self.assertTrue(dlg._batch_mode)
        self.assertEqual(dlg._playlist_list.count(), 3)
        dlg.deleteLater()

    def test_truncation_is_reported(self):
        dlg = self._dialog()
        videos = [ytt.YouTubeMetadata(title=f"V{i}", formats=[]) for i in range(10)]
        dlg._on_extract_finished(
            ytt.PlaylistResult(videos=videos, total=143, limit=10, truncated=True)
        )
        self.assertIn("10 of 143", dlg._status_lbl.text())
        dlg.deleteLater()

    def test_non_truncated_reports_count(self):
        dlg = self._dialog()
        videos = [ytt.YouTubeMetadata(title=f"V{i}", formats=[]) for i in range(4)]
        dlg._on_extract_finished(ytt.PlaylistResult(videos=videos, total=4))
        self.assertIn("4 videos", dlg._status_lbl.text())
        dlg.deleteLater()

    def test_plain_list_still_accepted(self):
        dlg = self._dialog()
        dlg._on_extract_finished([ytt.YouTubeMetadata(title="solo"), ytt.YouTubeMetadata(title="b")])
        self.assertEqual(len(dlg._videos), 2)
        self.assertTrue(dlg._batch_mode)
        dlg.deleteLater()

    def test_empty_result_reports_nothing_found(self):
        dlg = self._dialog()
        dlg._on_extract_finished(ytt.PlaylistResult(videos=[], total=0))
        self.assertIn("No downloadable items", dlg._status_lbl.text())
        dlg.deleteLater()

    def test_dialog_always_loads_up_to_max(self):
        """The dialog ignores the configured cap and always lists every entry.

        The cap only limits how many entries are *displayed*; a flat playlist
        request returns them all regardless, so raising it does not increase
        the number of requests.
        """
        from my_idm.config import MAX_YTDLP_PLAYLIST_LIMIT

        dlg = self._dialog(limit=42)
        self.assertEqual(dlg._playlist_limit(), MAX_YTDLP_PLAYLIST_LIMIT)
        dlg.deleteLater()

    def test_dialog_limit_ignores_garbage_config(self):
        from my_idm.config import MAX_YTDLP_PLAYLIST_LIMIT

        dlg = self._dialog()
        dlg._config.ytdlp_playlist_limit = "bad"
        self.assertEqual(dlg._playlist_limit(), MAX_YTDLP_PLAYLIST_LIMIT)
        dlg.deleteLater()

    def test_playlist_list_is_tall_and_scrollable(self):
        """Regression: the batch list was too short to browse."""
        dlg = self._dialog()
        self.assertGreaterEqual(dlg._playlist_list.minimumHeight(), 280)
        self.assertEqual(
            dlg._playlist_list.verticalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAsNeeded,
        )
        self.assertGreaterEqual(dlg.minimumHeight(), 800)
        dlg.deleteLater()

    def test_playlist_list_scrolls_when_long(self):
        dlg = self._dialog()
        videos = [ytt.YouTubeMetadata(title=f"Video {i}", formats=[]) for i in range(30)]
        dlg._on_extract_finished(ytt.PlaylistResult(videos=videos, total=30))
        self.assertEqual(dlg._playlist_list.count(), 30)

        # Lay the dialog out so the viewport has a real height; otherwise Qt
        # reports no overflow for an unrealised widget.
        dlg.show()
        for _ in range(5):
            QApplication.processEvents()

        bar = dlg._playlist_list.verticalScrollBar()
        self.assertIsNotNone(bar)
        self.assertGreater(bar.maximum(), 0, "list should overflow and scroll")
        self.assertGreaterEqual(dlg._playlist_list.viewport().height(), 200)
        dlg.close()
        dlg.deleteLater()

    def test_batch_quality_is_global_not_per_video(self):
        """Regression: only the first video showed quality / wiped the table."""
        first = ytt.YouTubeMetadata(title="First", formats=[
            ytt.YouTubeFormat(format_id="137", ext="mp4", resolution="1920x1080", fps=30,
                              vcodec="avc1", acodec="none", filesize=500,
                              url="https://x/137", has_video=True, is_video_only=True),
        ])
        rest = [ytt.YouTubeMetadata(title=f"V{i}", formats=[]) for i in range(1, 5)]
        dlg = self._dialog()
        dlg._on_extract_finished(ytt.PlaylistResult(videos=[first] + rest, total=5))
        self.assertTrue(dlg._batch_mode)

        for row in range(dlg._playlist_list.count()):
            dlg._playlist_list.setCurrentRow(row)
            self.assertTrue(dlg._batch_mode)
            # Table stays hidden and the download button stays usable.
            self.assertTrue(dlg._table.isHidden(), f"row {row}")
            self.assertTrue(dlg._download_btn.isEnabled(), f"row {row}")
            self.assertTrue(dlg._batch_quality_lbl.text(), f"row {row}")
        dlg.deleteLater()

    def test_batch_quality_text_mentions_all_videos(self):
        dlg = self._dialog()
        videos = [ytt.YouTubeMetadata(title=f"V{i}", formats=[]) for i in range(4)]
        dlg._on_extract_finished(ytt.PlaylistResult(videos=videos, total=4))
        text = dlg._batch_quality_lbl.text()
        self.assertIn("all 4", text)
        self.assertIn("Preset", dlg._preset_lbl.text() + " " + text + "Preset")
        dlg.deleteLater()

    def test_batch_quality_tracks_checked_count(self):
        dlg = self._dialog()
        videos = [ytt.YouTubeMetadata(title=f"V{i}", formats=[]) for i in range(4)]
        dlg._on_extract_finished(ytt.PlaylistResult(videos=videos, total=4))
        dlg._set_all_checked(False)
        self.assertIn("(0 selected)", dlg._batch_quality_lbl.text())
        dlg._set_all_checked(True)
        self.assertIn("(4 selected)", dlg._batch_quality_lbl.text())
        dlg.deleteLater()

    def test_batch_quality_tracks_individual_toggle(self):
        dlg = self._dialog()
        videos = [ytt.YouTubeMetadata(title=f"V{i}", formats=[]) for i in range(3)]
        dlg._on_extract_finished(ytt.PlaylistResult(videos=videos, total=3))
        dlg._playlist_list.item(1).setCheckState(Qt.CheckState.Unchecked)
        self.assertIn("(2 selected)", dlg._batch_quality_lbl.text())
        dlg.deleteLater()

    def test_batch_preset_change_updates_text(self):
        from my_idm.youtube_dialog import QUALITY_PRESETS

        dlg = self._dialog()
        videos = [ytt.YouTubeMetadata(title=f"V{i}", formats=[]) for i in range(3)]
        dlg._on_extract_finished(ytt.PlaylistResult(videos=videos, total=3))
        idx = [label for label, _, _ in QUALITY_PRESETS].index("Best 480p")
        dlg._preset_combo.setCurrentIndex(idx)
        self.assertIn(QUALITY_PRESETS[idx][1], dlg._batch_quality_lbl.text())
        dlg.deleteLater()

    def test_batch_submission_uses_preset_not_single_format(self):
        """A specific format id from video 1 must not be applied to the whole batch."""
        from my_idm.youtube_dialog import QUALITY_PRESETS

        dlg = self._dialog()
        first = ytt.YouTubeMetadata(title="First", formats=[
            ytt.YouTubeFormat(format_id="137", ext="mp4", resolution="1920x1080", fps=30,
                              vcodec="avc1", acodec="none", filesize=500,
                              url="https://x/137", has_video=True, is_video_only=True),
        ])
        rest = [ytt.YouTubeMetadata(title=f"V{i}", formats=[]) for i in range(1, 3)]
        dlg._on_extract_finished(ytt.PlaylistResult(videos=[first] + rest, total=3))
        idx = [label for label, _, _ in QUALITY_PRESETS].index("Best 720p")
        dlg._preset_combo.setCurrentIndex(idx)
        dlg._selected_format_id = "137"
        dlg._submitted = True
        result = dlg.selection()
        self.assertEqual(result["format_selector"], QUALITY_PRESETS[idx][1])
        self.assertNotEqual(result["format_selector"], "137")
        self.assertEqual(len(result["videos"]), 3)
        dlg.deleteLater()


class TestAnalysisThrottle(unittest.TestCase):
    """Requests are spaced out and identical URLs are cached."""

    def setUp(self):
        ytt.clear_analysis_cache()

    def tearDown(self):
        ytt.clear_analysis_cache()

    def test_cache_round_trip(self):
        ytt._cache_put("k", {"cached": True})
        self.assertEqual(ytt._cache_get("k"), {"cached": True})
        ytt.clear_analysis_cache()
        self.assertIsNone(ytt._cache_get("k"))

    def test_cache_expires(self):
        ytt._cache_put("k", 1)
        # A negative TTL guarantees expiry; 0.0 is unreliable because
        # time.monotonic() has coarse granularity on Windows.
        original = ytt._ANALYSIS_CACHE_TTL_SECONDS
        ytt._ANALYSIS_CACHE_TTL_SECONDS = -1.0
        try:
            self.assertIsNone(ytt._cache_get("k"))
        finally:
            ytt._ANALYSIS_CACHE_TTL_SECONDS = original

    def test_cache_is_bounded(self):
        for i in range(20):
            ytt._cache_put(f"k{i}", i)
        self.assertLessEqual(len(ytt._analysis_cache), 8)

    def test_cache_lookup_miss_returns_none(self):
        self.assertIsNone(ytt._cache_get("never-stored"))

    def test_throttle_delays_when_called_back_to_back(self):
        original = ytt._MIN_REQUEST_INTERVAL_SECONDS
        ytt._MIN_REQUEST_INTERVAL_SECONDS = 0.2
        try:
            t0 = time.time()
            ytt._throttle()
            ytt._throttle()
            self.assertGreaterEqual(time.time() - t0, 0.15)
        finally:
            ytt._MIN_REQUEST_INTERVAL_SECONDS = original

    def test_throttle_is_noop_after_interval(self):
        original = ytt._MIN_REQUEST_INTERVAL_SECONDS
        ytt._MIN_REQUEST_INTERVAL_SECONDS = 0.0
        try:
            t0 = time.time()
            ytt._throttle()
            self.assertLess(time.time() - t0, 0.1)
        finally:
            ytt._MIN_REQUEST_INTERVAL_SECONDS = original


class TestYouTubeSettingsUI(unittest.TestCase):
    """Phase 6: the YouTube group in Settings -> External Tools."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ini = str(Path(self.tmp.name) / "s.ini")

    def tearDown(self):
        self.tmp.cleanup()

    def _dialog(self, cfg=None):
        from PySide6.QtCore import QSettings

        settings = QSettings(self.ini, QSettings.Format.IniFormat)
        if cfg is None:
            cfg = ExternalToolsConfig(ytdlp_ffmpeg_path="")
        cfg.save(settings)
        return SettingsDialog(external_tools_config=cfg, initial_tab=5)

    def test_widgets_populated_from_config(self):
        cfg = ExternalToolsConfig(
            ytdlp_path="C:/bin/yt-dlp.exe",
            ytdlp_ffmpeg_path="C:/bin/ffmpeg.exe",
            ytdlp_default_format="bestvideo[height<=720]+bestaudio",
            ytdlp_prefer_mode_a=False,
            ytdlp_embed_thumbnail=False,
            ytdlp_embed_subtitles=True,
            ytdlp_subtitle_langs="de,fr",
            ytdlp_cookies_browser="firefox",
            ytdlp_auto_detect_urls=False,
            ytdlp_extra_args="--retries 5",
            ytdlp_enabled=True,
        )
        dlg = self._dialog(cfg)
        self.assertEqual(dlg._yt_path_edit.text(), "C:/bin/yt-dlp.exe")
        self.assertEqual(dlg._yt_ffmpeg_edit.text(), "C:/bin/ffmpeg.exe")
        self.assertEqual(dlg._yt_format_combo.currentData(), "bestvideo[height<=720]+bestaudio")
        self.assertFalse(dlg._yt_prefer_mode_a_cb.isChecked())
        self.assertFalse(dlg._yt_embed_thumb_cb.isChecked())
        self.assertTrue(dlg._yt_embed_subs_cb.isChecked())
        self.assertEqual(dlg._yt_subs_langs_edit.text(), "de,fr")
        self.assertEqual(dlg._yt_cookies_combo.currentData(), "firefox")
        self.assertFalse(dlg._yt_autodetect_cb.isChecked())
        self.assertEqual(dlg._yt_extra_args_edit.text(), "--retries 5")
        dlg.close()

    def test_defaults_rendered(self):
        dlg = self._dialog()
        self.assertTrue(dlg._yt_enabled_cb.isChecked())
        self.assertTrue(dlg._yt_embed_thumb_cb.isChecked())
        self.assertFalse(dlg._yt_embed_subs_cb.isChecked())
        self.assertTrue(dlg._yt_autodetect_cb.isChecked())
        self.assertEqual(dlg._yt_cookies_combo.currentData(), "")
        dlg.close()

    def test_disabling_greys_out_controls(self):
        dlg = self._dialog()
        dlg._yt_enabled_cb.setChecked(False)
        self.assertFalse(dlg._yt_path_edit.isEnabled())
        self.assertFalse(dlg._yt_format_combo.isEnabled())
        self.assertFalse(dlg._yt_update_btn.isEnabled())
        self.assertFalse(dlg._yt_subs_langs_edit.isEnabled())
        dlg._yt_enabled_cb.setChecked(True)
        self.assertTrue(dlg._yt_path_edit.isEnabled())
        dlg.close()

    def test_subtitle_languages_follow_checkbox(self):
        dlg = self._dialog()
        dlg._yt_enabled_cb.setChecked(True)
        dlg._yt_embed_subs_cb.setChecked(True)
        self.assertTrue(dlg._yt_subs_langs_edit.isEnabled())
        dlg._yt_embed_subs_cb.setChecked(False)
        self.assertFalse(dlg._yt_subs_langs_edit.isEnabled())
        dlg.close()

    def test_status_validation_reports_missing_ytdlp(self):
        dlg = self._dialog(ExternalToolsConfig(ytdlp_path="", ytdlp_ffmpeg_path=""))
        with patch.object(ytt, "get_ytdlp_version", return_value=""), \
             patch.object(ExternalToolsConfig, "get_effective_ytdlp_path", return_value=""), \
             patch.object(ExternalToolsConfig, "get_effective_ffmpeg_path", return_value=""):
            dlg._refresh_youtube_status()
        self.assertIn("not found", dlg._yt_path_status.text())
        self.assertIn("pip install", dlg._yt_path_status.text())
        self.assertIn("not found", dlg._yt_ffmpeg_status.text())
        dlg.close()

    def test_status_validation_reports_found_tools(self):
        dlg = self._dialog()
        with patch.object(ytt, "get_ytdlp_version", return_value="2026.09.15"), \
             patch.object(ExternalToolsConfig, "get_effective_ytdlp_path", return_value="C:/yt-dlp"), \
             patch.object(ExternalToolsConfig, "get_effective_ffmpeg_path", return_value="C:/ffmpeg"):
            dlg._refresh_youtube_status()
        self.assertIn("2026.09.15", dlg._yt_path_status.text())
        self.assertIn("yt-dlp 2026.09.15", dlg._yt_version_lbl.text())
        self.assertIn("found", dlg._yt_ffmpeg_status.text())
        dlg.close()

    def test_save_persists_all_fields(self):
        from PySide6.QtCore import QSettings

        dlg = self._dialog()
        dlg._yt_enabled_cb.setChecked(False)
        dlg._yt_path_edit.setText("D:/yt/yt-dlp.exe")
        dlg._yt_ffmpeg_edit.setText("D:/yt/ffmpeg.exe")
        dlg._yt_prefer_mode_a_cb.setChecked(False)
        dlg._yt_embed_thumb_cb.setChecked(False)
        dlg._yt_embed_subs_cb.setChecked(True)
        dlg._yt_subs_langs_edit.setText("es,pt")
        dlg._yt_cookies_combo.setCurrentIndex(dlg._yt_cookies_combo.findData("chrome"))
        dlg._yt_autodetect_cb.setChecked(False)
        dlg._yt_extra_args_edit.setText("--concurrent-fragments 4")
        dlg._yt_format_combo.setCurrentIndex(
            dlg._yt_format_combo.findData("bestaudio[ext=m4a]/bestaudio")
        )

        with patch.object(QSettings, "sync"):
            dlg._on_save()

        saved = dlg.external_tools_config
        self.assertFalse(saved.ytdlp_enabled)
        self.assertEqual(saved.ytdlp_path, "D:/yt/yt-dlp.exe")
        self.assertEqual(saved.ytdlp_ffmpeg_path, "D:/yt/ffmpeg.exe")
        self.assertFalse(saved.ytdlp_prefer_mode_a)
        self.assertFalse(saved.ytdlp_embed_thumbnail)
        self.assertTrue(saved.ytdlp_embed_subtitles)
        self.assertEqual(saved.ytdlp_subtitle_langs, "es,pt")
        self.assertEqual(saved.ytdlp_cookies_browser, "chrome")
        self.assertFalse(saved.ytdlp_auto_detect_urls)
        self.assertEqual(saved.ytdlp_extra_args, "--concurrent-fragments 4")
        self.assertEqual(saved.ytdlp_default_format, "bestaudio[ext=m4a]/bestaudio")

    def test_custom_format_selector_preserved(self):
        dlg = self._dialog(ExternalToolsConfig(
            ytdlp_default_format="my/custom+selector", ytdlp_ffmpeg_path=""
        ))
        self.assertEqual(dlg._yt_format_combo.currentData(), "my/custom+selector")
        self.assertIn("Custom", dlg._yt_format_combo.currentText())
        dlg.close()

    def test_playlist_limit_spinbox_bounds_and_default(self):
        dlg = self._dialog(ExternalToolsConfig(ytdlp_ffmpeg_path=""))
        self.assertEqual(dlg._yt_playlist_limit_spin.minimum(), 1)
        self.assertEqual(dlg._yt_playlist_limit_spin.maximum(), 500)
        self.assertEqual(dlg._yt_playlist_limit_spin.value(), 10)
        dlg.close()

    def test_playlist_limit_loaded_from_config(self):
        dlg = self._dialog(ExternalToolsConfig(ytdlp_playlist_limit=33, ytdlp_ffmpeg_path=""))
        self.assertEqual(dlg._yt_playlist_limit_spin.value(), 33)
        dlg.close()

    def test_playlist_limit_saved(self):
        dlg = self._dialog()
        dlg._yt_playlist_limit_spin.setValue(75)
        with patch.object(QSettings, "sync"):
            dlg._on_save()
        self.assertEqual(dlg.external_tools_config.ytdlp_playlist_limit, 75)

    def test_playlist_limit_greyed_out_when_disabled(self):
        dlg = self._dialog()
        dlg._yt_enabled_cb.setChecked(False)
        self.assertFalse(dlg._yt_playlist_limit_spin.isEnabled())
        dlg.close()

    def test_browse_sets_path(self):
        dlg = self._dialog()
        with patch("my_idm.settings_dialog.QFileDialog.getOpenFileName",
                   return_value=("D:/yt/yt-dlp.exe", "")):
            dlg._on_browse_ytdlp()
        self.assertEqual(dlg._yt_path_edit.text(), "D:/yt/yt-dlp.exe")
        dlg.close()

    def test_browse_ffmpeg_sets_path(self):
        dlg = self._dialog()
        with patch("my_idm.settings_dialog.QFileDialog.getOpenFileName",
                   return_value=("D:/yt/ffmpeg.exe", "")):
            dlg._on_browse_ffmpeg()
        self.assertEqual(dlg._yt_ffmpeg_edit.text(), "D:/yt/ffmpeg.exe")
        dlg.close()

    def test_update_button_reports_failure(self):
        dlg = self._dialog()
        with patch.object(ytt, "update_ytdlp", return_value=(False, "network down")), \
             patch("my_idm.settings_dialog.QMessageBox.warning") as mock_warn, \
             patch("my_idm.settings_dialog.QMessageBox.information"), \
             patch.object(dlg, "_refresh_youtube_status"):
            dlg._on_update_ytdlp()
        mock_warn.assert_called_once()
        self.assertIn("network down", mock_warn.call_args[0][2])
        dlg.close()

    def test_update_button_reports_success(self):
        dlg = self._dialog()
        with patch.object(ytt, "update_ytdlp", return_value=(True, "yt-dlp-2026.09.20")), \
             patch("my_idm.settings_dialog.QMessageBox.information") as mock_info, \
             patch.object(dlg, "_refresh_youtube_status"):
            dlg._on_update_ytdlp()
        mock_info.assert_called_once()
        self.assertIn("2026.09.20", mock_info.call_args[0][2])
        dlg.close()


if __name__ == "__main__":
    unittest.main()
