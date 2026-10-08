"""Unit tests for clipboard capture and the global capture hotkey.

Both features are opt-in and both are "does the app notice something the user did without
asking it to" — the failure modes that matter are silence and over-reach, so the tests are
weighted towards the boundaries rather than the happy path.
"""

import ctypes
import ctypes.wintypes
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from PySide6.QtCore import QCoreApplication, QObject, QSettings, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

import my_idm.hotkey as hotkey_module
from my_idm.browser_server import BrowserServer
from my_idm.clipboard_monitor import (
    ABSOLUTE_MAX_URLS,
    REASON_HTML_PAGE,
    REASON_IGNORED_EXT,
    REASON_TOO_SMALL,
    REASON_UNREACHABLE,
    ClipboardMonitor,
    decide_capture,
    extract_download_urls,
    extract_url_extension,
    is_ignored_extension,
    looks_like_download_url,
    resolve_candidates,
)
from my_idm.config import BrowserIntegrationConfig, GeneralConfig
from my_idm.dialogs import AddDownloadDialog
from my_idm.hotkey import (
    ERROR_HOTKEY_ALREADY_REGISTERED,
    HOTKEY_ID,
    WM_HOTKEY,
    HotkeyRegistration,
    describe_modifiers,
    parse_hotkey,
)
from my_idm.http_probe import ProbeResult, filename_from_headers, html_is_a_file, is_probeable
from my_idm.manager import DownloadManager
from my_idm.database import Database, DownloadEntry
from tests.fake_http import run_async
from tests.test_main_window import _MainWindowTestCase

app = QApplication.instance() or QApplication(sys.argv)


_CONFIG_GROUPS = (
    "General",
    "Torrent",
    "Tor",
    "ExternalTools",
    "BrowserIntegration",
    "Network",
    "Security",
    "Scheduler",
)


class ConfigIsolationMixin:
    """Snapshot/restore the whole QSettings tree around every test."""

    def setUp(self):
        super().setUp()
        settings = QSettings("MyIDM", "My-IDM")
        snapshot = {
            group: {
                key: settings.value(f"{group}/{key}", None) for key in settings.childKeys()
            }
            for group in _CONFIG_GROUPS
        }
        self.addCleanup(self._restore_settings, settings, snapshot)

    @staticmethod
    def _restore_settings(settings, snapshot):
        settings.clear()
        for group, values in snapshot.items():
            for key, value in values.items():
                settings.setValue(f"{group}/{key}", value)
        settings.sync()


class _FakeClipboard(QObject):
    """In-memory ``QClipboard`` with a real ``dataChanged`` signal.

    A plain object will not do: the monitor subscribes to ``dataChanged``, so the fake has to
    be a ``QObject`` for the connect/disconnect to have the same semantics as production.
    ``QGuiApplication.clipboard`` is patched rather than ``QApplication.clipboard`` because
    that is the accessor production calls, and patching only the subclass would leave the
    ``QGuiApplication`` call site untouched.
    """

    dataChanged = Signal()

    def __init__(self):
        super().__init__()
        self._text = ""
        self._image = None
        self._mime = None
        self.set_calls: list[str] = []

    def text(self, mode=None):
        return self._text

    def setText(self, text, mode=None):
        self._text = text or ""
        self.set_calls.append(self._text)
        self.dataChanged.emit()

    def clear(self, mode=None):
        self._text = ""
        self._image = None
        self._mime = None
        self.dataChanged.emit()

    def image(self, mode=None):
        return self._image

    def setImage(self, image, mode=None):
        self._image = image

    def setPixmap(self, pixmap, mode=None):
        self._image = pixmap

    def pixmap(self, mode=None):
        return self._image

    def mimeData(self, formats=None):
        return self._mime

    def setMimeData(self, data, mode=None):
        self._mime = data

    def supportsSelection(self):
        return False

    def ownsClipboard(self):
        return False

    def ownsSelection(self):
        return False


def install_fake_clipboard(testcase):
    fake = _FakeClipboard()
    patcher = patch.object(
        QGuiApplication, "clipboard", staticmethod(lambda: fake)
    )
    patcher.start()
    testcase.addCleanup(patcher.stop)
    return fake


class _FakeManager:
    """Records what the monitor hands over, with scriptable outcomes."""

    def __init__(self, result="id", raises_on=()):
        self.calls: list[str] = []
        self.kwargs_calls: list[dict] = []
        self.result = result
        self.raises_on = set(raises_on)

    def add_download(self, url, *args, **kwargs):
        self.calls.append(url)
        self.kwargs_calls.append(kwargs)
        if url in self.raises_on:
            raise RuntimeError("engine exploded")
        if callable(self.result):
            return self.result(url)
        return self.result


def permissive_probe(size: int = 64 * 1024 * 1024, **overrides):
    """Build a probe that accepts everything, for tests about something other than filtering.

    ``ClipboardMonitor`` resolves a URL before capturing it, so a test that only cares about
    dedup or the per-copy limit would otherwise have to fake a network answer. The size is a
    real 64 MB rather than 0 on purpose: a 0-byte probe means *unknown* and short-circuits the
    size check, so tests would pass against a threshold they never exercised.
    """

    async def _probe(url: str) -> ProbeResult:
        result = {"ok": True, "size": size, "content_type": "application/octet-stream"}
        result.update(overrides)
        return ProbeResult(url=url, filename="", final_url=url, **result)

    _probe.calls = []
    return _probe


# ---------------------------------------------------------------------------
# looks_like_download_url
# ---------------------------------------------------------------------------

class TestLooksLikeDownloadUrl(unittest.TestCase):
    def test_accepts_transportable_schemes(self):
        for url in (
            "http://example.com/a.zip",
            "https://example.com/a.zip",
            "HTTPS://EXAMPLE.COM/A.ZIP",
            "ftp://example.com/a.zip",
            "magnet:?xt=urn:btih:0123456789abcdef",
        ):
            with self.subTest(url=url):
                assert looks_like_download_url(url), url

    def test_rejects_schemes_with_no_transport(self):
        # blob: is what Chrome reports for a JS-generated download and GitHub hands out for
        # some asset pages. There is nothing for an external downloader to fetch, so queuing
        # one only makes the user wait out the retry ladder.
        for url in ("blob:https://example.com/abc", "javascript:alert(1)",
                    "data:text/plain,hi", "about:blank", "chrome://settings"):
            with self.subTest(url=url):
                assert not looks_like_download_url(url), url

    def test_rejects_blank_and_oversized(self):
        assert not looks_like_download_url("")
        assert not looks_like_download_url("   ")
        assert not looks_like_download_url("https://e.com/" + "a" * 5000)
        assert not looks_like_download_url(None)

    def test_torrent_suffix_needs_a_real_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            torrent = Path(tmp) / "x.torrent"
            torrent.write_bytes(b"d4:infod4:name1:ae")
            assert looks_like_download_url(str(torrent))
        assert looks_like_download_url("file:///c:/downloads/x.torrent")
        # A bare string that merely ends in .torrent is not something add_download can fetch.
        assert not looks_like_download_url("some random text.torrent")

    def test_shared_with_the_add_download_dialog(self):
        # The pre-fill gate and the auto-capture gate must not drift: the same string has to
        # be offered in the dialog and captured by the monitor, or neither.
        for text in ("https://example.com/a.zip", "magnet:?xt=urn:btih:abc",
                     "blob:https://example.com/x", "", "hello world"):
            with self.subTest(text=text):
                assert AddDownloadDialog._is_valid_download_url(text) == \
                    looks_like_download_url(text)


