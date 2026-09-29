"""Unit tests for YouTube download orchestration (manager, dialog, paste detection)."""

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from my_idm.config import ExternalToolsConfig
from my_idm.database import Database
from my_idm.manager import (
    DownloadManager,
    _can_use_mode_a,
    _expected_total_size,
    _pick_format,
    _resolve_format_id,
)
from my_idm import youtube_tool as ytt
from my_idm.youtube_dialog import QUALITY_PRESETS, YouTubeDialog
from my_idm.dialogs import AddDownloadDialog

app = QApplication.instance() or QApplication(sys.argv)

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def make_format(format_id, *, has_video=True, has_audio=True, filesize=1_000_000,
                ext="mp4", fps=30, resolution="1920x1080", url=None):
    is_video_only = has_video and not has_audio
    is_audio_only = has_audio and not has_video
    return ytt.YouTubeFormat(
        format_id=format_id,
        ext=ext,
        resolution=resolution if has_video else "audio only",
        fps=fps if has_video else None,
        vcodec="avc1.640028" if has_video else "none",
        acodec="mp4a.40.2" if has_audio else "none",
        filesize=filesize,
        url=url if url is not None else f"https://cdn.example.com/{format_id}",
        has_video=has_video,
        has_audio=has_audio,
        is_video_only=is_video_only,
        is_audio_only=is_audio_only,
        is_muxed=has_video and has_audio,
        requires_merge=is_video_only or is_audio_only,
        direct_capable=bool(url if url is not None else f"https://cdn.example.com/{format_id}") and not is_video_only,
    )


def make_metadata(formats=None):
    if formats is None:
        formats = [
            make_format("137", has_audio=False),
            make_format("140", has_video=False, ext="m4a", resolution="audio only"),
            make_format("18", resolution="640x360"),
        ]
    return ytt.YouTubeMetadata(
        id="dQw4w9WgXcQ",
        title="Test Video",
        uploader="Test Uploader",
        duration=213,
        upload_date="20091025",
        webpage_url=URL,
        formats=formats,
    )


class TestFormatSelectionHelpers(unittest.TestCase):
    """Mode A eligibility and expected-size helpers."""

    def setUp(self):
        self.md = make_metadata()

    def test_muxed_format_is_mode_a_capable(self):
        self.assertTrue(_can_use_mode_a(self.md, "18"))
        self.assertEqual(_resolve_format_id(self.md, "18"), "18")

    def test_audio_only_is_mode_a_capable(self):
        self.assertTrue(_can_use_mode_a(self.md, "140"))
        self.assertEqual(_resolve_format_id(self.md, "140"), "140")

    def test_video_only_requires_mode_b(self):
        self.assertFalse(_can_use_mode_a(self.md, "137"))
        self.assertEqual(_resolve_format_id(self.md, "137"), "")

    def test_merged_selector_requires_mode_b(self):
        self.assertFalse(_can_use_mode_a(self.md, "137+140"))

    def test_unknown_selector_not_mode_a(self):
        self.assertFalse(_can_use_mode_a(self.md, "9999"))

    def test_pick_format_by_id(self):
        self.assertEqual(_pick_format(self.md, "137").format_id, "137")

    def test_pick_format_from_expression(self):
        self.assertEqual(_pick_format(self.md, "137+140").format_id, "137")
        self.assertEqual(_pick_format(self.md, "137/18").format_id, "137")

    def test_pick_format_ignores_symbolic_keywords(self):
        """Selectors like bestvideo+bestaudio name no concrete format id."""
        self.assertIsNone(_pick_format(self.md, "bestvideo+bestaudio/best"))
        self.assertIsNone(_pick_format(self.md, "bestvideo[height<=1080]+bestaudio"))

    def test_pick_format_none_metadata(self):
        self.assertIsNone(_pick_format(None, "137"))
        self.assertIsNone(_pick_format(ytt.YouTubeMetadata(), "137"))

    def test_expected_total_sums_merged_streams(self):
        self.assertEqual(_expected_total_size(self.md, "137+140"), 2_000_000)

    def test_expected_total_single_format(self):
        self.assertEqual(_expected_total_size(self.md, "140"), 1_000_000)

    def test_expected_total_unknown_returns_zero(self):
        self.assertEqual(_expected_total_size(self.md, "9999"), 0)
        self.assertEqual(_expected_total_size(self.md, ""), 0)
        self.assertEqual(_expected_total_size(None, "137"), 0)

    def test_format_without_http_headers(self):
        self.assertEqual(make_format("22").http_headers, {})


