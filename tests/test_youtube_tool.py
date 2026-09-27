"""Unit tests for the yt-dlp integration layer (youtube_tool.py).

All yt-dlp interaction is mocked so the suite runs offline.
"""

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication

from my_idm.config import ExternalToolsConfig
from my_idm.utils import sanitize_filename
from my_idm import youtube_tool as yt

app = QApplication.instance() or QApplication(sys.argv)

MOCK_VIDEO_INFO = {
    "id": "dQw4w9WgXcQ",
    "title": "Rick Astley - Never Gonna Give You Up",
    "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg",
    "duration": 213,
    "uploader": "Rick Astley",
    "upload_date": "20091025",
    "description": "The classic video.",
    "extractor_key": "Youtube",
    "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "formats": [
        {
            "format_id": "22",
            "ext": "mp4",
            "resolution": "1280x720",
            "fps": 30,
            "vcodec": "avc1.64001F",
            "acodec": "mp4a.40.2",
            "filesize": 23_000_000,
            "url": "https://cdn.example.com/video.mp4",
        },
        {
            "format_id": "137",
            "ext": "mp4",
            "resolution": "1920x1080",
            "fps": 30,
            "vcodec": "avc1.640028",
            "acodec": "none",
            "filesize": 48_000_000,
            "url": "https://cdn.example.com/video_only.mp4",
        },
        {
            "format_id": "140",
            "ext": "m4a",
            "resolution": "audio only",
            "fps": None,
            "vcodec": "none",
            "acodec": "mp4a.40.2",
            "filesize": 3_500_000,
            "url": "https://cdn.example.com/audio.m4a",
        },
    ],
}


def _mock_ydl_module(info):
    """Build a fake yt_dlp module whose YoutubeDL returns *info*."""
    module = MagicMock()
    ydl_instance = MagicMock()
    ydl_instance.__enter__.return_value = ydl_instance
    ydl_instance.extract_info.return_value = info
    module.YoutubeDL.return_value = ydl_instance
    return module, ydl_instance


class TestURLDetection(unittest.TestCase):
    """detect_youtube_url() regex behaviour."""

    def test_detects_canonical_video_forms(self):
        for url in (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ&t=30s",
            "https://www.youtube.com/watch?feature=shared&v=dQw4w9WgXcQ",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/abc123XYZ",
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "https://www.youtube.com/live/dQw4w9WgXcQ",
            "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
            "https://www.youtube.com/v/dQw4w9WgXcQ",
        ):
            self.assertEqual(yt.detect_youtube_url(url), url, url)

    def test_detects_playlist_forms(self):
        for url in (
            "https://www.youtube.com/playlist?list=PL1234567890",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1234567890",
        ):
            self.assertEqual(yt.detect_youtube_url(url), url, url)

    def test_extracts_url_from_surrounding_text(self):
        text = "check this out https://youtu.be/dQw4w9WgXcQ later"
        self.assertEqual(yt.detect_youtube_url(text), "https://youtu.be/dQw4w9WgXcQ")

    def test_rejects_non_youtube_urls(self):
        for url in (
            "https://example.com/video.mp4",
            "https://notyoutube.com/watch?v=abcdef",
            "https://www.youtube.com/",
            "https://www.youtube.com/watch?v=abc",
            "https://youtube.com.evil.com/watch?v=dQw4w9WgXcQ",
            "ftp://youtu.be/dQw4w9WgXcQ",
            "https://vimeo.com/123456789",
            "",
            "just some text",
        ):
            self.assertIsNone(yt.detect_youtube_url(url), url)

    def test_handles_non_string_input(self):
        self.assertIsNone(yt.detect_youtube_url(None))
        self.assertIsNone(yt.detect_youtube_url(12345))

    def test_is_playlist_url(self):
        self.assertTrue(yt.is_playlist_url("https://www.youtube.com/playlist?list=PL123"))
        self.assertTrue(yt.is_playlist_url("https://www.youtube.com/watch?v=x&list=PL123"))
        self.assertFalse(yt.is_playlist_url("https://youtu.be/dQw4w9WgXcQ"))
        self.assertFalse(yt.is_playlist_url(""))