# ---------------------------------------------------------------------------
# extract_download_urls
# ---------------------------------------------------------------------------

class TestExtractDownloadUrls(unittest.TestCase):
    def test_single_url(self):
        assert extract_download_urls("https://example.com/a.zip") == [
            "https://example.com/a.zip"
        ]

    def test_multiple_lines_all_urls(self):
        text = "https://example.com/a.zip\nhttps://example.com/b.zip\n"
        assert extract_download_urls(text) == [
            "https://example.com/a.zip",
            "https://example.com/b.zip",
        ]

    def test_all_or_nothing_gate(self):
        # The gate that stops the monitor firing on a chat message that happens to contain a
        # link. One non-URL line poisons the whole copy.
        assert extract_download_urls("https://example.com/a.zip\nlook at this") == []
        assert extract_download_urls("check this out\nhttps://example.com/a.zip") == []
        assert extract_download_urls("https://example.com/a.zip\n\n  \n") == [
            "https://example.com/a.zip"
        ]

    def test_rejects_plain_prose(self):
        assert extract_download_urls("just some words") == []
        assert extract_download_urls("") == []
        assert extract_download_urls("   \n  ") == []

    def test_dedupes_case_insensitively_and_keeps_order(self):
        text = (
            "https://Example.com/a.zip\n"
            "https://example.com/B.zip\n"
            "HTTPS://EXAMPLE.COM/a.zip"
        )
        assert extract_download_urls(text) == [
            "https://Example.com/a.zip",
            "https://example.com/B.zip",
        ]

    def test_honours_the_configured_maximum(self):
        text = "\n".join(f"https://example.com/{i}.zip" for i in range(30))
        assert len(extract_download_urls(text, max_urls=5)) == 5
        assert len(extract_download_urls(text, max_urls=0)) == 20  # 0 -> default

    def test_absolute_ceiling_beats_a_generous_configuration(self):
        text = "\n".join(f"https://e.com/{i}.zip" for i in range(400))
        assert len(extract_download_urls(text, max_urls=100000)) == ABSOLUTE_MAX_URLS

    def test_oversized_single_line_rejected(self):
        # The 4096 cap is per line, so one enormous line is refused rather than truncated
        # into something that looks like a URL.
        assert extract_download_urls("https://example.com/" + "a" * 5000) == []

    def test_extract_url_extension(self):
        assert extract_url_extension("https://example.com/file.txt") == ".txt"
        assert extract_url_extension("https://example.com/file.HTML?foo=bar#baz") == ".html"
        assert extract_url_extension("https://example.com/path/to/archive.tar.gz") == ".gz"
        assert extract_url_extension("https://example.com/noextension") == ""
        assert extract_url_extension("https://example.com/") == ""
        assert extract_url_extension("magnet:?xt=urn:btih:abc123") == ""

    def test_is_ignored_extension(self):
        ignored = [".txt", ".htm", ".html", ".jpg", ".jpeg", ".png", ".gif", ".webp"]
        assert is_ignored_extension("https://example.com/page.html", ignored) is True
        assert is_ignored_extension("https://example.com/image.JPG", ignored) is True
        assert is_ignored_extension("https://example.com/IMAGE.WEBP?query=1", ignored) is True
        assert is_ignored_extension("https://example.com/setup.exe", ignored) is False
        assert is_ignored_extension("https://example.com/archive.zip", ignored) is False
        assert is_ignored_extension("https://example.com/no_ext", ignored) is False
        assert is_ignored_extension("https://example.com/file.txt", []) is False
        assert is_ignored_extension("https://example.com/file.txt", ["txt"]) is True

    def test_filters_ignored_extensions(self):
        text = (
            "https://example.com/installer.exe\n"
            "https://example.com/index.html\n"
            "https://example.com/photo.jpg\n"
            "https://example.com/archive.zip"
        )
        ignored = [".html", ".jpg"]
        assert extract_download_urls(text, ignored_extensions=ignored) == [
            "https://example.com/installer.exe",
            "https://example.com/archive.zip",
        ]



# ---------------------------------------------------------------------------
# ClipboardMonitor
# ---------------------------------------------------------------------------