class TestManagerYouTubeIntegration(unittest.TestCase):
    """DownloadManager.add_youtube_download and the Mode B job lifecycle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Registered first so it runs LAST (cleanups are LIFO). The fake yt-dlp
        # worker registered by _start_fake_ytdlp() must be stopped before this
        # directory is removed, or Windows refuses to unlink its still-open
        # .part file.
        self.addCleanup(self.tmp.cleanup)
        self.out = str(Path(self.tmp.name) / "yt")
        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)
        self.manager = DownloadManager(self.db)
        self.addCleanup(self.manager.stop)
        self.manager._external_tools_config = ExternalToolsConfig(ytdlp_enabled=True)
        # Disable the post-download AV scan so completion assertions are deterministic.
        self.manager._security_config.scan_after_download = False
        self.md = make_metadata()

    # -- Mode A ------------------------------------------------------------

    def test_mode_a_creates_http_entry_from_direct_url(self):
        direct = ytt.DirectUrlResult(
            direct_url="https://cdn.example.com/18",
            filename="Test Video.mp4",
            filesize=1_000_000,
            format_id="18",
            webpage_url=URL,
            http_headers={"User-Agent": "test-agent"},
        )
        with patch("my_idm.youtube_tool.resolve_direct_url", return_value=direct):
            did = self.manager.add_youtube_download(
                URL, self.md, save_path=self.out, format_selector="18", mode="a"
            )
        self.assertIsNotNone(did)
        entry = self.db.get_download(did)
        self.assertEqual(entry.url, "https://cdn.example.com/18")
        self.assertEqual(entry.filename, "Test Video.mp4")
        self.assertEqual(entry.metadata.get("source_type"), "youtube")
        self.assertEqual(entry.metadata.get("youtube_mode"), "a")
        self.assertEqual(entry.metadata.get("video_id"), "dQw4w9WgXcQ")
        self.assertEqual(entry.metadata.get("uploader"), "Test Uploader")
        self.assertEqual(entry.metadata.get("original_youtube_url"), URL)
        self.assertEqual(entry.metadata.get("headers"), {"User-Agent": "test-agent"})

    def test_mode_a_rejects_video_only_format(self):
        with patch("my_idm.youtube_tool.resolve_direct_url") as mock_resolve:
            did = self.manager.add_youtube_download(
                URL, self.md, save_path=self.out, format_selector="137", mode="a"
            )
        self.assertIsNone(did)
        mock_resolve.assert_not_called()

    def test_mode_a_reports_resolve_failure(self):
        errors = []
        self.manager.youtube_error.connect(lambda u, m: errors.append((u, m)))
        with patch(
            "my_idm.youtube_tool.resolve_direct_url",
            side_effect=ytt.YouTubeToolError("expired", kind="error"),
        ):
            did = self.manager.add_youtube_download(
                URL, self.md, save_path=self.out, format_selector="18", mode="a"
            )
        self.assertIsNone(did)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], URL)

    def test_disabled_config_blocks_download(self):
        self.manager._external_tools_config = ExternalToolsConfig(ytdlp_enabled=False)
        did = self.manager.add_youtube_download(URL, self.md, save_path=self.out, format_selector="18", mode="b")
        self.assertIsNone(did)

    def test_empty_url_rejected(self):
        self.assertIsNone(self.manager.add_youtube_download("", self.md, save_path=self.out))

    # -- Mode B ------------------------------------------------------------

    def _start_mode_b(self, selector="137+140", filename=""):
        with patch("my_idm.youtube_tool.start_native_download") as mock_start:
            mock_start.return_value = (MagicMock(), {"cancel": False})
            did = self.manager.add_youtube_download(
                URL, self.md, save_path=self.out, format_selector=selector,
                mode="b", filename=filename,
            )
        return did, mock_start

    def test_mode_b_creates_native_entry(self):
        did, mock_start = self._start_mode_b()
        self.assertIsNotNone(did)
        mock_start.assert_called_once()
        entry = self.db.get_download(did)
        self.assertEqual(entry.url, URL)
        self.assertEqual(entry.metadata.get("source_type"), "youtube_native")
        self.assertEqual(entry.metadata.get("youtube_format"), "137+140")
        self.assertEqual(entry.metadata.get("youtube_expected_size"), 2_000_000)
        self.assertEqual(entry.total_size, 2_000_000)
        self.assertEqual(entry.status, "downloading")

    def test_mode_b_derives_filename_from_title(self):
        did, _ = self._start_mode_b()
        entry = self.db.get_download(did)
        self.assertTrue(entry.filename.endswith(".mp4"))
        self.assertIn("Test Video", entry.filename)

    def test_mode_b_uses_default_format_when_absent(self):
        did, mock_start = self._start_mode_b(selector="")
        self.assertIsNotNone(did)
        kwargs = mock_start.call_args.kwargs
        self.assertEqual(kwargs["format_selector"], self.manager.external_tools_config.ytdlp_default_format)

    def test_mode_b_registers_job(self):
        did, _ = self._start_mode_b()
        self.assertTrue(self.manager.is_ytdlp_native_job(did))
        self.assertTrue(self.manager.is_youtube_download(did))
        self.assertTrue(self.manager._is_ytdlp_native_entry(self.db.get_download(did)))

    def test_mode_b_missing_ytdlp_reports_error(self):
        self.manager._external_tools_config = ExternalToolsConfig(ytdlp_ffmpeg_path="")
        errors = []
        self.manager.youtube_error.connect(lambda u, m: errors.append(m))
        with patch("my_idm.youtube_tool.check_ytdlp_available", return_value=False):
            did = self.manager.add_youtube_download(
                URL, self.md, save_path=self.out, format_selector="137+140", mode="b"
            )
        self.assertIsNone(did)
        self.assertEqual(len(errors), 1)

    def test_mode_b_duplicate_start_is_ignored(self):
        did, _ = self._start_mode_b()
        entry = self.db.get_download(did)
        with patch("my_idm.youtube_tool.start_native_download") as mock_start:
            self.manager._start_ytdlp_native_job(entry, "137+140", self.out)
            mock_start.assert_not_called()

    def test_pause_cancels_native_job(self):
        did, _ = self._start_mode_b()
        self.manager.pause_download(did)
        self.assertEqual(self.db.get_download(did).status, "paused")
        with self.manager._ytdlp_lock:
            holder = self.manager._ytdlp_jobs[did]["cancel_holder"]
        self.assertTrue(holder["cancel"])

    def test_stop_cancels_native_job(self):
        did, _ = self._start_mode_b()
        self.manager.stop_download(did)
        self.assertEqual(self.db.get_download(did).status, "stopped")
        with self.manager._ytdlp_lock:
            self.assertTrue(self.manager._ytdlp_jobs[did]["cancel_holder"]["cancel"])

    def test_delete_drops_native_job(self):
        did, _ = self._start_mode_b()
        self.manager.delete_download(did)
        self.assertIsNone(self.db.get_download(did))
        self.assertFalse(self.manager.is_ytdlp_native_job(did))

    def test_progress_relay_updates_entry(self):
        did, _ = self._start_mode_b()
        events = []
        self.manager.progress_updated.connect(lambda *a: events.append(a))
        self.manager._on_ytdlp_progress(did, {
            "status": "downloading",
            "downloaded_bytes": 500_000,
            "total_bytes": 0,
        })
        entry = self.db.get_download(did)
        self.assertEqual(entry.downloaded_size, 500_000)
        self.assertEqual(entry.total_size, 2_000_000)
        self.assertTrue(events)

    def test_progress_ignored_when_paused(self):
        did, _ = self._start_mode_b()
        self.manager.pause_download(did)
        events = []
        self.manager.progress_updated.connect(lambda *a: events.append(a))
        self.manager._on_ytdlp_progress(did, {"status": "downloading", "downloaded_bytes": 999})
        self.assertEqual(events, [])

    def test_done_marks_completed(self):
        did, _ = self._start_mode_b()
        produced = Path(self.out) / "merged.mp4"
        produced.write_bytes(b"x" * 2048)
        self.manager._on_ytdlp_done(did, str(produced))
        entry = self.db.get_download(did)
        self.assertEqual(entry.status, "completed")
        self.assertEqual(entry.filename, "merged.mp4")
        self.assertEqual(entry.total_size, 2048)
        self.assertEqual(entry.downloaded_size, 2048)
        self.assertFalse(self.manager.is_ytdlp_native_job(did))

    def test_done_respects_paused_state(self):
        did, _ = self._start_mode_b()
        self.manager.pause_download(did)
        produced = Path(self.out) / "merged.mp4"
        produced.write_bytes(b"x" * 10)
        self.manager._on_ytdlp_done(did, str(produced))
        self.assertEqual(self.db.get_download(did).status, "paused")

    def test_error_marks_error(self):
        did, _ = self._start_mode_b()
        self.manager._on_ytdlp_error(did, "Unsupported URL")
        entry = self.db.get_download(did)
        self.assertEqual(entry.status, "error")
        self.assertEqual(entry.error_message, "Unsupported URL")
        self.assertFalse(self.manager.is_ytdlp_native_job(did))

    def test_cancellation_error_maps_to_paused(self):
        did, _ = self._start_mode_b()
        self.manager._on_ytdlp_error(did, "Download cancelled by user.")
        self.assertEqual(self.db.get_download(did).status, "paused")

    def test_stop_cancels_all_jobs(self):
        did, _ = self._start_mode_b()
        with self.manager._ytdlp_lock:
            holder = self.manager._ytdlp_jobs[did]["cancel_holder"]
        self.manager.stop()
        self.assertTrue(holder["cancel"])

    # -- URL refresh --------------------------------------------------------

    def test_refresh_youtube_url_replaces_expired_url(self):
        did, _ = self._start_mode_b()
        entry = self.db.get_download(did)
        entry.metadata["source_type"] = "youtube"
        entry.metadata["youtube_mode"] = "a"
        entry.metadata["youtube_format_id"] = "18"
        entry.metadata["original_youtube_url"] = URL
        entry.url = "https://cdn.example.com/expired"
        self.db.update_download(entry)

        fresh = ytt.DirectUrlResult(
            direct_url="https://cdn.example.com/fresh",
            filename="Test Video.mp4",
            format_id="18",
            http_headers={"User-Agent": "new"},
        )
        with patch("my_idm.youtube_tool.resolve_direct_url", return_value=fresh):
            refreshed = self.manager._refresh_youtube_url(self.db.get_download(did))
        self.assertTrue(refreshed)
        updated = self.db.get_download(did)
        self.assertEqual(updated.url, "https://cdn.example.com/fresh")
        self.assertEqual(updated.metadata.get("headers"), {"User-Agent": "new"})

    def test_refresh_skipped_without_source(self):
        did, _ = self._start_mode_b()
        entry = self.db.get_download(did)
        entry.metadata["original_youtube_url"] = ""
        self.db.update_download(entry)
        self.assertFalse(self.manager._maybe_refresh_youtube_url(self.db.get_download(did)))

    def test_maybe_refresh_skips_fresh_expiry(self):
        import time as _time
        did, _ = self._start_mode_b()
        entry = self.db.get_download(did)
        entry.metadata["source_type"] = "youtube"
        entry.metadata["youtube_mode"] = "a"
        entry.metadata["youtube_format_id"] = "18"
        entry.metadata["original_youtube_url"] = URL
        entry.metadata["youtube_url_expires_at"] = _time.time() + 3600
        self.db.update_download(entry)
        with patch("my_idm.manager.DownloadManager._refresh_youtube_url") as mock_refresh:
            self.assertFalse(self.manager._maybe_refresh_youtube_url(self.db.get_download(did)))
            mock_refresh.assert_not_called()

    def test_maybe_refresh_triggers_near_expiry(self):
        import time as _time
        did, _ = self._start_mode_b()
        entry = self.db.get_download(did)
        entry.metadata["source_type"] = "youtube"
        entry.metadata["youtube_mode"] = "a"
        entry.metadata["youtube_format_id"] = "18"
        entry.metadata["original_youtube_url"] = URL
        entry.metadata["youtube_url_expires_at"] = _time.time() + 30
        self.db.update_download(entry)
        with patch("my_idm.manager.DownloadManager._refresh_youtube_url", return_value=True) as mock_refresh:
            self.assertTrue(self.manager._maybe_refresh_youtube_url(self.db.get_download(did)))
            mock_refresh.assert_called_once()

    def test_resume_does_not_refresh_mode_b(self):
        import time as _time
        did, _ = self._start_mode_b()
        entry = self.db.get_download(did)
        entry.metadata["youtube_url_expires_at"] = _time.time() + 10
        self.db.update_download(entry)
        with patch("my_idm.manager.DownloadManager._refresh_youtube_url") as mock_refresh:
            self.manager.resume_download(did)
            mock_refresh.assert_not_called()

    # -- output-name control (regression: "file not found" bug) ------------

    SLASH_TITLE = "Passacaglia - G.F. Handel/ Arr. by J. Halvorsen [PIANO COVER]"

    def test_mode_b_filename_has_no_path_separator(self):
        md = make_metadata()
        md.title = self.SLASH_TITLE
        with patch("my_idm.youtube_tool.start_native_download") as mock_start:
            mock_start.return_value = (MagicMock(), {"cancel": False})
            did = self.manager.add_youtube_download(
                URL, md, save_path=self.out, format_selector="137+140", mode="b"
            )
        entry = self.db.get_download(did)
        self.assertNotIn("/", entry.filename)
        self.assertNotIn("\\", entry.filename)
        self.assertTrue(entry.filename.endswith(".mp4"))
        self.assertEqual(Path(entry.file_path).parent, Path(self.out).resolve() if Path(self.out).exists() else Path(self.out))

    def test_mode_b_records_stem_in_metadata(self):
        md = make_metadata()
        md.title = self.SLASH_TITLE
        with patch("my_idm.youtube_tool.start_native_download") as mock_start:
            mock_start.return_value = (MagicMock(), {"cancel": False})
            did = self.manager.add_youtube_download(
                URL, md, save_path=self.out, format_selector="137+140", mode="b"
            )
        entry = self.db.get_download(did)
        stem = entry.metadata.get("youtube_stem")
        self.assertTrue(stem)
        self.assertNotIn("/", stem)
        self.assertIn("Halvorsen", stem)

    def test_mode_b_passes_explicit_outtmpl(self):
        md = make_metadata()
        md.title = self.SLASH_TITLE
        with patch("my_idm.youtube_tool.start_native_download") as mock_start:
            mock_start.return_value = (MagicMock(), {"cancel": False})
            self.manager.add_youtube_download(
                URL, md, save_path=self.out, format_selector="137+140", mode="b"
            )
        kwargs = mock_start.call_args.kwargs
        outtmpl = kwargs["outtmpl"]
        self.assertIn("%(ext)s", outtmpl)
        self.assertNotIn("/Arr", outtmpl.replace("\\", "/"))
        self.assertEqual(Path(outtmpl).parent, Path(self.out))

    def test_mode_b_done_locates_file_by_recorded_stem(self):
        """With an explicit outtmpl, completion resolves the file we asked for."""
        md = make_metadata()
        md.title = self.SLASH_TITLE
        with patch("my_idm.youtube_tool.start_native_download") as mock_start:
            mock_start.return_value = (MagicMock(), {"cancel": False})
            did = self.manager.add_youtube_download(
                URL, md, save_path=self.out, format_selector="137+140", mode="b"
            )

        entry = self.db.get_download(did)
        real = Path(self.out) / entry.filename
        real.write_bytes(b"z" * 5000)

        # done_cb receives no path hint (worker could not resolve it in time).
        self.manager._on_ytdlp_done(did, "")
        entry = self.db.get_download(did)
        self.assertEqual(entry.status, "completed")
        self.assertEqual(Path(entry.file_path), real)
        self.assertEqual(entry.total_size, 5000)

    def test_mode_b_done_ignores_merge_fragments(self):
        md = make_metadata()
        md.title = "Plain Title"
        with patch("my_idm.youtube_tool.start_native_download") as mock_start:
            mock_start.return_value = (MagicMock(), {"cancel": False})
            did = self.manager.add_youtube_download(
                URL, md, save_path=self.out, format_selector="137+140", mode="b"
            )
        frag = Path(self.out) / "Plain Title.f602.mp4"
        frag.write_bytes(b"q" * 10)
        self.manager._on_ytdlp_done(did, "")
        entry = self.db.get_download(did)
        self.assertNotIn(".f602.", entry.file_path or "")

    # -- self-heal for already-broken entries -------------------------------

    def test_relocate_recovers_mismatched_filename(self):
        md = make_metadata()
        md.title = self.SLASH_TITLE
        with patch("my_idm.youtube_tool.start_native_download") as mock_start:
            mock_start.return_value = (MagicMock(), {"cancel": False})
            did = self.manager.add_youtube_download(
                URL, md, save_path=self.out, format_selector="137+140", mode="b"
            )

        entry = self.db.get_download(did)
        entry.filename = "Passacaglia - G.F. Handel/ Arr. by J. Halvorsen [PIANO COVER].mp4"
        entry.file_path = str(Path(self.out) / "Passacaglia - G.F. Handel" / " Arr. by J. Halvorsen [PIANO COVER].mp4")
        self.db.update_download(entry)

        real = Path(self.out) / "Passacaglia - G.F. Handel⧸ Arr. by J. Halvorsen [PIANO COVER].mp4"
        real.write_bytes(b"y" * 3000)

        self.manager.mark_file_not_found(did)
        fixed = self.db.get_download(did)
        self.assertEqual(fixed.status, "completed")
        self.assertEqual(Path(fixed.file_path), real)
        self.assertEqual(fixed.total_size, 3000)

    def test_relocate_ignores_non_youtube_entries(self):
        self.manager.add_download("https://example.com/a.zip", save_path=self.out)
        entry = self.db.get_all_downloads()[0]
        self.manager.mark_file_not_found(entry.id)
        self.assertEqual(self.db.get_download(entry.id).status, "file_not_found")

    def test_relocate_ignores_when_file_present(self):
        md = make_metadata()
        with patch("my_idm.youtube_tool.start_native_download") as mock_start:
            mock_start.return_value = (MagicMock(), {"cancel": False})
            did = self.manager.add_youtube_download(
                URL, md, save_path=self.out, format_selector="137+140", mode="b"
            )
        target = Path(self.out) / "present.mp4"
        target.write_bytes(b"a" * 5)
        entry = self.db.get_download(did)
        entry.filename = "present.mp4"
        entry.file_path = str(target)
        self.db.update_download(entry)
        self.manager.mark_file_not_found(did)
        self.assertEqual(self.db.get_download(did).status, "file_not_found")

    def test_mark_file_not_found_without_entry(self):
        self.manager.mark_file_not_found("does-not-exist")

    # -- deleting while running (regression: orphaned .part, locked file) ----

    STEM = "Mariage d'Amour - Paul de Senneville __ Jacob's Piano"

    def _start_fake_ytdlp(self):
        """Fake worker that holds a .f616.mp4.part file open until cancelled."""
        import threading
        import time

        state = {
            "holder": {"cancel": False},
            "exited": threading.Event(),
            "stop": threading.Event(),
            "handle": None,
            "thread": None,
        }
        part_path = {}

        def fake_start(url, save_dir, format_selector, config, outtmpl=None, **kwargs):
            part = Path(save_dir) / f"{self.STEM}.f616.mp4.part"
            part_path["path"] = part
            state["handle"] = open(part, "wb")
            written = {"n": 0}

            def run():
                try:
                    while not state["holder"].get("cancel") and not state["stop"].is_set():
                        state["handle"].write(b"x" * 8192)
                        written["n"] += 8192
                        cb = kwargs.get("progress_cb")
                        if cb:
                            try:
                                cb({"status": "downloading", "downloaded_bytes": written["n"]})
                            except Exception:
                                # The test may have torn the manager down already.
                                break
                        time.sleep(0.02)
                finally:
                    state["handle"].close()
                    state["exited"].set()

            thread = threading.Thread(target=run, daemon=True)
            state["thread"] = thread
            thread.start()
            return thread, state["holder"]

        patcher = patch("my_idm.youtube_tool.start_native_download", side_effect=fake_start)
        patcher.start()
        self.addCleanup(patcher.stop)

        def _shutdown():
            state["holder"]["cancel"] = True
            state["stop"].set()
            thread = state["thread"]
            if thread is not None:
                thread.join(timeout=5)

        self.addCleanup(_shutdown)
        return state, part_path

    def test_delete_during_download_removes_part_file(self):
        state, part_path = self._start_fake_ytdlp()
        md = make_metadata()
        md.title = self.STEM
        did = self.manager.add_youtube_download(
            URL, md, save_path=self.out, format_selector="137+140", mode="b"
        )
        deadline = time.time() + 5
        while time.time() < deadline and not part_path["path"].exists():
            time.sleep(0.02)
        self.assertTrue(part_path["path"].is_file(), "fake .part was never created")

        self.manager.delete_download(did, delete_files=True)
        state["exited"].wait(timeout=15)

        self.assertTrue(state["exited"].is_set(), "worker should have exited")
        self.assertFalse(part_path["path"].exists(), ".part file must be removed")
        self.assertEqual(list(Path(self.out).iterdir()), [])
        self.assertIsNone(self.db.get_download(did))

    def test_delete_during_download_releases_lock(self):
        state, _ = self._start_fake_ytdlp()
        md = make_metadata()
        md.title = self.STEM
        did = self.manager.add_youtube_download(
            URL, md, save_path=self.out, format_selector="137+140", mode="b"
        )
        time.sleep(0.3)
        self.assertFalse(state["handle"].closed)

        self.manager.delete_download(did, delete_files=True)
        state["exited"].wait(timeout=15)
        self.assertTrue(state["handle"].closed, "file handle must be released")

    def test_purge_removes_all_scratch_variants(self):
        md = make_metadata()
        md.title = self.STEM
        with patch("my_idm.youtube_tool.start_native_download") as mock_start:
            mock_start.return_value = (MagicMock(), {"cancel": False})
            did = self.manager.add_youtube_download(
                URL, md, save_path=self.out, format_selector="137+140", mode="b"
            )
        entry = self.db.get_download(did)
        stem = self.manager.external_tools_config and entry.metadata.get("youtube_stem")

        kept = Path(self.out) / f"{stem}.mp4"
        kept.write_bytes(b"final")
        for suffix in (".f616.mp4.part", ".f140.m4a.part", ".mp4.ytdl", ".mp4.temp"):
            Path(self.out) / f"{stem}{suffix}".replace(".mp4.f616.mp4.part", ".f616.mp4.part")
            (Path(self.out) / f"{stem}{suffix}").write_bytes(b"scratch")

        removed = self.manager._purge_youtube_temp_files(entry)
        self.assertEqual(len(removed), 4)
        self.assertTrue(kept.is_file(), "the final media file must survive")
        self.assertEqual(
            sorted(p.name for p in Path(self.out).iterdir()),
            [f"{stem}.mp4"],
        )

    def test_progress_hook_is_noop_after_entry_deleted(self):
        """Regression: a late hook raised 'NoneType' has no 'total_size'."""
        md = make_metadata()
        with patch("my_idm.youtube_tool.start_native_download") as mock_start:
            mock_start.return_value = (MagicMock(), {"cancel": False})
            did = self.manager.add_youtube_download(
                URL, md, save_path=self.out, format_selector="137+140", mode="b"
            )
        self.db.delete_download(did)
        # Must not raise.
        self.manager._on_ytdlp_progress(did, {"status": "downloading", "downloaded_bytes": 10})
        self.manager._on_ytdlp_done(did, "")
        self.manager._on_ytdlp_error(did, "boom")

    def test_stop_worker_times_out_without_hanging(self):
        """A worker that ignores cancellation must not block delete indefinitely."""
        import threading
        import time

        never = threading.Event()
        with patch("my_idm.youtube_tool.start_native_download") as mock_start:
            mock_start.return_value = (threading.Thread(target=never.wait, daemon=True), {"cancel": False})
            did = self.manager.add_youtube_download(
                URL, make_metadata(), save_path=self.out,
                format_selector="137+140", mode="b",
            )
        t0 = time.time()
        self.manager.delete_download(did, delete_files=True)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 8.0, "delete must not block on an unresponsive worker")
        self.assertIsNone(self.db.get_download(did))
        never.set()


async def _no_sleep(_delay):
    return None


class TestSegmentStatusReporting(unittest.TestCase):
    """Regression: segments showed 'Pending' while bytes were arriving.

    ``SegmentEntry.status`` was only ever set to 'completed' or 'error', so the
    details panel's Segments tab read 'Pending' for the whole download even
    though the byte counters and progress bars moved. The panel reads the live
    in-memory ``SegmentEntry``, so database-only updates were not enough either.
    """

    def setUp(self):
        from my_idm.database import Database, DownloadEntry, SegmentEntry
        from my_idm.http_engine import HTTPEngine

        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(":memory:")
        self.db.open()
        self.db.add_download(
            DownloadEntry(
                id="d1", url="https://example.com/big.bin", filename="big.bin",
                save_path=self.tmp.name, file_path=f"{self.tmp.name}/big.bin",
                total_size=3000, downloaded_size=0, status="downloading",
                num_segments=3,
            )
        )
        self.db.add_segments(
            [
                SegmentEntry(
                    id=f"d1-seg{i}", download_id="d1", index=i,
                    start_byte=i * 1000, end_byte=(i + 1) * 1000 - 1,
                    status="pending",
                )
                for i in range(3)
            ]
        )
        self.engine = HTTPEngine(self.db)
        self.engine._tor_config = None
        self.engine._network_config = None
        self.engine._session = MagicMock()
        self.updates = []
        real_update = self.db.update_segment

        def spy(segment_id, downloaded, status=None):
            self.updates.append((segment_id, status))
            return real_update(segment_id, downloaded, status)

        self.db.update_segment = spy

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _run(self, seg, cancel=None):
        import asyncio
        return asyncio.run(
            self.engine._download_one_segment(
                self.db.get_download("d1"), seg, {seg.id: seg}, 0.0, 0,
                cancel or asyncio.Event(),
            )
        )

    def test_active_segment_is_marked_downloading_first(self):
        """The first status write for a transferring segment must be 'downloading'."""
        from my_idm import http_engine as he

        seg = self.db.get_segments("d1")[0]
        with patch.object(he.HTTPEngine, "_download_segment_curl",
                          side_effect=OSError("boom")):
            with patch("asyncio.sleep", new=_no_sleep):
                with self.assertRaises(Exception):
                    self._run(seg)
        self.assertEqual(self.updates[0], ("d1-seg0", "downloading"))

    def test_cancelled_segment_reports_paused_not_pending(self):
        import asyncio
        seg = self.db.get_segments("d1")[0]
        seg.status = "downloading"
        cancel = asyncio.Event()
        cancel.set()
        self._run(seg, cancel)
        self.assertEqual(seg.status, "paused")
        self.assertIn(("d1-seg0", "paused"), self.updates)
        self.assertNotIn(("d1-seg0", "pending"), self.updates)

    def test_finished_segment_completes_without_downloading(self):
        seg = self.db.get_segments("d1")[0]
        seg.downloaded_bytes = seg.end_byte - seg.start_byte + 1
        self._run(seg)
        self.assertEqual(seg.status, "completed")
        self.assertIn(("d1-seg0", "completed"), self.updates)
        self.assertNotIn(("d1-seg0", "downloading"), self.updates)

    def test_in_memory_status_tracks_database(self):
        """The panel reads the live object, so it must never go stale."""
        from my_idm import http_engine as he

        seg = self.db.get_segments("d1")[0]
        with patch.object(he.HTTPEngine, "_download_segment_curl",
                          side_effect=OSError("boom")):
            with patch("asyncio.sleep", new=_no_sleep):
                with self.assertRaises(Exception):
                    self._run(seg)
        stored = self.db.get_segments("d1")[0]
        self.assertEqual(seg.status, stored.status)


class TestPasteDetection(unittest.TestCase):
    """Add Download dialog auto-detects pasted YouTube URLs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _dialog(self, cfg=None):
        mgr = MagicMock()
        mgr.external_tools_config = cfg or ExternalToolsConfig(ytdlp_auto_detect_urls=True)
        dlg = AddDownloadDialog(manager=mgr)
        dlg._update_youtube_banner()
        return dlg

    def test_banner_shown_for_youtube_paste(self):
        dlg = self._dialog()
        dlg._url_edit.setPlainText(URL)
        dlg._update_youtube_banner()
        self.assertFalse(dlg._yt_banner.isHidden())
        self.assertEqual(dlg.youtube_url, URL)
        dlg.close()

    def test_banner_hidden_for_regular_url(self):
        dlg = self._dialog()
        dlg._url_edit.setPlainText("https://example.com/file.zip")
        dlg._update_youtube_banner()
        self.assertTrue(dlg._yt_banner.isHidden())
        self.assertEqual(dlg.youtube_url, "")
        dlg.close()

    def test_banner_hidden_for_empty_input(self):
        dlg = self._dialog()
        dlg._url_edit.setPlainText("")
        dlg._update_youtube_banner()
        self.assertTrue(dlg._yt_banner.isHidden())
        dlg.close()

    def test_detection_disabled_by_config(self):
        dlg = self._dialog(ExternalToolsConfig(ytdlp_auto_detect_urls=False))
        dlg._url_edit.setPlainText(URL)
        dlg._update_youtube_banner()
        self.assertTrue(dlg._yt_banner.isHidden())
        self.assertEqual(dlg.youtube_url, "")
        dlg.close()

    def test_youtube_selection_defaults_empty(self):
        dlg = self._dialog()
        self.assertEqual(dlg.youtube_selection, {})
        dlg.close()

    def test_text_change_triggers_banner_update(self):
        dlg = self._dialog()
        dlg._url_edit.setPlainText(URL)
        self.assertFalse(dlg._yt_banner.isHidden())
        dlg._url_edit.setPlainText("https://example.com/x.zip")
        self.assertTrue(dlg._yt_banner.isHidden())
        dlg.close()