class TestAvailabilityChecks(unittest.TestCase):
    """Tool availability and version detection."""

    def test_ytdlp_available_when_module_importable(self):
        with patch.object(yt, "_import_ytdlp", return_value=MagicMock()):
            self.assertTrue(yt.check_ytdlp_available())

    def test_ytdlp_unavailable_when_import_fails(self):
        with patch.object(yt, "_import_ytdlp", side_effect=yt.YouTubeToolError("nope")):
            self.assertFalse(yt.check_ytdlp_available())

    def test_ytdlp_available_via_configured_binary(self):
        with patch.object(yt, "_import_ytdlp", side_effect=yt.YouTubeToolError("nope")):
            cfg = ExternalToolsConfig(ytdlp_path="C:/does/not/exist")
            with patch.object(
                ExternalToolsConfig, "get_effective_ytdlp_path", return_value="C:/bin/yt-dlp"
            ):
                self.assertTrue(yt.check_ytdlp_available(cfg))

    def test_ffmpeg_detection(self):
        with patch.object(yt.shutil, "which", return_value="C:/bin/ffmpeg"):
            self.assertTrue(yt.check_ffmpeg_available())
        with patch.object(yt.shutil, "which", return_value=None):
            self.assertFalse(yt.check_ffmpeg_available())
        cfg = ExternalToolsConfig()
        with patch.object(
            ExternalToolsConfig, "get_effective_ffmpeg_path", return_value="C:/bin/ffmpeg"
        ):
            self.assertTrue(yt.check_ffmpeg_available(cfg))

    def test_get_ytdlp_version_from_module(self):
        module = MagicMock()
        module.version = "2026.09.15"
        with patch.object(yt, "_import_ytdlp", return_value=module):
            self.assertEqual(yt.get_ytdlp_version(), "2026.09.15")

    def test_get_ytdlp_version_falls_back_to_binary(self):
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = "2026.09.20\n"
        with patch.object(yt, "_import_ytdlp", side_effect=yt.YouTubeToolError("nope")), \
             patch.object(yt.shutil, "which", return_value="C:/bin/yt-dlp"), \
             patch.object(yt.subprocess, "run", return_value=completed):
            self.assertEqual(yt.get_ytdlp_version(), "2026.09.20")

    def test_get_ytdlp_version_empty_when_unavailable(self):
        with patch.object(yt, "_import_ytdlp", side_effect=yt.YouTubeToolError("nope")), \
             patch.object(yt.shutil, "which", return_value=None):
            self.assertEqual(yt.get_ytdlp_version(), "")

    def test_get_ytdlp_version_handles_binary_failure(self):
        completed = MagicMock()
        completed.returncode = 1
        completed.stdout = ""
        with patch.object(yt, "_import_ytdlp", side_effect=yt.YouTubeToolError("nope")), \
             patch.object(yt.shutil, "which", return_value="C:/bin/yt-dlp"), \
             patch.object(yt.subprocess, "run", return_value=completed):
            self.assertEqual(yt.get_ytdlp_version(), "")