class TestClipboardMonitor(ConfigIsolationMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.clipboard = install_fake_clipboard(self)
        self.manager = _FakeManager()
        self.addCleanup(QApplication.processEvents)

    def make(self, **kwargs):
        kwargs.setdefault("probe", permissive_probe())
        monitor = ClipboardMonitor(self.manager, **kwargs)
        self.addCleanup(monitor.stop)
        return monitor

    # -- lifecycle ---------------------------------------------------------

    def test_start_and_stop_are_idempotent(self):
        monitor = self.make(debounce_ms=5000)
        monitor.start()
        monitor.start()
        assert monitor.is_active
        monitor.stop()
        monitor.stop()
        assert not monitor.is_active

    def test_start_is_inert_when_there_is_no_clipboard(self):
        # QGuiApplication.clipboard() can legitimately return None; the feature must then do
        # nothing rather than raise on a real desktop.
        patcher = patch.object(QGuiApplication, "clipboard", staticmethod(lambda: None))
        patcher.start()
        self.addCleanup(patcher.stop)
        monitor = self.make()
        monitor.start()
        assert not monitor.is_active

    def test_stop_without_start_does_not_raise(self):
        self.make().stop()

    def test_stop_survives_a_clipboard_that_raises(self):
        # stop() runs from MainWindow.closeEvent; an exception there would abort teardown and
        # leave the rest of the window's cleanup undone.
        monitor = self.make()
        monitor.start()
        patcher = patch.object(
            QGuiApplication,
            "clipboard",
            staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("gone"))),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        monitor.stop()
        assert monitor.is_active is False

    def test_stopped_monitor_stops_capturing(self):
        monitor = self.make()
        monitor.start()
        monitor.stop()
        self.clipboard.setText("https://example.com/a.zip")
        assert self.manager.calls == []

    # -- capturing ---------------------------------------------------------

    def test_captures_a_single_copied_url(self):
        monitor = self.make()
        self.clipboard.setText("https://example.com/a.zip")
        assert monitor.capture_now() == ["https://example.com/a.zip"]
        assert self.manager.calls == ["https://example.com/a.zip"]

    def test_ignores_prose(self):
        monitor = self.make()
        self.clipboard.setText("remember to email bob")
        assert monitor.capture_now() == []
        assert self.manager.calls == []

    def test_emits_what_it_added(self):
        monitor = self.make()
        seen = []
        monitor.urls_captured.connect(lambda urls, skipped: seen.append((urls, skipped)))
        self.clipboard.setText("https://example.com/a.zip\nhttps://example.com/b.zip")
        monitor.capture_now()
        assert seen == [
            (["https://example.com/a.zip", "https://example.com/b.zip"], 0)
        ]

    def test_reports_how_many_were_skipped_by_the_limit(self):
        monitor = self.make(max_urls=2)
        seen = []
        monitor.urls_captured.connect(lambda urls, skipped: seen.append((urls, skipped)))
        self.clipboard.setText("\n".join(f"https://example.com/{i}.zip" for i in range(5)))
        monitor.capture_now()
        assert len(seen[0][0]) == 2
        assert seen[0][1] == 3

    def test_a_duplicate_line_is_collapsed_not_reported_as_skipped(self):
        # Reporting it as "skipped" would blame the per-copy limit for dedup.
        monitor = self.make(max_urls=20)
        seen = []
        monitor.urls_captured.connect(lambda urls, skipped: seen.append((urls, skipped)))
        self.clipboard.setText("https://example.com/a.zip\nhttps://example.com/a.zip")
        monitor.capture_now()
        assert seen == [(["https://example.com/a.zip"], 0)]

    def test_nothing_skipped_when_the_copy_fits(self):
        monitor = self.make(max_urls=5)
        seen = []
        monitor.urls_captured.connect(lambda urls, skipped: seen.append((urls, skipped)))
        self.clipboard.setText("https://example.com/a.zip\nhttps://example.com/b.zip")
        monitor.capture_now()
        assert seen[0][1] == 0

    def test_already_completed_download_is_not_reported_as_added(self):
        # add_download returns None for three distinct outcomes; only a real id counts as added.
        self.manager.result = None
        monitor = self.make()
        seen = []
        monitor.urls_captured.connect(lambda urls, skipped: seen.append(urls))
        self.clipboard.setText("https://example.com/a.zip")
        assert monitor.capture_now() == []
        assert seen == []
        assert self.manager.calls == ["https://example.com/a.zip"]

    def test_one_failing_url_does_not_abort_the_rest(self):
        self.manager.result = lambda url: None if url.endswith("/b.zip") else "id"
        monitor = self.make()
        self.clipboard.setText("https://example.com/a.zip\nhttps://example.com/b.zip")
        assert monitor.capture_now() == ["https://example.com/a.zip"]

    def test_an_exception_from_the_manager_is_contained(self):
        self.manager.raises_on = {"https://example.com/b.zip"}
        monitor = self.make()
        self.clipboard.setText("https://example.com/a.zip\nhttps://example.com/b.zip")
        assert monitor.capture_now() == ["https://example.com/a.zip"]

    def test_missing_clipboard_is_handled(self):
        monitor = self.make()
        patcher = patch.object(QGuiApplication, "clipboard", staticmethod(lambda: None))
        patcher.start()
        self.addCleanup(patcher.stop)
        assert monitor.capture_now() == []

    def test_reading_text_that_raises_is_handled(self):
        monitor = self.make()

        class Boom:
            def text(self, mode=None):
                raise RuntimeError("clipboard locked")

        patcher = patch.object(QGuiApplication, "clipboard", staticmethod(lambda: Boom()))
        patcher.start()
        self.addCleanup(patcher.stop)
        assert monitor.capture_now() == []

    def test_blank_clipboard_is_a_no_op(self):
        monitor = self.make()
        self.clipboard.setText("   ")
        assert monitor.capture_now() == []

    # -- self-write suppression -------------------------------------------

    def test_suppressed_text_is_ignored_once(self):
        # MainWindow._on_copy_url writes a selected row's URL to the clipboard. Deduping
        # inside add_download is not enough: re-adding a *paused* download resumes it, so
        # Ctrl+C on a paused row would silently start it.
        monitor = self.make()
        url = "https://example.com/a.zip"
        monitor.suppress(url)
        self.clipboard.setText(url)
        assert monitor.capture_now() == []
        assert self.manager.calls == []

    def test_suppression_is_consumed_by_the_first_matching_copy(self):
        monitor = self.make()
        url = "https://example.com/a.zip"
        monitor.suppress(url)
        self.clipboard.setText(url)
        monitor.capture_now()
        # The user copies the same URL again on purpose: this one is a real capture.
        self.clipboard.setText(url)
        assert monitor.capture_now() == [url]

    def test_suppression_does_not_leak_to_other_text(self):
        monitor = self.make()
        monitor.suppress("https://example.com/a.zip")
        self.clipboard.setText("https://example.com/b.zip")
        assert monitor.capture_now() == ["https://example.com/b.zip"]

    def test_blank_suppression_is_ignored(self):
        monitor = self.make()
        monitor.suppress("   ")
        self.clipboard.setText("https://example.com/a.zip")
        assert monitor.capture_now() == ["https://example.com/a.zip"]

    def test_the_suppression_queue_is_capped(self):
        # Every entry is meant to be consumed by one clipboard change. A writer that never
        # produces a matching change must not grow this without bound.
        monitor = self.make()
        for i in range(200):
            monitor.suppress(f"https://example.com/{i}.zip")
        assert len(monitor._suppressed_texts) <= 32
        self.clipboard.setText("https://example.com/a.zip")
        assert monitor.capture_now() == ["https://example.com/a.zip"]

    def test_the_suppression_cap_trims_the_oldest_and_keeps_the_newest(self):
        monitor = self.make()
        for i in range(200):
            monitor.suppress(f"https://example.com/{i}.zip")

        self.clipboard.setText("https://example.com/199.zip")
        assert monitor.capture_now() == []

        self.clipboard.setText("https://example.com/0.zip")
        assert monitor.capture_now() == ["https://example.com/0.zip"]

    # -- debounce ----------------------------------------------------------

    def test_a_change_only_arms_the_debounce_it_does_not_capture(self):
        monitor = self.make(debounce_ms=60000)
        monitor.start()
        self.clipboard.setText("https://example.com/a.zip")
        assert self.manager.calls == []
        assert monitor._debounce.isActive()
        assert monitor._debounce.isSingleShot()

    def test_repeated_changes_restart_a_single_shot_timer(self):
        monitor = self.make(debounce_ms=60000)
        monitor.start()
        self.clipboard.setText("https://example.com/a.zip")
        first = monitor._debounce.remainingTime()
        # A burst of clipboard writes must coalesce into one read, not queue N of them.
        self.clipboard.setText("https://example.com/b.zip")
        self.clipboard.setText("https://example.com/c.zip")
        assert monitor._debounce.isActive()
        assert monitor._debounce.remainingTime() >= first - 1000

    def test_zero_debounce_still_captures(self):
        monitor = self.make(debounce_ms=0)
        self.clipboard.setText("https://example.com/a.zip")
        assert monitor.capture_now() == ["https://example.com/a.zip"]

    # -- metadata & ignored extensions ------------------------------------

    def test_passes_pending_min_bytes_metadata(self):
        monitor = self.make(min_file_size_kb=1024)
        self.clipboard.setText("https://example.com/file.iso")
        monitor.capture_now()
        assert len(self.manager.calls) == 1
        assert self.manager.calls[0] == "https://example.com/file.iso"
        assert len(self.manager.kwargs_calls) == 1
        assert self.manager.kwargs_calls[0]["metadata"] == {
            "capture_source": "clipboard",
            "pending_min_bytes": 1024 * 1024,
        }

    def test_passes_no_pending_min_bytes_when_zero(self):
        monitor = self.make(min_file_size_kb=0)
        self.clipboard.setText("https://example.com/file.iso")
        monitor.capture_now()
        assert len(self.manager.kwargs_calls) == 1
        assert self.manager.kwargs_calls[0]["metadata"] == {
            "capture_source": "clipboard",
        }


    def test_skips_ignored_extensions(self):
        monitor = self.make(ignored_extensions=[".html", ".jpg", ".txt"])
        self.clipboard.setText("https://example.com/page.html")
        assert monitor.capture_now() == []
        assert self.manager.calls == []

    def test_setters_update_configuration(self):
        monitor = self.make(max_urls=5, min_file_size_kb=512, ignored_extensions=[".txt"])
        monitor.set_max_urls(2)
        monitor.set_min_file_size_kb(2048)
        monitor.set_ignored_extensions([".zip"])

        self.clipboard.setText("https://example.com/archive.zip")
        assert monitor.capture_now() == []

        self.clipboard.setText("https://example.com/doc.txt")
        assert monitor.capture_now() == ["https://example.com/doc.txt"]
        assert self.manager.kwargs_calls[-1]["metadata"]["pending_min_bytes"] == 2048 * 1024

    # -- resolve-before-capture --------------------------------------------

    def test_a_url_that_is_not_a_file_is_never_added(self):
        # The bug this whole path exists for: a bare page link copied out of the address bar
        # used to become a row that failed, and the user had to find it and delete it.
        monitor = self.make(probe=permissive_probe(ok=True, size=4096,
                                                   content_type="text/html"))
        seen = []
        monitor.urls_filtered.connect(seen.extend)
        self.clipboard.setText("https://bag-buddy-f0896.firebaseapp.com/")
        assert monitor.capture_now() == []
        assert self.manager.calls == []
        assert seen == [("https://bag-buddy-f0896.firebaseapp.com/", REASON_HTML_PAGE)]

    def test_a_file_below_the_minimum_is_never_added(self):
        # A real file, just a small one: the size minimum has to hold against a resolved URL,
        # not only against one whose extension said so.
        monitor = self.make(min_file_size_kb=1024,
                            probe=permissive_probe(size=500 * 1024))
        seen = []
        monitor.urls_filtered.connect(seen.extend)
        self.clipboard.setText("https://example.com/small.pdf")
        assert monitor.capture_now() == []
        assert self.manager.calls == []
        assert seen == [("https://example.com/small.pdf", REASON_TOO_SMALL)]

    def test_a_file_above_the_minimum_is_added(self):
        monitor = self.make(min_file_size_kb=1024, probe=permissive_probe(size=2 * 1024 * 1024))
        self.clipboard.setText("https://example.com/big.pdf")
        assert monitor.capture_now() == ["https://example.com/big.pdf"]

    def test_an_unreachable_url_is_dropped_rather_than_queued(self):
        # A 404 or a login-walled link must not become a permanent error row either.
        monitor = self.make(probe=permissive_probe(ok=False, size=0, status=404,
                                                   error="HTTP 404"))
        seen = []
        monitor.urls_filtered.connect(seen.extend)
        self.clipboard.setText("https://example.com/gone.zip")
        assert monitor.capture_now() == []
        assert self.manager.calls == []
        assert seen == [("https://example.com/gone.zip", REASON_UNREACHABLE)]

    def test_an_unknown_size_is_captured_not_refused(self):
        # size == 0 means *unknown* (chunked, or a HEAD the origin refused). Refusing every
        # unsizeable response would reject every gzip-encoded download there is.
        monitor = self.make(min_file_size_kb=1024, probe=permissive_probe(size=0))
        self.clipboard.setText("https://example.com/stream.bin")
        assert monitor.capture_now() == ["https://example.com/stream.bin"]

    def test_the_ignored_extension_catches_the_resolved_filename(self):
        # /download?id=9 has no extension to ignore in the URL; the probe finds out it serves
        # a .txt. Checking only the copied URL is what let these through.
        async def probe(url):
            return ProbeResult(url=url, ok=True, size=64 * 1024 * 1024,
                               filename="notes.txt", content_type="text/plain")

        monitor = self.make(ignored_extensions=[".txt"], probe=probe)
        seen = []
        monitor.urls_filtered.connect(seen.extend)
        self.clipboard.setText("https://example.com/download?id=9")
        assert monitor.capture_now() == []
        assert seen == [("https://example.com/download?id=9", REASON_IGNORED_EXT)]

    def test_the_probe_filename_is_handed_to_the_manager(self):
        # Otherwise the row appears nameless until the engine's own probe lands.
        async def probe(url):
            return ProbeResult(url=url, ok=True, size=64 * 1024 * 1024,
                               filename="ubuntu.iso", content_type="application/octet-stream")

        monitor = self.make(probe=probe)
        self.clipboard.setText("https://example.com/download?id=9")
        assert monitor.capture_now() == ["https://example.com/download?id=9"]
        assert self.manager.kwargs_calls[0]["filename"] == "ubuntu.iso"

    def test_one_unreachable_url_does_not_cost_the_others(self):
        async def probe(url):
            if "bad" in url:
                return ProbeResult(url=url, ok=False, error="HTTP 500")
            return ProbeResult(url=url, ok=True, size=64 * 1024 * 1024,
                               content_type="application/octet-stream")

        monitor = self.make(probe=probe)
        self.clipboard.setText(
            "https://example.com/bad.zip\nhttps://example.com/good.zip"
        )
        assert monitor.capture_now() == ["https://example.com/good.zip"]

    def test_a_probe_that_raises_is_treated_as_unreachable(self):
        async def probe(url):
            raise RuntimeError("dns exploded")

        monitor = self.make(probe=probe)
        seen = []
        monitor.urls_filtered.connect(seen.extend)
        self.clipboard.setText("https://example.com/a.zip")
        assert monitor.capture_now() == []
        assert self.manager.calls == []
        assert seen == [("https://example.com/a.zip", REASON_UNREACHABLE)]

    def test_a_magnet_link_is_not_probed(self):
        # Nothing to ask and no size to weigh: probing it could only report failure.
        calls = []

        async def probe(url):
            calls.append(url)
            return ProbeResult(url=url, ok=True, size=64 * 1024 * 1024)

        monitor = self.make(probe=probe)
        magnet = "magnet:?xt=urn:btih:0123456789abcdef"
        self.clipboard.setText(magnet)
        assert monitor.capture_now() == [magnet]
        assert calls == []

    def test_pending_min_bytes_survives_a_resolved_size(self):
        # The engine probes again authoritatively; a URL that shrank in between must still be
        # refused there, so the deferral cannot be dropped just because the probe succeeded.
        monitor = self.make(min_file_size_kb=1024, probe=permissive_probe(size=64 * 1024 * 1024))
        self.clipboard.setText("https://example.com/big.pdf")
        monitor.capture_now()
        assert self.manager.kwargs_calls[0]["metadata"]["pending_min_bytes"] == 1024 * 1024

    def test_a_deferred_run_async_does_not_lose_the_capture(self):
        # Production schedules on the manager's loop and returns at once; capture_now must not
        # report that as "nothing happened".
        self.manager = _FakeManager()
        monitor = ClipboardMonitor(
            self.manager, debounce_ms=0, probe=permissive_probe(),
            run_async=lambda coro: run_async(coro),
        )
        self.addCleanup(monitor.stop)
        self.clipboard.setText("https://example.com/a.zip")
        assert monitor.capture_now() == ["https://example.com/a.zip"]

    def test_a_scheduler_that_raises_does_not_escape_the_timer_slot(self):
        # This runs from a QTimer; an exception there tears down the connection and the
        # monitor silently stops capturing forever.
        def explode(coro):
            coro.close()
            raise RuntimeError("loop is gone")

        monitor = ClipboardMonitor(self.manager, debounce_ms=0,
                                  probe=permissive_probe(), run_async=explode)
        self.addCleanup(monitor.stop)
        self.clipboard.setText("https://example.com/a.zip")
        assert monitor.capture_now() == []