class TestYouTubeDialog(unittest.TestCase):
    """YouTube dialog construction and format-table population."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _dialog(self):
        mgr = MagicMock()
        cfg = ExternalToolsConfig(ytdlp_ffmpeg_path="")
        mgr.external_tools_config = cfg
        mgr.general_config.get_effective_save_path.return_value = str(Path(self.tmp.name))
        return YouTubeDialog(manager=mgr)

    def test_dialog_constructs_with_presets(self):
        dlg = self._dialog()
        self.assertEqual(dlg._preset_combo.count(), len(QUALITY_PRESETS))
        self.assertFalse(dlg._download_btn.isEnabled())
        self.assertEqual(dlg._playlist_list.count(), 0)
        dlg.close()

    def test_table_populated_from_metadata(self):
        dlg = self._dialog()
        md = make_metadata()
        dlg._videos = [md]
        dlg._current = md
        dlg._populate_table(md)
        self.assertEqual(dlg._table.rowCount(), 3)
        self.assertEqual(len(dlg._format_ids), 3)
        self.assertIn("18", dlg._format_ids)
        self.assertTrue(dlg._download_btn.isEnabled())
        dlg.close()

    def test_table_marks_video_only_as_mode_b(self):
        dlg = self._dialog()
        md = make_metadata()
        dlg._current = md
        dlg._populate_table(md)
        row = dlg._format_ids.index("137")
        self.assertEqual(dlg._table.item(row, 0).text(), "B (merge)")
        audio_row = dlg._format_ids.index("140")
        self.assertEqual(dlg._table.item(audio_row, 0).text(), "A (audio)")
        dlg.close()

    def test_audio_preset_selects_audio_row(self):
        dlg = self._dialog()
        md = make_metadata()
        dlg._current = md
        dlg._populate_table(md)
        index = [label for label, _, _ in QUALITY_PRESETS].index("Audio only (M4A)")
        dlg._preset_combo.setCurrentIndex(index)
        self.assertEqual(dlg._table.currentRow(), dlg._format_ids.index("140"))
        dlg.close()

    def test_720p_preset_respects_height_cap(self):
        md = make_metadata(
            formats=[
                make_format("137", has_audio=False, resolution="1920x1080"),
                make_format("136", has_audio=False, resolution="1280x720"),
                make_format("135", has_audio=False, resolution="640x360"),
            ]
        )
        dlg = self._dialog()
        dlg._current = md
        dlg._populate_table(md)
        index = [label for label, _, _ in QUALITY_PRESETS].index("Best 720p")
        dlg._preset_combo.setCurrentIndex(index)
        self.assertEqual(dlg._format_id_at(dlg._table.currentRow()), "136")
        dlg.close()

    def test_playlist_entries_build_checked_list(self):
        dlg = self._dialog()
        vids = [make_metadata(), make_metadata(), make_metadata()]
        dlg._videos = vids
        dlg._current = vids[0]
        dlg._fill_info(vids[0])
        dlg._show_playlist_list()
        self.assertEqual(dlg._playlist_list.count(), 3)
        self.assertFalse(dlg._playlist_wrap.isHidden())
        self.assertEqual(len(dlg._selected_playlist_items()), 3)
        dlg._set_all_checked(False)
        self.assertEqual(len(dlg._selected_playlist_items()), 0)
        dlg._set_all_checked(True)
        self.assertEqual(len(dlg._selected_playlist_items()), 3)
        dlg.close()

    def test_info_labels_filled(self):
        dlg = self._dialog()
        md = make_metadata()
        dlg._fill_info(md)
        self.assertEqual(dlg._title_lbl.text(), "Test Video")
        self.assertEqual(dlg._uploader_lbl.text(), "Test Uploader")
        self.assertEqual(dlg._duration_lbl.text(), "3:33")
        self.assertEqual(dlg._date_lbl.text(), "2009-10-25")
        dlg.close()

    def test_selection_empty_before_accept(self):
        dlg = self._dialog()
        self.assertEqual(dlg.selection(), {})
        dlg.close()

    def test_selection_reports_targets(self):
        dlg = self._dialog()
        md = make_metadata()
        dlg._videos = [md]
        dlg._current = md
        dlg._populate_table(md)
        dlg._submitted = True
        result = dlg.selection()
        self.assertEqual(len(result["videos"]), 1)
        self.assertIn("format_selector", result)
        self.assertIn("save_path", result)
        dlg.close()

    def test_subtitle_langs_toggle_enables_field(self):
        dlg = self._dialog()
        dlg._embed_subs_cb.setChecked(True)
        self.assertTrue(dlg._subs_langs_edit.isEnabled())
        dlg._embed_subs_cb.setChecked(False)
        self.assertFalse(dlg._subs_langs_edit.isEnabled())
        dlg.close()

    def test_empty_formats_clears_selection(self):
        dlg = self._dialog()
        md = make_metadata(formats=[])
        dlg._current = md
        dlg._populate_table(md)
        self.assertEqual(dlg._table.rowCount(), 0)
        self.assertEqual(dlg._format_ids, [])
        dlg.close()


if __name__ == "__main__":
    unittest.main()