class TestExtractMetadata(unittest.TestCase):
    """extract_metadata() parsing of the yt-dlp info dict."""

    def setUp(self):
        yt.clear_analysis_cache()

    def _extract(self, info, config=None):
        module, instance = _mock_ydl_module(info)
        with patch.object(yt, "_import_ytdlp", return_value=module):
            return yt.extract_metadata("https://www.youtube.com/watch?v=dQw4w9WgXcQ", config), instance

    def test_parses_core_metadata(self):
        metadata, instance = self._extract(MOCK_VIDEO_INFO)
        self.assertEqual(metadata.id, "dQw4w9WgXcQ")
        self.assertEqual(metadata.title, "Rick Astley - Never Gonna Give You Up")
        self.assertEqual(metadata.uploader, "Rick Astley")
        self.assertEqual(metadata.thumbnail, "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg")
        self.assertEqual(metadata.duration, 213)
        self.assertEqual(metadata.extractor, "Youtube")
        self.assertEqual(
            metadata.webpage_url, "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )
        instance.extract_info.assert_called_once_with(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ", download=False
        )

    def test_parses_formats_with_classification(self):
        metadata, _ = self._extract(MOCK_VIDEO_INFO)
        self.assertEqual(len(metadata.formats), 3)

        muxed = metadata.find_format("22")
        self.assertTrue(muxed.is_muxed)
        self.assertFalse(muxed.requires_merge)
        self.assertTrue(muxed.direct_capable)

        video_only = metadata.find_format("137")
        self.assertTrue(video_only.is_video_only)
        self.assertTrue(video_only.requires_merge)
        self.assertFalse(video_only.direct_capable)

        audio_only = metadata.find_format("140")
        self.assertTrue(audio_only.is_audio_only)
        self.assertTrue(audio_only.direct_capable)

    def test_format_display_helpers(self):
        metadata, _ = self._extract(MOCK_VIDEO_INFO)
        self.assertEqual(metadata.find_format("22").display_resolution, "720p")
        self.assertEqual(metadata.find_format("137").display_resolution, "1080p")
        self.assertEqual(metadata.find_format("140").display_resolution, "audio only")
        self.assertEqual(metadata.find_format("22").display_fps, "30")
        self.assertEqual(metadata.find_format("140").display_fps, "-")
        self.assertIn("MB", metadata.find_format("22").quality_label)
        self.assertEqual(metadata.find_format("22").quality_label, "21.9 MB")
        self.assertEqual(metadata.find_format("140").quality_label, "3.3 MB")

    def test_metadata_label_helpers(self):
        metadata, _ = self._extract(MOCK_VIDEO_INFO)
        self.assertEqual(metadata.duration_label, "3:33")
        self.assertEqual(metadata.upload_date_label, "2009-10-25")

    def test_long_duration_label(self):
        metadata, _ = self._extract({**MOCK_VIDEO_INFO, "duration": 3725})
        self.assertEqual(metadata.duration_label, "1:02:05")

    def test_direct_formats_excludes_video_only(self):
        metadata, _ = self._extract(MOCK_VIDEO_INFO)
        ids = [f.format_id for f in metadata.direct_formats]
        self.assertIn("22", ids)
        self.assertIn("140", ids)
        self.assertNotIn("137", ids)

    def test_storyboard_and_mhtml_formats_filtered(self):
        info = {
            **MOCK_VIDEO_INFO,
            "formats": [
                *MOCK_VIDEO_INFO["formats"],
                {
                    "format_id": "sb0",
                    "ext": "mhtml",
                    "format_note": "storyboard",
                    "vcodec": "none",
                    "acodec": "none",
                    "url": "https://cdn.example.com/sb.mhtml",
                },
            ],
        }
        metadata, _ = self._extract(info)
        self.assertEqual(len(metadata.formats), 3)
        self.assertIsNone(metadata.find_format("sb0"))

    def test_formats_without_ids_are_skipped(self):
        info = {**MOCK_VIDEO_INFO, "formats": [{"ext": "mp4", "url": "https://x/1.mp4"}]}
        metadata, _ = self._extract(info)
        self.assertEqual(len(metadata.formats), 0)

    def test_malformed_format_entries_are_skipped(self):
        info = {**MOCK_VIDEO_INFO, "formats": ["not-a-dict", None, *MOCK_VIDEO_INFO["formats"]]}
        metadata, _ = self._extract(info)
        self.assertEqual(len(metadata.formats), 3)

    def test_approximate_filesize_used_when_exact_missing(self):
        info = {
            **MOCK_VIDEO_INFO,
            "formats": [
                {
                    "format_id": "18",
                    "ext": "mp4",
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                    "filesize_approx": 1_000_000,
                    "url": "https://cdn.example.com/18.mp4",
                }
            ],
        }
        metadata, _ = self._extract(info)
        self.assertEqual(metadata.find_format("18").filesize, 1_000_000)

    def test_live_and_age_flags(self):
        metadata, _ = self._extract({**MOCK_VIDEO_INFO, "is_live": True, "age_restrict": 1})
        self.assertTrue(metadata.is_live)
        self.assertTrue(metadata.age_restricted)

    def test_playlist_flag_and_entries(self):
        info = {**MOCK_VIDEO_INFO, "entries": [MOCK_VIDEO_INFO, MOCK_VIDEO_INFO]}
        metadata, _ = self._extract(info)
        self.assertTrue(metadata.is_playlist)
        self.assertFalse(metadata.is_batch)

    def test_empty_url_raises(self):
        with self.assertRaises(yt.YouTubeToolError) as ctx:
            yt.extract_metadata("")
        self.assertEqual(ctx.exception.kind, "invalid_url")

    def test_none_result_raises(self):
        module, instance = _mock_ydl_module(None)
        with patch.object(yt, "_import_ytdlp", return_value=module):
            with self.assertRaises(yt.YouTubeToolError):
                yt.extract_metadata("https://youtu.be/x")

    def test_missing_ytdlp_raises_not_installed(self):
        with patch.object(yt, "_import_ytdlp", side_effect=yt.YouTubeToolError("nope", kind="not_installed")):
            with self.assertRaises(yt.YouTubeToolError) as ctx:
                yt.extract_metadata("https://youtu.be/x")
        self.assertEqual(ctx.exception.kind, "not_installed")


class TestErrorClassification(unittest.TestCase):
    """yt-dlp exceptions are translated into classified errors."""

    def setUp(self):
        yt.clear_analysis_cache()

    def _raise_with(self, message):
        module, instance = _mock_ydl_module(None)
        instance.extract_info.side_effect = RuntimeError(message)
        with patch.object(yt, "_import_ytdlp", return_value=module):
            with self.assertRaises(yt.YouTubeToolError) as ctx:
                yt.extract_metadata("https://youtu.be/x")
        return ctx.exception

    def test_private_video(self):
        self.assertEqual(self._raise_with("Private video. Sign in if you've been granted access").kind, "auth")

    def test_age_restricted(self):
        self.assertEqual(self._raise_with("Sign in to confirm your age").kind, "age_restricted")
        self.assertEqual(self._raise_with("This video is age-restricted").kind, "age_restricted")

    def test_age_gate_takes_priority_over_auth(self):
        self.assertEqual(
            self._raise_with("ERROR: Sign in to confirm your age. This video may be inappropriate").kind,
            "age_restricted",
        )

    def test_members_only(self):
        self.assertEqual(
            self._raise_with("This video is available to this channel's members").kind, "auth"
        )

    def test_geo_restricted(self):
        self.assertEqual(
            self._raise_with("The uploader has not made this video available in your country").kind,
            "geo_restricted",
        )

    def test_unsupported_site(self):
        self.assertEqual(self._raise_with("Unsupported URL: https://example.com/x").kind, "unsupported")

    def test_rate_limited(self):
        self.assertEqual(self._raise_with("HTTP Error 429: Too Many Requests").kind, "rate_limited")

    def test_generic_error(self):
        self.assertEqual(self._raise_with("something odd happened").kind, "error")


class TestResolveDirectUrl(unittest.TestCase):
    """Mode A direct URL resolution."""

    def setUp(self):
        yt.clear_analysis_cache()

    def _resolve(self, info, format_id="22", config=None, title=""):
        module, instance = _mock_ydl_module(info)
        with patch.object(yt, "_import_ytdlp", return_value=module):
            return (
                yt.resolve_direct_url(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ", format_id, config, title
                ),
                instance,
            )

    def test_resolves_muxed_format(self):
        result, _ = self._resolve(MOCK_VIDEO_INFO, "22")
        self.assertEqual(result.direct_url, "https://cdn.example.com/video.mp4")
        self.assertEqual(result.format_id, "22")
        self.assertEqual(result.filesize, 23_000_000)
        self.assertEqual(result.content_type, "video/mp4")
        self.assertEqual(result.filename, "Rick Astley - Never Gonna Give You Up.mp4")
        self.assertEqual(
            result.webpage_url, "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

    def test_resolves_audio_only_format(self):
        result, _ = self._resolve(MOCK_VIDEO_INFO, "140")
        self.assertEqual(result.direct_url, "https://cdn.example.com/audio.m4a")
        self.assertEqual(result.content_type, "audio/m4a")
        self.assertTrue(result.filename.endswith(".m4a"))

    def test_video_only_format_rejected(self):
        with self.assertRaises(yt.YouTubeToolError) as ctx:
            self._resolve(MOCK_VIDEO_INFO, "137")
        self.assertEqual(ctx.exception.kind, "merge_required")

    def test_unknown_format_rejected(self):
        with self.assertRaises(yt.YouTubeToolError) as ctx:
            self._resolve(MOCK_VIDEO_INFO, "9999")
        self.assertEqual(ctx.exception.kind, "invalid_format")

    def test_format_without_url_rejected(self):
        info = {
            **MOCK_VIDEO_INFO,
            "formats": [
                {
                    "format_id": "sb1",
                    "ext": "mp4",
                    "vcodec": "avc1",
                    "acodec": "none",
                    "url": "",
                }
            ],
        }
        with self.assertRaises(yt.YouTubeToolError) as ctx:
            self._resolve(info, "sb1")
        self.assertEqual(ctx.exception.kind, "merge_required")

    def test_custom_title_used(self):
        result, _ = self._resolve(MOCK_VIDEO_INFO, "22", title="My Custom Name")
        self.assertEqual(result.filename, "My Custom Name.mp4")

    def test_expiry_parsed_from_url(self):
        info = {
            **MOCK_VIDEO_INFO,
            "formats": [
                {
                    "format_id": "22",
                    "ext": "mp4",
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                    "url": "https://cdn.example.com/v.mp4?expire=1735689600",
                }
            ],
        }
        result, _ = self._resolve(info, "22")
        self.assertEqual(result.expires_at, 1735689600.0)

    def test_no_expiry_when_absent(self):
        result, _ = self._resolve(MOCK_VIDEO_INFO, "22")
        self.assertIsNone(result.expires_at)

    def test_empty_arguments_rejected(self):
        with self.assertRaises(yt.YouTubeToolError) as ctx:
            yt.resolve_direct_url("", "22")
        self.assertEqual(ctx.exception.kind, "invalid_url")

        with self.assertRaises(yt.YouTubeToolError) as ctx:
            yt.resolve_direct_url("https://youtu.be/x", "")
        self.assertEqual(ctx.exception.kind, "invalid_format")


class TestExtractPlaylist(unittest.TestCase):
    """Playlist extraction returns one metadata object per entry."""

    def setUp(self):
        yt.clear_analysis_cache()

    def test_returns_entries(self):
        info = {
            "id": "PL123",
            "title": "My Playlist",
            "entries": [MOCK_VIDEO_INFO, MOCK_VIDEO_INFO, None],
        }
        module, _ = _mock_ydl_module(info)
        with patch.object(yt, "_import_ytdlp", return_value=module):
            videos = yt.extract_playlist("https://www.youtube.com/playlist?list=PL123")
        self.assertEqual(len(videos), 2)
        self.assertEqual(videos[0].id, "dQw4w9WgXcQ")

    def test_empty_url_raises(self):
        with self.assertRaises(yt.YouTubeToolError):
            yt.extract_playlist("")


class TestFilenameHelpers(unittest.TestCase):
    """Filename generation and sanitisation."""

    def test_build_filename(self):
        self.assertEqual(yt.build_filename("My Video", "mp4"), "My Video.mp4")

    def test_build_filename_removes_illegal_chars(self):
        self.assertEqual(yt.build_filename('a/b:c*d?e"f<g>h|i', "mp4"), "a_b_c_d_e_f_g_h_i.mp4")

    def test_build_filename_does_not_double_extension(self):
        self.assertEqual(yt.build_filename("clip.mp4", "mp4"), "clip.mp4")

    def test_build_filename_without_ext(self):
        self.assertEqual(yt.build_filename("plain", ""), "plain")

    def test_build_stem_neutralises_path_separator(self):
        stem = yt.build_stem("Passacaglia - G.F. Handel/ Arr. by J. Halvorsen")
        self.assertNotIn("/", stem)
        self.assertNotIn("\\", stem)

    def test_build_stem_keeps_dotted_title_intact(self):
        """A dotted tail that is not a real extension must not truncate the name."""
        title = "Passacaglia - G.F. Handel_ Arr. by J. Halvorsen [PIANO COVER]"
        self.assertEqual(yt.build_stem(title), title)

    def test_build_stem_strips_real_extension(self):
        self.assertEqual(yt.build_stem("clip.mp4"), "clip")

    def test_build_stem_of_empty_title(self):
        self.assertEqual(yt.build_stem(""), "video")

    def test_split_extension_conservative(self):
        from my_idm.utils import split_extension
        self.assertEqual(split_extension("a.mp4"), ("a", "mp4"))
        self.assertEqual(split_extension("noext"), ("noext", ""))
        self.assertEqual(
            split_extension("Arr. by J. Halvorsen [PIANO COVER]"),
            ("Arr. by J. Halvorsen [PIANO COVER]", ""),
        )
        self.assertEqual(split_extension(".hidden"), (".hidden", ""))

    def test_sanitize_filename_truncates_and_keeps_extension(self):
        result = sanitize_filename("x" * 400 + ".mp4")
        self.assertTrue(result.endswith(".mp4"))
        self.assertLessEqual(len(result), 255)

    def test_sanitize_filename_reserved_names(self):
        self.assertEqual(sanitize_filename("CON"), "_CON")
        self.assertEqual(sanitize_filename("nul.txt"), "_nul.txt")

    def test_sanitize_filename_empty_fallback(self):
        self.assertEqual(sanitize_filename(""), "download")
        self.assertEqual(sanitize_filename("   "), "download")
        self.assertEqual(sanitize_filename("..."), "download")


class TestResolveProducedFile(unittest.TestCase):
    """Locating the file yt-dlp actually wrote."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_exact_match(self):
        target = self.dir / "My Video.mp4"
        target.write_bytes(b"x" * 10)
        self.assertEqual(yt.resolve_produced_file(str(self.dir), "My Video"), str(target))

    def test_finds_alternative_container(self):
        target = self.dir / "My Video.webm"
        target.write_bytes(b"x" * 10)
        self.assertEqual(yt.resolve_produced_file(str(self.dir), "My Video"), str(target))

    def test_ignores_part_and_temp_files(self):
        (self.dir / "My Video.mp4.part").write_bytes(b"x")
        (self.dir / "My Video.mp4.ytdl").write_bytes(b"x")
        self.assertEqual(yt.resolve_produced_file(str(self.dir), "My Video"), "")

    def test_ignores_merge_fragments(self):
        (self.dir / "My Video.f602.mp4").write_bytes(b"x" * 99)
        self.assertEqual(yt.resolve_produced_file(str(self.dir), "My Video"), "")

    def test_prefers_real_file_over_fragment(self):
        (self.dir / "My Video.f602.mp4").write_bytes(b"x" * 99)
        real = self.dir / "My Video.mp4"
        real.write_bytes(b"x" * 10)
        self.assertEqual(yt.resolve_produced_file(str(self.dir), "My Video"), str(real))

    def test_handles_glob_metacharacters_in_stem(self):
        target = self.dir / "a [b] (c).mp4"
        target.write_bytes(b"x" * 5)
        self.assertEqual(yt.resolve_produced_file(str(self.dir), "a [b] (c)"), str(target))

    def test_missing_directory(self):
        self.assertEqual(yt.resolve_produced_file(str(self.dir / "nope"), "x"), "")

    def test_empty_stem(self):
        self.assertEqual(yt.resolve_produced_file(str(self.dir), ""), "")


class TestDownloadOptions(unittest.TestCase):
    """Mode B yt-dlp option assembly."""

    def test_basic_options(self):
        cfg = ExternalToolsConfig(ytdlp_embed_thumbnail=False, ytdlp_ffmpeg_path="")
        options = yt.get_download_options(cfg, "best", "/tmp/out")
        self.assertEqual(options["format"], "best")
        self.assertIn("outtmpl", options)
        self.assertNotIn("postprocessors", options)
        self.assertTrue(options["quiet"])

    def test_thumbnail_postprocessor(self):
        cfg = ExternalToolsConfig(ytdlp_embed_thumbnail=True, ytdlp_ffmpeg_path="")
        options = yt.get_download_options(cfg, "best", "/tmp/out")
        keys = [p["key"] for p in options["postprocessors"]]
        self.assertIn("EmbedThumbnail", keys)

    def test_subtitle_postprocessor_and_langs(self):
        cfg = ExternalToolsConfig(
            ytdlp_embed_thumbnail=False,
            ytdlp_embed_subtitles=True,
            ytdlp_subtitle_langs="en,fr",
            ytdlp_ffmpeg_path="",
        )
        options = yt.get_download_options(cfg, "best", "/tmp/out")
        self.assertTrue(options["writesubtitles"])
        self.assertEqual(options["subtitleslangs"], ["en", "fr"])
        keys = [p["key"] for p in options["postprocessors"]]
        self.assertIn("FFmpegEmbedSubtitle", keys)

    def test_ffmpeg_location_and_cookies_forwarded(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ffmpeg = Path(tmp) / "ffmpeg.exe"
            ffmpeg.write_text("#!/bin/sh\n", encoding="utf-8")
            cfg = ExternalToolsConfig(
                ytdlp_ffmpeg_path=str(ffmpeg), ytdlp_cookies_browser="firefox"
            )
            options = yt.get_download_options(cfg, "best", tmp)
            self.assertEqual(
                str(Path(options["ffmpeg_location"])).lower(), str(ffmpeg).lower()
            )
        self.assertEqual(options["cookiesfrombrowser"], ("firefox",))

    def test_invalid_cookie_browser_ignored(self):
        cfg = ExternalToolsConfig(ytdlp_cookies_browser="notabrowser")
        options = yt.get_download_options(cfg, "best", "/tmp/out")
        self.assertNotIn("cookiesfrombrowser", options)

    def test_extract_opts_use_configured_format(self):
        cfg = ExternalToolsConfig(ytdlp_default_format="bestvideo[height<=720]+bestaudio")
        options = yt._build_ydl_opts(cfg, mode="extract")
        self.assertEqual(options["format"], "bestvideo[height<=720]+bestaudio")
        self.assertFalse(options["extract_flat"])


class TestNativeDownload(unittest.TestCase):
    """Mode B native download thread wiring."""

    def test_start_native_download_runs_and_completes(self):
        module = MagicMock()
        ydl_instance = MagicMock()
        ydl_instance.__enter__.return_value = ydl_instance
        ydl_instance.extract_info.return_value = {"filepath": "/out/video.mp4"}
        ydl_instance.sanitize_info.return_value = {"filepath": "/out/video.mp4"}
        module.YoutubeDL.return_value = ydl_instance

        with tempfile.TemporaryDirectory() as out:
            # The worker reports the file yt-dlp actually produced, found by stem.
            produced = Path(out) / "video.mp4"
            produced.write_bytes(b"x" * 32)
            outtmpl = str(Path(out) / "video.%(ext)s")

            cfg = ExternalToolsConfig(ytdlp_ffmpeg_path="")
            done = threading.Event()
            result = {}

            with patch.object(yt, "_import_ytdlp", return_value=module):
                thread, cancel = yt.start_native_download(
                    "https://youtu.be/x",
                    out,
                    "best",
                    cfg,
                    done_cb=lambda p: (result.__setitem__("path", p), done.set()),
                    error_cb=lambda m: (result.__setitem__("error", m), done.set()),
                    outtmpl=outtmpl,
                )
                self.assertTrue(done.wait(timeout=10))
                thread.join(timeout=5)

        self.assertIsNone(result.get("error"))
        self.assertEqual(result.get("path"), str(produced))
        ydl_instance.download.assert_called_once_with(["https://youtu.be/x"])

        opts = module.YoutubeDL.call_args[0][0]
        self.assertTrue(any(h.__name__ == "_progress_hook" for h in opts["progress_hooks"]))
        self.assertEqual(opts["outtmpl"], outtmpl)

    def test_start_native_download_reports_empty_path_when_nothing_written(self):
        module = MagicMock()
        ydl_instance = MagicMock()
        ydl_instance.__enter__.return_value = ydl_instance
        module.YoutubeDL.return_value = ydl_instance

        with tempfile.TemporaryDirectory() as out:
            done = threading.Event()
            result = {}
            with patch.object(yt, "_import_ytdlp", return_value=module):
                thread, _ = yt.start_native_download(
                    "https://youtu.be/x", out, "best", ExternalToolsConfig(ytdlp_ffmpeg_path=""),
                    done_cb=lambda p: (result.__setitem__("path", p), done.set()),
                    error_cb=lambda m: (result.__setitem__("error", m), done.set()),
                    outtmpl=str(Path(out) / "%(title)s.%(ext)s"),
                )
                self.assertTrue(done.wait(timeout=10))
                thread.join(timeout=5)

        self.assertIsNone(result.get("error"))
        self.assertEqual(result.get("path"), "")

    def test_start_native_download_reports_error(self):
        module = MagicMock()
        ydl_instance = MagicMock()
        ydl_instance.__enter__.return_value = ydl_instance
        ydl_instance.download.side_effect = RuntimeError("Unsupported URL: https://x")
        module.YoutubeDL.return_value = ydl_instance

        captured = {}
        done = threading.Event()

        with patch.object(yt, "_import_ytdlp", return_value=module):
            thread, _ = yt.start_native_download(
                "https://x", "/out", "best", ExternalToolsConfig(ytdlp_ffmpeg_path=""),
                done_cb=lambda p: done.set(),
                error_cb=lambda m: (captured.__setitem__("error", m), done.set()),
            )
            self.assertTrue(done.wait(timeout=10))
            thread.join(timeout=5)

        self.assertIn("Unsupported URL", captured.get("error", ""))

    def test_cancellation_aborts_download(self):
        module = MagicMock()
        ydl_instance = MagicMock()
        ydl_instance.__enter__.return_value = ydl_instance

        def _download(_urls):
            ydl_instance.add_progress_hook.call_args[0][0]({"status": "downloading"})
            return 0

        ydl_instance.download.side_effect = _download
        module.YoutubeDL.return_value = ydl_instance

        captured = {}
        done = threading.Event()

        with patch.object(yt, "_import_ytdlp", return_value=module):
            thread, cancel = yt.start_native_download(
                "https://x", "/out", "best", ExternalToolsConfig(ytdlp_ffmpeg_path=""),
                done_cb=lambda p: done.set(),
                error_cb=lambda m: (captured.__setitem__("error", m), done.set()),
            )
            cancel["cancel"] = True
            self.assertTrue(done.wait(timeout=10))
            thread.join(timeout=5)

        self.assertIn("cancelled", captured.get("error", "").lower())

    def test_missing_ytdlp_raises_before_thread_start(self):
        with patch.object(yt, "_import_ytdlp", side_effect=yt.YouTubeToolError("nope", kind="not_installed")):
            with self.assertRaises(yt.YouTubeToolError):
                yt.start_native_download(
                    "https://x", "/out", "best", ExternalToolsConfig(ytdlp_ffmpeg_path="")
                )


class TestUpdateYtDlp(unittest.TestCase):
    """Self-update helper."""

    def test_update_via_pip(self):
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = "Successfully installed yt-dlp-2026.09.20"
        with patch.object(yt, "_import_ytdlp", return_value=MagicMock()), \
             patch.object(yt.subprocess, "run", return_value=completed) as mock_run:
            ok, msg = yt.update_ytdlp()
        self.assertTrue(ok)
        self.assertIn("yt-dlp-2026.09.20", msg)
        self.assertIn("pip", " ".join(mock_run.call_args[0][0]))

    def test_update_failure_reports_message(self):
        completed = MagicMock()
        completed.returncode = 1
        completed.stdout = ""
        completed.stderr = "permission denied"
        with patch.object(yt, "_import_ytdlp", return_value=MagicMock()), \
             patch.object(yt.subprocess, "run", return_value=completed):
            ok, msg = yt.update_ytdlp()
        self.assertFalse(ok)
        self.assertIn("permission denied", msg)

    def test_update_uses_binary_when_module_missing(self):
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = "Latest version: 2026.09.20"
        cfg = ExternalToolsConfig()
        with patch.object(yt, "_import_ytdlp", side_effect=yt.YouTubeToolError("nope")), \
             patch.object(
                 ExternalToolsConfig, "get_effective_ytdlp_path", return_value="C:/bin/yt-dlp"
             ), \
             patch.object(yt.subprocess, "run", return_value=completed) as mock_run:
            ok, msg = yt.update_ytdlp(cfg)
        self.assertTrue(ok)
        self.assertIn("2026.09.20", msg)
        self.assertIn("-U", mock_run.call_args[0][0])

    def test_update_handles_oserror(self):
        with patch.object(yt, "_import_ytdlp", return_value=MagicMock()), \
             patch.object(yt.subprocess, "run", side_effect=OSError("boom")):
            ok, msg = yt.update_ytdlp()
        self.assertFalse(ok)
        self.assertIn("boom", msg)


if __name__ == "__main__":
    unittest.main()