# ---------------------------------------------------------------------------
# decide_capture
# ---------------------------------------------------------------------------

class TestDecideCapture(unittest.TestCase):
    """The filter itself, independent of the monitor around it."""

    BIG = ProbeResult(url="https://e.com/a.zip", ok=True, size=64 * 1024 * 1024,
                      content_type="application/octet-stream")

    def test_a_resolvable_file_is_accepted(self):
        assert decide_capture("https://e.com/a.zip", self.BIG) == (True, "")

    def test_missing_and_failed_probes_are_unreachable(self):
        assert decide_capture("https://e.com/a.zip", None) == (False, REASON_UNREACHABLE)
        assert decide_capture(
            "https://e.com/a.zip", ProbeResult(url="https://e.com/a.zip", ok=False)
        ) == (False, REASON_UNREACHABLE)

    def test_html_is_a_page_however_small_the_setting(self):
        # With no minimum configured the size check cannot catch it, so this has to stand alone.
        page = ProbeResult(url="https://e.com/", ok=True, size=5_000_000,
                           content_type="text/html; charset=utf-8")
        assert decide_capture("https://e.com/", page, min_bytes=0) == (False, REASON_HTML_PAGE)

    def test_the_size_check_is_off_when_the_minimum_is_zero(self):
        tiny = ProbeResult(url="https://e.com/a.zip", ok=True, size=10)
        assert decide_capture("https://e.com/a.zip", tiny, min_bytes=0) == (True, "")

    def test_a_size_exactly_at_the_minimum_is_kept(self):
        # The setting is a *minimum*: a file of exactly the threshold size is one the user asked
        # for, and an off-by-one here silently drops the smallest file they configured to keep.
        exact = ProbeResult(url="https://e.com/a.zip", ok=True, size=1024)
        assert decide_capture("https://e.com/a.zip", exact, min_bytes=1024) == (True, "")

    def test_the_extension_check_reads_the_resolved_name_too(self):
        named = ProbeResult(url="https://e.com/d", ok=True, size=64 * 1024 * 1024,
                            filename="page.html")
        assert decide_capture("https://e.com/d", named,
                              ignored_extensions=[".html"]) == (False, REASON_IGNORED_EXT)

    def test_the_extension_check_normalises_a_bare_dotless_name(self):
        # filename_from_headers can return "notes.txt"; is_ignored_extension adds the dot, so
        # a user who typed "txt" and a user who typed ".txt" get the same answer.
        named = ProbeResult(url="https://e.com/d", ok=True, size=1, filename="notes.txt")
        assert decide_capture("https://e.com/d", named,
                              ignored_extensions=["txt"])[0] is False


# ---------------------------------------------------------------------------
# resolve_candidates
# ---------------------------------------------------------------------------

class TestResolveCandidates(unittest.TestCase):
    def test_results_keep_url_probe_and_reason_in_order(self):
        async def probe(url):
            if url.endswith("page.html"):
                return ProbeResult(url=url, ok=True, size=9000, content_type="text/html")
            return ProbeResult(url=url, ok=True, size=8 << 20)

        got = run_async(resolve_candidates(
            ["https://e.com/a.zip", "https://e.com/page.html"],
            probe=probe, min_bytes=1024 * 1024,
        ))
        assert [(u, r) for u, p, r in got] == [
            ("https://e.com/a.zip", ""),
            ("https://e.com/page.html", REASON_HTML_PAGE),
        ]
        # A rejected URL carries no probe: the caller must not act on a result it refused.
        assert got[1][1] is None
        assert got[0][1] is not None

    def test_an_empty_list_needs_no_probe(self):
        assert run_async(resolve_candidates([])) == []

    def test_probes_run_concurrently(self):
        # A pasted list of 20 URLs must not take 20 x the probe timeout; the semaphore is what
        # stops that without turning the origin into a rate-limit target.
        import asyncio

        live = 0
        peak = 0

        async def probe(url):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0)
            live -= 1
            return ProbeResult(url=url, ok=True, size=1 << 30)

        urls = [f"https://e.com/{i}.zip" for i in range(20)]
        run_async(resolve_candidates(urls, probe=probe, max_concurrent=4))
        assert peak <= 4


# ---------------------------------------------------------------------------
# http_probe helpers
# ---------------------------------------------------------------------------

class TestProbeHelpers(unittest.TestCase):
    def test_only_http_schemes_are_probeable(self):
        for url in ("http://e.com/a.zip", "https://e.com/a.zip", "HTTPS://E.COM/A.ZIP"):
            assert is_probeable(url), url
        for url in ("magnet:?xt=urn:btih:abc", "ftp://e.com/a.zip",
                    "file:///c:/x.torrent", "blob:https://e.com/x", "", None):
            assert not is_probeable(url), url

    def test_html_counts_as_a_file_only_when_the_url_says_so(self):
        # /api/export answering text/html is a browser answering, not a file being offered; its
        # Content-Length is the size of a page and must not satisfy a minimum-size filter.
        assert html_is_a_file("https://e.com/report.html") is True
        assert html_is_a_file("https://e.com/report.htm?a=1") is True
        assert html_is_a_file("https://e.com/api/export") is False
        assert html_is_a_file("https://e.com/") is False

    def test_content_disposition_wins_over_the_url(self):
        assert filename_from_headers(
            {"Content-Disposition": 'attachment; filename="ubuntu.iso"'}, "https://e.com/d?id=1"
        ) == "ubuntu.iso"

    def test_the_rfc5987_form_is_preferred(self):
        # filename* survives non-ASCII names the quoted form is not allowed to carry.
        assert filename_from_headers(
            {"Content-Disposition": "attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf"},
            "https://e.com/d",
        ) == "résumé.pdf"

    def test_a_bare_header_filename_still_yields_an_extension(self):
        # Some origins send an unquoted `filename=report.pdf` plus a decorative
        # `filename*=UTF-8''report` with no extension in it; the usable one must win.
        assert filename_from_headers(
            {"Content-Disposition": "attachment; filename*=UTF-8''report; filename=report.pdf"},
            "https://e.com/d",
        ) == "report.pdf"

    def test_the_final_url_is_the_fallback(self):
        assert filename_from_headers({}, "https://e.com/files/ubuntu.iso?sig=abc") == "ubuntu.iso"

    def test_a_probe_result_defaults_to_unknown_not_empty(self):
        r = ProbeResult(url="https://e.com/a.zip")
        # 0 is "unknown" and False is "did not answer"; conflating them would make a caller
        # treat a timeout as an empty file.
        assert r.size == 0
        assert r.ok is False
        assert r.is_html is False



# ---------------------------------------------------------------------------
# parse_hotkey
# ---------------------------------------------------------------------------

class TestParseHotkey(unittest.TestCase):
    def test_accepted_chords(self):
        cases = {
            "Ctrl+Alt+D": (hotkey_module.MOD_CONTROL | hotkey_module.MOD_ALT, 0x44),
            "Ctrl+Shift+Y": (hotkey_module.MOD_CONTROL | hotkey_module.MOD_SHIFT, 0x59),
            "Alt+F4": (hotkey_module.MOD_ALT, 0x73),
            "Alt+F12": (hotkey_module.MOD_ALT, 0x7B),
            "Ctrl+Space": (hotkey_module.MOD_CONTROL, 0x20),
            "Ctrl+Return": (hotkey_module.MOD_CONTROL, 0x0D),
            "Ctrl+Alt+Escape": (
                hotkey_module.MOD_CONTROL | hotkey_module.MOD_ALT, 0x1B,
            ),
            "Shift+Alt+PgDn": (
                hotkey_module.MOD_SHIFT | hotkey_module.MOD_ALT, 0x22,
            ),
        }
        for sequence, expected in cases.items():
            with self.subTest(sequence=sequence):
                assert parse_hotkey(sequence) == expected

    def test_windows_key_spellings_all_resolve_to_mod_win(self):
        # Qt drops a Meta/Win chord entirely (QKeySequence("Win+D") is empty), which is why
        # this parser works on the string instead of on a QKeySequence.
        for spelling in ("Win+D", "Meta+D", "Windows+D", "Super+D", "win+d"):
            with self.subTest(spelling=spelling):
                assert parse_hotkey(spelling) == (
                    hotkey_module.MOD_WIN, 0x44,
                )

    def test_shift_only_letters_do_not_imply_shift(self):
        # VkKeyScanW('D') reports "needs shift" for the character D, which would silently
        # turn Ctrl+Alt+D into Ctrl+Alt+Shift+D. Letters are looked up in lower case.
        assert parse_hotkey("Ctrl+Alt+D") == (
            hotkey_module.MOD_CONTROL | hotkey_module.MOD_ALT, 0x44,
        )

    def test_symbols_keep_the_shift_the_layout_needs(self):
        # '+' lives on the VK 0xBB key and genuinely requires shift on a US layout.
        mods, vk = parse_hotkey("Ctrl+Alt++")
        assert vk == 0xBB
        assert mods & hotkey_module.MOD_SHIFT

    def test_refuses_a_chord_with_no_system_modifier(self):
        # Claiming a bare key system-wide would swallow it in every other application.
        for sequence in ("D", "F5", "Shift+D", "+", "", "   ", "Ctrl+K, Ctrl+B"):
            with self.subTest(sequence=sequence):
                assert parse_hotkey(sequence) is None, sequence

    def test_refuses_unknown_keys_and_modifiers(self):
        assert parse_hotkey("Ctrl+Alt+NonsenseKey") is None
        assert parse_hotkey("Hyper+D") is None
        assert parse_hotkey("Ctrl+F99") is None

    def test_none_input(self):
        assert parse_hotkey(None) is None

    def test_describe_modifiers(self):
        assert describe_modifiers(
            hotkey_module.MOD_CONTROL | hotkey_module.MOD_ALT
        ) == "Ctrl+Alt"
        assert describe_modifiers(0) == ""


# ---------------------------------------------------------------------------
# HotkeyRegistration
# ---------------------------------------------------------------------------

class _Api:
    """Callable wrapper so a fake can accept the ``argtypes``/``restype`` ctypes assigns."""

    def __init__(self, fn):
        self._fn = fn
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._fn(*args)


class _FakeUser32:
    def __init__(self, register_ok=True):
        self.register_ok = register_ok
        self.register_calls: list[tuple] = []
        self.unregister_calls: list[tuple] = []
        # Win32 virtual-key codes for letters are the *upper* case ASCII value: the real
        # VkKeyScanW('d') is 0x44, not 0x64. Digits are unchanged by upper().
        self.VkKeyScanW = _Api(lambda ch: ord(ch.upper()) if len(ch) == 1 else -1)
        self.RegisterHotKey = _Api(self._register)
        self.UnregisterHotKey = _Api(self._unregister)

    def _register(self, hwnd, hotkey_id, mods, vk):
        self.register_calls.append((hwnd, hotkey_id, mods, vk))
        return self.register_ok

    def _unregister(self, hwnd, hotkey_id):
        self.unregister_calls.append((hwnd, hotkey_id))
        return True


class HotkeyTestCase(ConfigIsolationMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._real_user32 = hotkey_module._USER32
        self.addCleanup(self._restore_user32)

    def _restore_user32(self):
        hotkey_module._USER32 = self._real_user32

    def use_fake_user32(self, register_ok=True):
        fake = _FakeUser32(register_ok=register_ok)
        hotkey_module._USER32 = fake
        return fake

    def make_registration(self):
        registration = HotkeyRegistration()
        self.addCleanup(registration.unregister)
        self.addCleanup(self._remove_filters, registration)
        return registration

    @staticmethod
    def _remove_filters(registration):
        core = QCoreApplication.instance()
        if core is not None:
            # PySide6 exposes no way to enumerate installed native filters, and removing one
            # that was never installed is a no-op, so this is safe to call unconditionally.
            # Without it a leftover filter outlives the test and sees later tests' messages.
            core.removeNativeEventFilter(registration._filter)


class TestHotkeyRegistration(HotkeyTestCase):
    def test_register_claims_the_parsed_chord(self):
        fake = self.use_fake_user32()
        registration = self.make_registration()

        ok, message = registration.register("Ctrl+Alt+D")

        assert ok, message
        assert fake.register_calls == [
            (None, HOTKEY_ID, hotkey_module.MOD_CONTROL | hotkey_module.MOD_ALT, 0x44)
        ]
        assert registration.is_registered
        assert registration.sequence == "Ctrl+Alt+D"
        assert registration.last_error == ""

    def test_already_registered_reports_the_conflict(self):
        self.use_fake_user32(register_ok=False)
        ctypes.set_last_error(ERROR_HOTKEY_ALREADY_REGISTERED)
        registration = self.make_registration()

        ok, message = registration.register("Ctrl+Alt+D")

        assert not ok
        assert "already used by another application" in message
        assert not registration.is_registered
        assert registration.sequence == ""

    def test_other_failure_reports_the_error_code(self):
        self.use_fake_user32(register_ok=False)
        ctypes.set_last_error(5)
        registration = self.make_registration()

        ok, message = registration.register("Ctrl+Alt+D")

        assert not ok
        assert "error 5" in message

    def test_rejects_an_unusable_chord_without_calling_win32(self):
        fake = self.use_fake_user32()
        registration = self.make_registration()

        ok, message = registration.register("D")

        assert not ok
        assert "Ctrl, Alt or Win" in message
        assert fake.register_calls == []

    def test_blank_sequence_is_refused(self):
        fake = self.use_fake_user32()
        registration = self.make_registration()
        assert registration.register("")[0] is False
        assert fake.register_calls == []

    def test_registering_the_same_chord_twice_is_a_no_op(self):
        # Re-registering would unregister first, opening a window in which the chord belongs
        # to nobody; and if the second claim then failed, the hotkey would be gone entirely
        # rather than merely stale.
        fake = self.use_fake_user32()
        registration = self.make_registration()

        assert registration.register("Ctrl+Alt+D")[0] is True
        assert registration.register("Ctrl+Alt+D")[0] is True

        assert len(fake.register_calls) == 1
        assert fake.unregister_calls == []

    def test_unregister_is_idempotent(self):
        fake = self.use_fake_user32()
        registration = self.make_registration()
        registration.register("Ctrl+Alt+D")

        registration.unregister()
        registration.unregister()

        assert fake.unregister_calls == [(None, HOTKEY_ID)]
        assert not registration.is_registered

    def test_unregister_without_register_is_a_no_op(self):
        fake = self.use_fake_user32()
        self.make_registration().unregister()
        assert fake.unregister_calls == []

    def test_re_registering_replaces_the_previous_binding(self):
        fake = self.use_fake_user32()
        registration = self.make_registration()

        registration.register("Ctrl+Alt+D")
        registration.register("Ctrl+Shift+Y")

        # Leaving the first chord claimed would make it unusable system-wide forever.
        assert fake.unregister_calls == [(None, HOTKEY_ID)]
        assert len(fake.register_calls) == 2
        assert registration.sequence == "Ctrl+Shift+Y"

    def test_no_event_loop_means_no_registration(self):
        fake = self.use_fake_user32()
        registration = self.make_registration()
        patcher = patch.object(QCoreApplication, "instance", staticmethod(lambda: None))
        patcher.start()
        self.addCleanup(patcher.stop)

        ok, message = registration.register("Ctrl+Alt+D")

        assert not ok
        assert "native event filter" in message
        assert fake.register_calls == []

    @unittest.skipUnless(sys.platform == "win32", "Windows-only platform gate")
    def test_off_windows_is_refused(self):
        self.use_fake_user32()
        registration = self.make_registration()
        patcher = patch.object(hotkey_module.sys, "platform", "linux")
        patcher.start()
        self.addCleanup(patcher.stop)

        ok, message = registration.register("Ctrl+Alt+D")

        assert not ok
        assert "only available on Windows" in message


class TestHotkeyNativeEvent(HotkeyTestCase):
    def _deliver(self, registration, message, wparam, lparam=0):
        msg = ctypes.wintypes.MSG()
        msg.message = message
        msg.wParam = wparam
        msg.lParam = lparam
        return registration._filter.nativeEventFilter(
            b"windows_generic_MSG", ctypes.addressof(msg)
        )

    def test_wm_hotkey_for_our_id_emits_triggered(self):
        self.use_fake_user32()
        registration = self.make_registration()
        registration.register("Ctrl+Alt+D")

        fired = []
        registration.triggered.connect(lambda: fired.append(True))
        handled, _result = self._deliver(registration, WM_HOTKEY, HOTKEY_ID)

        assert handled is True
        assert fired == [True]

    def test_wm_hotkey_for_another_app_is_left_alone(self):
        self.use_fake_user32()
        registration = self.make_registration()
        registration.register("Ctrl+Alt+D")

        fired = []
        registration.triggered.connect(lambda: fired.append(True))
        handled, _result = self._deliver(registration, WM_HOTKEY, HOTKEY_ID + 1)

        assert handled is False
        assert fired == []

    def test_unrelated_windows_messages_are_left_alone(self):
        self.use_fake_user32()
        registration = self.make_registration()
        fired = []
        registration.triggered.connect(lambda: fired.append(True))

        handled, _result = self._deliver(registration, 0x0001, HOTKEY_ID)  # WM_CREATE

        assert handled is False
        assert fired == []

    def test_an_undecodable_message_does_not_raise(self):
        self.use_fake_user32()
        registration = self.make_registration()
        assert registration._filter.nativeEventFilter(b"x", 0) == (False, 0)


# ---------------------------------------------------------------------------
# Capture toggle: manager + browser server
# ---------------------------------------------------------------------------

class TestCaptureToggle(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.open()
        self.addCleanup(self.db.close)
        self.manager = DownloadManager(self.db)
        self.addCleanup(lambda: setattr(self.manager, "_stopped", True))

    def test_flips_intercept_all_without_disabling_integration(self):
        seen = []
        self.manager.browser_config_changed.connect(seen.append)

        changed, message = self.manager.set_capture_enabled(False)

        assert changed is True
        assert "disabled" in message
        assert self.manager.browser_config.intercept_all is False
        # enabled must stay True: flipping it tears the loopback server down, so the extension
        # could no longer reach the app to be told why its capture was declined.
        assert self.manager.browser_config.enabled is True
        assert len(seen) == 1

    def test_round_trip_back_on(self):
        self.manager.set_capture_enabled(False)
        changed, _message = self.manager.set_capture_enabled(True)
        assert changed is True
        assert self.manager.browser_config.intercept_all is True

    def test_is_a_no_op_when_already_in_that_state(self):
        changed, message = self.manager.set_capture_enabled(True)
        assert changed is False
        assert "already on" in message

    def test_toggle_survives_a_config_reload(self):
        self.manager.set_capture_enabled(False)
        assert BrowserIntegrationConfig.load().intercept_all is False


# ---------------------------------------------------------------------------
# MainWindow integration
# ---------------------------------------------------------------------------

class TestCaptureIntegration(_MainWindowTestCase):
    """The window owns both capture objects, so the wiring is what needs pinning here.

    Both are created in ``_setup_capture`` and must survive it: a ``QObject`` with no Python
    reference is collectable, and that would silently take the clipboard subscription or the
    OS hotkey registration with it.
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(self._restore_user32)
        self.use_fake_user32()
        # The window builds the real monitor, which means the real probe and therefore a real
        # DNS lookup the moment anything is copied. Replaced here so the wiring tests stay
        # hermetic; the probe's own behaviour is covered by TestClipboardMonitor below.
        if self.win._clipboard_monitor is not None:
            self.win._clipboard_monitor.set_probe(permissive_probe())

    def _restore_user32(self):
        hotkey_module._USER32 = self._real_user32

    def use_fake_user32(self, register_ok=True):
        self._real_user32 = hotkey_module._USER32
        fake = _FakeUser32(register_ok=register_ok)
        hotkey_module._USER32 = fake
        return fake

    def test_both_capture_objects_exist_after_construction(self):
        assert isinstance(self.win._clipboard_monitor, ClipboardMonitor)
        assert isinstance(self.win._hotkey, HotkeyRegistration)

    def test_capture_stays_off_by_default(self):
        assert self.win._clipboard_monitor.is_active is False
        assert self.win._hotkey.is_registered is False

    def test_enabling_via_preferences_starts_the_monitor(self):
        cfg = self.manager.general_config
        cfg.clipboard_monitor_enabled = True
        self.manager.general_config_changed.emit(cfg)
        assert self.win._clipboard_monitor.is_active is True

        cfg.clipboard_monitor_enabled = False
        self.manager.general_config_changed.emit(cfg)
        assert self.win._clipboard_monitor.is_active is False

    def test_enabling_via_preferences_registers_the_hotkey(self):
        fake = self.use_fake_user32()
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "Ctrl+Shift+K"
        self.manager.general_config_changed.emit(cfg)

        assert self.win._hotkey.is_registered is True
        assert self.win._hotkey.sequence == "Ctrl+Shift+K"
        assert len(fake.register_calls) == 1

        cfg.capture_hotkey_enabled = False
        self.manager.general_config_changed.emit(cfg)
        assert self.win._hotkey.is_registered is False
        assert fake.unregister_calls == [(None, HOTKEY_ID)]

    def test_a_chord_owned_elsewhere_is_reported_in_the_status_line(self):
        self.use_fake_user32(register_ok=False)
        ctypes.set_last_error(ERROR_HOTKEY_ALREADY_REGISTERED)
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        self.manager.general_config_changed.emit(cfg)

        assert self.win._hotkey.is_registered is False
        assert "already used by another application" in self.win._status_label.text()

    def test_the_hotkey_toggles_capture(self):
        self.use_fake_user32()
        before = self.manager.browser_config.intercept_all
        self.win._on_global_hotkey()
        assert self.manager.browser_config.intercept_all is not before
        self.win._on_global_hotkey()
        assert self.manager.browser_config.intercept_all is before

    def test_the_hotkey_does_not_silence_the_tray_checkbox(self):
        self.use_fake_user32()
        if self.win._tray_act_capture is None:
            self.skipTest("no system tray available on this host")
        self.win._on_global_hotkey()
        assert self.win._tray_act_capture.isChecked() == (
            self.manager.browser_config.intercept_all
        )

    def test_toggling_the_tray_checkbox_moves_the_config(self):
        if self.win._tray_act_capture is None:
            self.skipTest("no system tray available on this host")
        self.win._tray_act_capture.setChecked(False)
        assert self.manager.browser_config.intercept_all is False
        self.win._tray_act_capture.setChecked(True)
        assert self.manager.browser_config.intercept_all is True

    def test_toggling_the_clipboard_tray_checkbox_moves_the_config(self):
        if self.win._tray_act_clipboard is None:
            self.skipTest("no system tray available on this host")
        self.win._tray_act_clipboard.setChecked(True)
        assert self.manager.general_config.clipboard_monitor_enabled is True
        assert self.win._clipboard_monitor.is_active is True

        self.win._tray_act_clipboard.setChecked(False)
        assert self.manager.general_config.clipboard_monitor_enabled is False
        assert self.win._clipboard_monitor.is_active is False

    def test_closing_releases_the_hotkey(self):
        # A hotkey left registered outlives the process: Windows keeps the chord claimed and
        # the next launch cannot take it, with no way for the user to tell why.
        fake = self.use_fake_user32()
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        self.manager.general_config_changed.emit(cfg)
        assert self.win._hotkey.is_registered is True

        self.win._force_exit = True
        self.win.close()

        assert fake.unregister_calls == [(None, HOTKEY_ID)]
        assert self.win._hotkey.is_registered is False
        assert self.win._clipboard_monitor.is_active is False

    def test_copying_a_row_does_not_re_add_it(self):
        entry = DownloadEntry(
            id="cap1",
            url="https://example.com/copied.zip",
            filename="copied.zip",
            status="paused",
        )
        self.db.add_download(entry)
        self.win._load_history()
        self.win._table.selectAll()
        assert len(self.win._selected_ids()) == 1

        fake = install_fake_clipboard(self)
        self.win._on_copy_url()
        assert fake.text() == entry.url

        # A *paused* row is the interesting case: without the suppression, add_download would
        # dedup it by calling resume_download, so Ctrl+C on a paused row would start it.
        self.win._clipboard_monitor._manager = _FakeManager()
        assert self.win._clipboard_monitor.capture_now() == []
        assert self.win._clipboard_monitor._manager.calls == []

    def test_clipboard_capture_reports_what_it_added(self):
        monitor = self.win._clipboard_monitor
        monitor._manager = _FakeManager()
        fake = install_fake_clipboard(self)
        fake.setText("https://example.com/a.zip\nhttps://example.com/b.zip")

        monitor.capture_now()

        assert "2 URLs" in self.win._status_label.text()


# ---------------------------------------------------------------------------
# Config round-trip
# ---------------------------------------------------------------------------

class TestCaptureConfig(ConfigIsolationMixin, unittest.TestCase):
    def test_defaults_are_opt_in(self):
        cfg = GeneralConfig()
        assert cfg.clipboard_monitor_enabled is False
        assert cfg.capture_hotkey_enabled is False
        assert cfg.capture_hotkey_sequence == "Ctrl+Alt+D"
        assert cfg.clipboard_monitor_max_urls == 20

    def test_round_trips_through_qsettings(self):
        cfg = GeneralConfig()
        cfg.clipboard_monitor_enabled = True
        cfg.clipboard_monitor_max_urls = 7
        cfg.capture_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "Ctrl+Shift+K"
        cfg.save()

        loaded = GeneralConfig.load()

        assert loaded.clipboard_monitor_enabled is True
        assert loaded.clipboard_monitor_max_urls == 7
        assert loaded.capture_hotkey_enabled is True
        assert loaded.capture_hotkey_sequence == "Ctrl+Shift+K"

    def test_round_trips_through_a_dict(self):
        cfg = GeneralConfig()
        cfg.clipboard_monitor_enabled = True
        cfg.capture_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "Alt+F9"
        cfg.clipboard_monitor_max_urls = 3

        rebuilt = GeneralConfig.from_dict(cfg.to_dict())

        assert rebuilt.clipboard_monitor_enabled is True
        assert rebuilt.capture_hotkey_enabled is True
        assert rebuilt.capture_hotkey_sequence == "Alt+F9"
        assert rebuilt.clipboard_monitor_max_urls == 3

    def test_a_zero_or_missing_limit_is_clamped(self):
        assert GeneralConfig.from_dict({"clipboard_monitor_max_urls": 0}
                                       ).clipboard_monitor_max_urls == 1
        assert GeneralConfig.from_dict({}).clipboard_monitor_max_urls == 20

    def test_an_empty_sequence_falls_back_to_the_default(self):
        assert GeneralConfig.from_dict(
            {"capture_hotkey_sequence": ""}
        ).capture_hotkey_sequence == "Ctrl+Alt+D"

    def test_from_dict_coerces_junk(self):
        cfg = GeneralConfig.from_dict(
            {
                "clipboard_monitor_enabled": 1,
                "capture_hotkey_enabled": 0,
                "clipboard_monitor_max_urls": "5",
            }
        )
        assert cfg.clipboard_monitor_enabled is True
        assert cfg.capture_hotkey_enabled is False
        assert cfg.clipboard_monitor_max_urls == 5