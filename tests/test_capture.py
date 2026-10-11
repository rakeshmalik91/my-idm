"""Unit tests for clipboard capture and the global capture hotkey.

Both features are opt-in and both are "does the app notice something the user did without
asking it to" — the failure modes that matter are silence and over-reach, so the tests are
weighted towards the boundaries rather than the happy path.
"""

import ctypes
import ctypes.wintypes
import sys
import tempfile
import time
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
    BINDING_CAPTURE,
    BINDING_CLIPBOARD,
    CLIPBOARD_HOTKEY_ID,
    DEFAULT_LOCK_KEY,
    ERROR_HOTKEY_ALREADY_REGISTERED,
    HOTKEY_ID,
    LOCK_KEYS,
    LOCK_KEY_LABELS,
    VK_NUMLOCK,
    VK_SCROLLLOCK,
    WM_HOTKEY,
    HotkeyManager,
    HotkeyRegistration,
    canonical_lock_key,
    lock_key_is_on,
    lock_key_state,
    lock_key_vk,
    parse_hotkey,
    set_lock_key,
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
    def test_only_the_two_lock_keys_resolve(self):
        """The capture keys are lock keys and nothing else, so chords no longer parse.

        Not a reduced ambition: a chord has one meaning, while a lock key carries state the
        app reads. Only the second can drive a capture that follows the LED, and pretending a
        chord could would give the user a key that appears to bind and does nothing.
        """
        assert parse_hotkey("ScrollLock") == (0, VK_SCROLLLOCK)
        assert parse_hotkey("NumLock") == (0, VK_NUMLOCK)
        # Both spellings, because Qt writes them without a space and people type them with one.
        assert parse_hotkey("scroll lock") == (0, VK_SCROLLLOCK)
        assert parse_hotkey("NUM LOCK") == (0, VK_NUMLOCK)

    def test_every_chord_is_refused(self):
        for sequence in (
            "Ctrl+Alt+D",
            "Ctrl+Shift+K",
            "Alt+F4",
            "Win+D",
            "Ctrl+ScrollLock",
            "ScrollLock+Alt",
            "Ctrl+K, Ctrl+B",
        ):
            with self.subTest(sequence=sequence):
                assert parse_hotkey(sequence) is None, sequence

    def test_bare_and_unknown_keys_are_refused(self):
        # A bare letter claimed system-wide would swallow it in every other application, so
        # it has to be refused rather than registered.
        for sequence in ("D", "F5", "CapsLock", "PrintScreen", "Home", "NonsenseKey"):
            with self.subTest(sequence=sequence):
                assert parse_hotkey(sequence) is None, sequence

    def test_blank_and_none_input(self):
        for sequence in (None, "", "   "):
            with self.subTest(sequence=sequence):
                assert parse_hotkey(sequence) is None

    def test_canonical_lock_key_normalises_every_spelling(self):
        assert canonical_lock_key("ScrollLock") == "ScrollLock"
        assert canonical_lock_key(" scroll lock ") == "ScrollLock"
        assert canonical_lock_key("NumLock") == "NumLock"
        for sequence in ("Ctrl+Alt+D", "D", "", None, "CapsLock"):
            with self.subTest(sequence=sequence):
                assert canonical_lock_key(sequence) is None


# ---------------------------------------------------------------------------
# lock_key_is_on / set_lock_key: the LED is the switch
# ---------------------------------------------------------------------------

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
        # Lock-key state, keyed by virtual key, so the capture switches can be driven by a
        # "LED" the tests control. Off unless a test says otherwise.
        self.lock_state: dict[int, bool] = {}
        # Every synthesised press, so a test can tell one that moved the LED from one that
        # would have fought the OS.
        self.key_presses: list[int] = []
        self.GetKeyState = _Api(self._get_key_state)
        self.keybd_event = _Api(self._keybd_event)

    def _register(self, hwnd, hotkey_id, mods, vk):
        self.register_calls.append((hwnd, hotkey_id, mods, vk))
        return self.register_ok

    def _unregister(self, hwnd, hotkey_id):
        self.unregister_calls.append((hwnd, hotkey_id))
        return True

    def _get_key_state(self, vk):
        return 1 if self.lock_state.get(vk) else 0

    def _keybd_event(self, vk, scan, flags, extra):
        # One press is a down (EXTENDEDKEY) plus an up (+KEYEVENTF_KEYUP); only the down
        # flips the lock. Toggling on both would be a no-op, which is exactly the trap the
        # read-first in `press_lock_key` exists to avoid.
        if not (flags & 0x0002):
            self.key_presses.append(vk)
            self.lock_state[vk] = not self.lock_state.get(vk, False)


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

    def _deliver(self, registration, message, wparam, lparam=0):
        """Feed one Windows message to a registration's native filter, for real ctypes.

        The filter is what turns WM_HOTKEY into a Qt signal, so it is the ctypes boundary -
        exercising it with a real MSG keeps that honest. Built with the module's own `_MSG`
        rather than `ctypes.wintypes.MSG`: the point of the regression test below is that the
        submodule does not exist in the running app, and a helper that needs it would hide the
        very thing it is meant to check.
        """
        msg = hotkey_module._MSG()
        msg.message = message
        msg.wParam = wparam
        msg.lParam = lparam
        return registration._filter.nativeEventFilter(
            b"windows_generic_MSG", ctypes.addressof(msg)
        )

    @staticmethod
    def _remove_filters(registration):
        core = QCoreApplication.instance()
        if core is not None:
            # PySide6 exposes no way to enumerate installed native filters, and removing one
            # that was never installed is a no-op, so this is safe to call unconditionally.
            # Without it a leftover filter outlives the test and sees later tests' messages.
            core.removeNativeEventFilter(registration._filter)


class TestLockKeyState(HotkeyTestCase):
    def test_is_on_reads_the_low_bit_of_get_key_state(self):
        fake = self.use_fake_user32()
        fake.lock_state[VK_SCROLLLOCK] = True

        assert lock_key_is_on(VK_SCROLLLOCK) is True
        assert lock_key_is_on(VK_NUMLOCK) is False

    def test_setting_a_key_toggles_it_once(self):
        """One press is a down plus an up, and only the down flips the lock.

        Windows toggles the LED once per press, so a fake that flips on both events would be
        modelling a press that does nothing - which is the bug the read-first in set_lock_key
        exists to avoid, and this test is what keeps both honest.
        """
        fake = self.use_fake_user32()
        fake.lock_state[VK_SCROLLLOCK] = False

        assert set_lock_key(VK_SCROLLLOCK, True) is True
        assert lock_key_is_on(VK_SCROLLLOCK) is True

    def test_setting_a_key_that_already_matches_is_a_no_op(self):
        """The read-first is what stops the LED and the setting from fighting.

        Without it this would be a *toggle*, so driving Scroll Lock off while it is already
        off would turn it on - and the WM_HOTKEY that follows would then flip the capture back.
        """
        fake = self.use_fake_user32()
        fake.lock_state[VK_SCROLLLOCK] = True

        assert set_lock_key(VK_SCROLLLOCK, True) is True
        assert lock_key_is_on(VK_SCROLLLOCK) is True
        assert len(fake.register_calls) == 0 and fake.lock_state == {VK_SCROLLLOCK: True}

    def test_lock_key_state_reports_none_for_a_non_key(self):
        assert lock_key_state("ScrollLock") in (True, False)
        assert lock_key_state("Ctrl+Alt+D") is None

    def test_both_keys_can_be_read_independently(self):
        fake = self.use_fake_user32()
        fake.lock_state[VK_SCROLLLOCK] = True
        fake.lock_state[VK_NUMLOCK] = False

        assert lock_key_state("ScrollLock") is True
        assert lock_key_state("NumLock") is False


class TestHotkeyRegistration(HotkeyTestCase):
    def test_register_claims_the_key(self):
        fake = self.use_fake_user32()
        registration = self.make_registration()

        ok, message = registration.register("ScrollLock")

        assert ok, message
        # A lock key carries no modifier at all: fsModifiers 0 is the whole point.
        assert fake.register_calls == [(None, HOTKEY_ID, 0, VK_SCROLLLOCK)]
        assert registration.is_registered
        assert registration.sequence == "ScrollLock"
        assert registration.last_error == ""

    def test_already_registered_reports_the_conflict(self):
        self.use_fake_user32(register_ok=False)
        ctypes.set_last_error(ERROR_HOTKEY_ALREADY_REGISTERED)
        registration = self.make_registration()

        ok, message = registration.register("ScrollLock")

        assert not ok
        assert "already used by another application" in message
        assert not registration.is_registered
        assert registration.sequence == ""

    def test_other_failure_reports_the_error_code(self):
        self.use_fake_user32(register_ok=False)
        ctypes.set_last_error(5)
        registration = self.make_registration()

        ok, message = registration.register("NumLock")

        assert not ok
        assert "error 5" in message

    def test_rejects_an_unusable_key_without_calling_win32(self):
        fake = self.use_fake_user32()
        registration = self.make_registration()

        ok, message = registration.register("Ctrl+Alt+D")

        assert not ok
        # Saying which keys *are* usable is the fix for the complaint this used to earn: an
        # error that only says what is wrong leaves the user no better off.
        assert "Scroll Lock or Num Lock" in message
        assert fake.register_calls == []

    def test_blank_sequence_is_refused(self):
        fake = self.use_fake_user32()
        registration = self.make_registration()
        assert registration.register("")[0] is False
        assert fake.register_calls == []

    def test_registering_the_same_key_twice_is_a_no_op(self):
        # Re-registering would unregister first, opening a window in which the key belongs
        # to nobody; and if the second claim then failed, the hotkey would be gone entirely
        # rather than merely stale.
        fake = self.use_fake_user32()
        registration = self.make_registration()

        assert registration.register("ScrollLock")[0] is True
        assert registration.register("ScrollLock")[0] is True

        assert len(fake.register_calls) == 1
        assert fake.unregister_calls == []

    def test_unregister_is_idempotent(self):
        fake = self.use_fake_user32()
        registration = self.make_registration()
        registration.register("ScrollLock")

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

        registration.register("ScrollLock")
        registration.register("NumLock")

        # Leaving the first key claimed would make it unusable system-wide forever.
        assert fake.unregister_calls == [(None, HOTKEY_ID)]
        assert len(fake.register_calls) == 2
        assert registration.sequence == "NumLock"
        assert fake.register_calls[-1] == (None, HOTKEY_ID, 0, VK_NUMLOCK)
    def test_no_event_loop_means_no_registration(self):
        fake = self.use_fake_user32()
        registration = self.make_registration()
        patcher = patch.object(QCoreApplication, "instance", staticmethod(lambda: None))
        patcher.start()
        self.addCleanup(patcher.stop)

        ok, message = registration.register("ScrollLock")

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

    def test_the_filter_decodes_without_ctypes_wintypes_imported(self):
        """The app never imports ``ctypes.wintypes``; this test file does.

        That single import is what hid the bug. `hotkey` used ``ctypes.wintypes.MSG`` at
        *decode* time, which only exists once some module has imported the submodule — so in
        the running app every native message raised ``AttributeError`` and the filter returned
        ``(False, 0)``, which means no hotkey press was ever recognised. The key worked only
        after something else imported the submodule, which is why opening Preferences once
        "fixed" it.
        """
        self.use_fake_user32()
        registration = self.make_registration()
        registration.register("ScrollLock")
        fired = []
        registration.triggered.connect(lambda: fired.append("pressed"))

        # Remove the submodule from the module namespace, as an app that never imported it
        # would have it. The filter must still decode, because it resolves its types at
        # import time rather than at message time.
        had_wintypes = hasattr(ctypes, "wintypes")
        if had_wintypes:
            del ctypes.wintypes
        self.addCleanup(self._restore_ctypes_wintypes, had_wintypes)

        handled, _result = self._deliver(registration, WM_HOTKEY, HOTKEY_ID)

        assert handled is True, "the filter could not decode the message"
        assert fired == ["pressed"], "the press did not reach the binding"

    @staticmethod
    def _restore_ctypes_wintypes(had_it: bool):
        import ctypes.wintypes  # noqa: F401 - restores the submodule attribute

        if not had_it:
            del ctypes.wintypes


# ---------------------------------------------------------------------------
# HotkeyManager: several bindings, one id each
# ---------------------------------------------------------------------------

class TestHotkeyManager(HotkeyTestCase):
    def setUp(self):
        super().setUp()
        # Installed for the whole class, not per test: touching the real user32 here would
        # claim a chord the developer's own running My-IDM instance may already own, and a
        # registration that fails with 1409 for that reason is indistinguishable from a
        # genuine bug in the manager.
        self.use_fake_user32()

    def make_manager(self) -> HotkeyManager:
        manager = HotkeyManager()
        self.addCleanup(manager.unregister_all)
        self.addCleanup(self._remove_manager_filters, manager)
        return manager

    @staticmethod
    def _remove_manager_filters(manager: HotkeyManager):
        core = QCoreApplication.instance()
        if core is None:
            return
        for registration in manager._registrations.values():
            # `_filter` exists on every HotkeyRegistration; removing a filter that was never
            # installed is a no-op, so this is safe to call unconditionally.
            core.removeNativeEventFilter(registration._filter)

    def test_each_binding_gets_its_own_id(self):
        """The id is what comes back in WM_HOTKEY, so two bindings cannot share one."""
        manager = self.make_manager()

        capture = manager.registration(BINDING_CAPTURE)
        clipboard = manager.registration(BINDING_CLIPBOARD)
        assert capture._hotkey_id == HOTKEY_ID
        assert clipboard._hotkey_id == CLIPBOARD_HOTKEY_ID
        assert capture._hotkey_id != clipboard._hotkey_id

    def test_registering_one_binding_leaves_the_other_alone(self):
        fake = self.use_fake_user32()
        manager = self.make_manager()

        manager.register(BINDING_CAPTURE, "ScrollLock")

        assert manager.registration(BINDING_CAPTURE).is_registered is True
        assert manager.registration(BINDING_CLIPBOARD).is_registered is False
        assert fake.register_calls == [(None, HOTKEY_ID, 0, VK_SCROLLLOCK)]

    def test_both_bindings_can_be_live_at_once(self):
        fake = self.use_fake_user32()
        manager = self.make_manager()

        manager.register(BINDING_CAPTURE, "NumLock")
        manager.register(BINDING_CLIPBOARD, "ScrollLock")

        assert manager.is_any_registered is True
        ids = {call[1] for call in fake.register_calls}
        assert ids == {HOTKEY_ID, CLIPBOARD_HOTKEY_ID}
        # Each key registers with no modifier at all: fsModifiers 0.
        assert (None, HOTKEY_ID, 0, VK_NUMLOCK) in fake.register_calls
        assert (None, CLIPBOARD_HOTKEY_ID, 0, VK_SCROLLLOCK) in fake.register_calls

    def test_a_press_names_the_binding_that_was_pressed(self):
        """`triggered` carries the name, which is the point of having two bindings."""
        self.use_fake_user32()
        manager = self.make_manager()
        manager.register(BINDING_CAPTURE, "NumLock")
        manager.register(BINDING_CLIPBOARD, "ScrollLock")

        fired = []
        manager.triggered.connect(fired.append)

        capture = manager.registration(BINDING_CAPTURE)
        clipboard = manager.registration(BINDING_CLIPBOARD)
        self._deliver(capture, WM_HOTKEY, HOTKEY_ID)
        assert fired == [BINDING_CAPTURE]
        self._deliver(clipboard, WM_HOTKEY, CLIPBOARD_HOTKEY_ID)
        assert fired == [BINDING_CAPTURE, BINDING_CLIPBOARD]

    def test_a_message_for_one_id_does_not_fire_the_other(self):
        self.use_fake_user32()
        manager = self.make_manager()
        manager.register(BINDING_CAPTURE, "NumLock")
        manager.register(BINDING_CLIPBOARD, "ScrollLock")

        fired = []
        manager.triggered.connect(fired.append)
        clipboard = manager.registration(BINDING_CLIPBOARD)

        # The capture binding's own id arrives on the clipboard binding's filter: ignored.
        handled, _result = self._deliver(clipboard, WM_HOTKEY, HOTKEY_ID)

        assert handled is False
        assert fired == []

    def test_unregister_all_releases_every_binding(self):
        fake = self.use_fake_user32()
        manager = self.make_manager()
        manager.register(BINDING_CAPTURE, "ScrollLock")
        manager.register(BINDING_CLIPBOARD, "NumLock")

        manager.unregister_all()

        assert manager.is_any_registered is False
        assert sorted(call[1] for call in fake.unregister_calls) == sorted(
            [HOTKEY_ID, CLIPBOARD_HOTKEY_ID]
        )

    def test_an_unknown_binding_raises_rather_than_returning_the_wrong_one(self):
        manager = self.make_manager()
        for call in (
            lambda: manager.registration("nope"),
            lambda: manager.register("nope", "ScrollLock"),
            lambda: manager.unregister("nope"),
        ):
            with self.subTest(call=call):
                with pytest.raises(KeyError):
                    call()

    # -- one key, two purposes -----------------------------------------------

    def test_the_same_key_can_be_bound_to_both_captures(self):
        """Win32 refuses a second RegisterHotKey of an identical key for a thread.

        That is 1409, which the old path reported as "already used by another application".
        The user's own setting — the same key on both rows — was the cause, so the key is
        now registered once and dispatched to both purposes.
        """
        fake = self.use_fake_user32()
        manager = self.make_manager()

        ok_capture, message_capture = manager.register(BINDING_CAPTURE, "ScrollLock")
        ok_clipboard, message_clipboard = manager.register(BINDING_CLIPBOARD, "ScrollLock")

        self.assertTrue(ok_capture)
        self.assertTrue(ok_clipboard)
        self.assertIn("drives both", message_clipboard)
        self.assertEqual(len(fake.register_calls), 1, "the key was claimed twice")
        self.assertEqual(fake.register_calls[0][2:], (0, VK_SCROLLLOCK))
        self.assertTrue(manager.is_binding_registered(BINDING_CAPTURE))
        self.assertTrue(manager.is_binding_registered(BINDING_CLIPBOARD))

    def test_spelling_differences_do_not_make_a_second_key(self):
        """"ScrollLock" and "scroll lock" — one key, not two."""
        fake = self.use_fake_user32()
        manager = self.make_manager()

        manager.register(BINDING_CAPTURE, "ScrollLock")
        _ok, message = manager.register(BINDING_CLIPBOARD, "scroll lock")

        self.assertIn("drives both", message)
        self.assertEqual(len(fake.register_calls), 1)

    def test_a_shared_key_drives_both_captures(self):
        self.use_fake_user32()
        manager = self.make_manager()
        manager.register(BINDING_CAPTURE, "ScrollLock")
        manager.register(BINDING_CLIPBOARD, "ScrollLock")
        fired = []
        manager.triggered.connect(fired.append)

        manager._on_binding_triggered(BINDING_CAPTURE)

        self.assertEqual(sorted(fired), sorted([BINDING_CAPTURE, BINDING_CLIPBOARD]))

    def test_bindings_for_reports_the_whole_group(self):
        manager = self.make_manager()
        manager.register(BINDING_CAPTURE, "ScrollLock")
        manager.register(BINDING_CLIPBOARD, "ScrollLock")

        self.assertEqual(
            manager.bindings_for("ScrollLock"), {BINDING_CAPTURE, BINDING_CLIPBOARD}
        )
        self.assertEqual(manager.bindings_for("NumLock"), set())
        self.assertEqual(manager.bindings_for(""), set())

    def test_disabling_one_of_two_sharers_keeps_the_other_live(self):
        """The chord must be handed over, not released with the binding that owned it."""
        fake = self.use_fake_user32()
        manager = self.make_manager()
        manager.register(BINDING_CAPTURE, "ScrollLock")
        manager.register(BINDING_CLIPBOARD, "ScrollLock")

        manager.unregister(BINDING_CAPTURE)

        self.assertFalse(manager.is_binding_registered(BINDING_CAPTURE))
        self.assertTrue(
            manager.is_binding_registered(BINDING_CLIPBOARD),
            "turning off one of two captures sharing a key killed the other's hotkey",
        )
        # Still exactly one claim, now under the surviving binding's id.
        self.assertEqual(len(fake.register_calls), 2)
        self.assertEqual(
            (fake.register_calls[-1][1], fake.register_calls[-1][2:]),
            (CLIPBOARD_HOTKEY_ID, (0, VK_SCROLLLOCK)),
        )

        manager.unregister(BINDING_CLIPBOARD)
        self.assertFalse(manager.is_any_registered)
        self.assertEqual(fake.unregister_calls[-1], (None, CLIPBOARD_HOTKEY_ID))

    def test_two_different_keys_still_get_two_registrations(self):
        fake = self.use_fake_user32()
        manager = self.make_manager()
        manager.register(BINDING_CAPTURE, "NumLock")
        manager.register(BINDING_CLIPBOARD, "ScrollLock")

        self.assertEqual(
            {call[1]: call[2:] for call in fake.register_calls},
            {HOTKEY_ID: (0, VK_NUMLOCK), CLIPBOARD_HOTKEY_ID: (0, VK_SCROLLLOCK)},
        )
        self.assertEqual(manager.bindings_for("NumLock"), {BINDING_CAPTURE})


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
        self._fake_user32 = _FakeUser32(register_ok=register_ok)
        hotkey_module._USER32 = self._fake_user32
        return self._fake_user32

    def patch_notifications(self, seen: list):
        """Collect what would go to a Windows toast instead of showing a real one.

        Patched at the module's ``show_notification`` rather than on a helper, so every
        notification route in the module is captured — including the tray dispatcher's.
        """
        from my_idm import notifications as notifications_module

        def _record(title, message, duration=5, icon_path=None):
            seen.append((title, message, duration))
            return True

        patcher = patch.object(notifications_module, "show_notification", _record)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_both_capture_objects_exist_after_construction(self):
        assert isinstance(self.win._clipboard_monitor, ClipboardMonitor)
        assert isinstance(self.win._hotkey, HotkeyManager)

    def test_capture_stays_off_by_default(self):
        assert self.win._clipboard_monitor.is_active is False
        assert self.win._hotkey.registration(BINDING_CAPTURE).is_registered is False
        assert self.win._hotkey.registration(BINDING_CLIPBOARD).is_registered is False

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
        cfg.capture_hotkey_sequence = "NumLock"
        self.manager.general_config_changed.emit(cfg)

        binding = self.win._hotkey.registration(BINDING_CAPTURE)
        assert binding.is_registered is True
        assert binding.sequence == "NumLock"
        assert (None, HOTKEY_ID, 0, VK_NUMLOCK) in fake.register_calls

        cfg.capture_hotkey_enabled = False
        self.manager.general_config_changed.emit(cfg)
        assert binding.is_registered is False
        assert (None, HOTKEY_ID) in fake.unregister_calls

    def test_the_clipboard_binding_is_registered_and_used_separately(self):
        """Two bindings, two ids: the hotkey layer tells them apart, so the app can too."""
        fake = self.use_fake_user32()
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        cfg.clipboard_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "NumLock"
        cfg.clipboard_hotkey_sequence = "ScrollLock"
        self.manager.general_config_changed.emit(cfg)

        capture = self.win._hotkey.registration(BINDING_CAPTURE)
        clipboard = self.win._hotkey.registration(BINDING_CLIPBOARD)
        assert capture.is_registered and clipboard.is_registered
        # Both are bare keys, so both register with fsModifiers 0.
        assert (None, HOTKEY_ID, 0, VK_NUMLOCK) in fake.register_calls
        assert (None, CLIPBOARD_HOTKEY_ID, 0, VK_SCROLLLOCK) in fake.register_calls

    def _press(self, vk: int, *bindings: str) -> None:
        """Press the lock key with virtual key *vk* the way Windows does.

        A real press moves the key's toggle bit — on the key **up**, which is *after* the
        `WM_HOTKEY` the handler is running in. Modelling that split is the point: the old
        read-the-LED handler saw the pre-press state and only worked on alternate presses, so a
        test that sets the LED before calling the handler is exactly the case that hid it.

        Bindings are dispatched in the order the manager emits them, so a shared key reaches the
        handler the same way a real press does.
        """
        fake = self._fake_user32
        fake.lock_state[vk] = not fake.lock_state.get(vk, False)
        for binding in sorted(bindings):
            self.win._on_capture_hotkey_triggered(binding)

    def test_a_press_switches_the_capture(self):
        """The press is what switches, and the LED is Windows' to move.

        Reading `GetKeyState` in the handler used to be the design, and it is why a key press
        sometimes did nothing: Windows delivers `WM_HOTKEY` on the key down and flips a lock
        key's toggle bit on the key up, so the handler could only ever see the *previous*
        press's state.
        """
        fake = self.use_fake_user32()
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "ScrollLock"
        cfg.clipboard_monitor_enabled = False
        fake.lock_state[VK_SCROLLLOCK] = False
        # Start from a known state: the press below is the first thing to change it.
        self.manager.set_capture_enabled(False)
        assert self.manager.browser_config.intercept_all is False
        # The toast: this switch happens with the window closed, so the key's LED is the only
        # sign otherwise - and the LED says nothing about *which capture* moved.
        shown = []
        self.patch_notifications(shown)

        self._press(VK_SCROLLLOCK, BINDING_CAPTURE)

        assert self.manager.browser_config.intercept_all is True
        assert fake.lock_state[VK_SCROLLLOCK] is True, "Windows moved the LED"
        assert [(title, message) for title, message, _d in shown] == [
            ("Capture On", "Download capture is now on — Scroll Lock is on too")
        ]

        self._press(VK_SCROLLLOCK, BINDING_CAPTURE)

        assert self.manager.browser_config.intercept_all is False
        assert fake.lock_state[VK_SCROLLLOCK] is False

    def test_every_press_always_switches(self):
        """The failure this replaced: alternate presses doing nothing.

        The LED below is deliberately left stale, as it is in a real handler, so nothing here
        depends on a read being fresh.
        """
        fake = self.use_fake_user32()
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "ScrollLock"
        cfg.clipboard_monitor_enabled = False
        self.manager.set_capture_enabled(False)
        shown = []
        self.patch_notifications(shown)

        for expected in (True, False, True, False, True):
            fake.lock_state[VK_SCROLLLOCK] = not expected  # always the old state
            self.win._on_capture_hotkey_triggered(BINDING_CAPTURE)
            with self.subTest(expected=expected):
                assert self.manager.browser_config.intercept_all is expected
        # One toast per real change, and no more: a press that lands on the state the capture
        # is already in has nothing to say.
        assert len(shown) == 5
        assert [title for title, _m, _d in shown] == [
            "Capture On", "Capture Off", "Capture On", "Capture Off", "Capture On"
        ]

    def test_each_binding_controls_its_own_capture(self):
        fake = self.use_fake_user32()
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        cfg.clipboard_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "NumLock"
        cfg.clipboard_hotkey_sequence = "ScrollLock"
        self.manager.clipboard_monitor_enabled = False
        self.manager.general_config_changed.emit(cfg)
        fake.lock_state[VK_NUMLOCK] = False
        fake.lock_state[VK_SCROLLLOCK] = False
        shown = []
        self.patch_notifications(shown)

        # Scroll Lock is on, so clipboard capture switches on.
        self._press(VK_SCROLLLOCK, BINDING_CLIPBOARD)
        assert cfg.clipboard_monitor_enabled is True
        assert self.manager.browser_config.intercept_all is False

        # ...and off, so it switches back off.
        self._press(VK_SCROLLLOCK, BINDING_CLIPBOARD)
        assert cfg.clipboard_monitor_enabled is False

        # Num Lock, meanwhile, never moved the clipboard capture at all.
        self._press(VK_NUMLOCK, BINDING_CAPTURE)
        assert self.manager.browser_config.intercept_all is True
        assert cfg.clipboard_monitor_enabled is False
        # One toast per capture that moved, each naming its own key.
        assert [message for _title, message, _d in shown] == [
            "Clipboard capture is now on — Scroll Lock is on too",
            "Clipboard capture is now off — Scroll Lock is off too",
            "Download capture is now on — Num Lock is on too",
        ]

    def test_one_key_assigned_to_both_rows_controls_both(self):
        """The setting a user actually makes: the same key on both rows.

        Only browser capture used to react, because the second registration failed with 1409
        and reported a phantom "another application". A shared key is registered once and
        drives both captures, which is the only honest reading of that setting.
        """
        fake = self.use_fake_user32()
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        cfg.clipboard_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "ScrollLock"
        cfg.clipboard_hotkey_sequence = "ScrollLock"
        self.manager.general_config_changed.emit(cfg)

        hotkeys = self.win._hotkey
        assert hotkeys.is_binding_registered(BINDING_CAPTURE)
        assert hotkeys.is_binding_registered(BINDING_CLIPBOARD)
        assert len(fake.register_calls) == 1, "one key, one claim"
        # The startup sync ran: the LED is off and the captures were off with it.
        assert self.manager.browser_config.intercept_all is False
        shown = []
        self.patch_notifications(shown)

        self._press(VK_SCROLLLOCK, BINDING_CLIPBOARD, BINDING_CAPTURE)
        assert self.manager.browser_config.intercept_all is True
        assert cfg.clipboard_monitor_enabled is True
        # One LED can be driving two settings, so each says what moved.
        assert [message for _t, message, _d in shown] == [
            "Download capture is now on — Scroll Lock is on too",
            "Clipboard capture is now on — Scroll Lock is on too",
        ]

        self._press(VK_SCROLLLOCK, BINDING_CLIPBOARD, BINDING_CAPTURE)
        assert self.manager.browser_config.intercept_all is False
        assert cfg.clipboard_monitor_enabled is False

    def test_a_shared_key_says_nothing_in_the_status_line(self):
        """The status line is for what the user is doing, not for describing a setting.

        Nothing overwrites it at startup, so a shared key would sit there for the whole
        session — and the tray row names the key on each capture already.
        """
        fake = self.use_fake_user32()
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        cfg.clipboard_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "ScrollLock"
        cfg.clipboard_hotkey_sequence = "ScrollLock"
        self.manager.general_config_changed.emit(cfg)

        assert "drives both" not in self.win._status_label.text()
        capture, clipboard = self._tray_rows()
        # The key is still named on both rows, which is where it belongs.
        assert capture.text() == "🎯 Download Capture    Scroll Lock"
        assert clipboard.text() == "📋 Clipboard Capture    Scroll Lock"

    def test_a_press_does_not_write_the_key_back(self):
        """Windows moved the LED on the press; mirroring now would move it again.

        Reading the LED in the handler gets the pre-press state, so a mirror there would
        synthesise a second press and leave the light where it started - the switch would be
        off while the capture is on.
        """
        fake = self.use_fake_user32()
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "ScrollLock"
        self.manager.general_config_changed.emit(cfg)
        # Started off, so the press above turns the capture on and Windows' LED with it.
        fake.lock_state[VK_SCROLLLOCK] = False

        self._press(VK_SCROLLLOCK, BINDING_CAPTURE)

        assert self.manager.browser_config.intercept_all is True
        assert fake.lock_state[VK_SCROLLLOCK] is True
        # No synthesised press: mirroring on a key press would move the LED a second time.
        assert fake.key_presses == []
        assert self.win._ignore_key_presses_until <= time.monotonic()

    def test_the_key_follows_a_capture_changed_from_the_tray(self):
        """A key whose LED lies is worse than no key, so the LED is driven to match."""
        fake = self.use_fake_user32()
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        cfg.clipboard_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "ScrollLock"
        cfg.clipboard_hotkey_sequence = "ScrollLock"
        # LED on *before* the save, so the sync puts the captures in step with it — which is
        # also what makes the switch below a change worth announcing.
        fake.lock_state[VK_SCROLLLOCK] = True
        self.manager.general_config_changed.emit(cfg)
        assert self.manager.browser_config.intercept_all is True
        shown = []
        self.patch_notifications(shown)

        self.win._on_capture_toggled(False)

        assert self.manager.browser_config.intercept_all is False
        assert cfg.clipboard_monitor_enabled is False
        assert fake.lock_state[VK_SCROLLLOCK] is False
        # Same toast as a key press, because the state moved the same way.
        assert [title for title, _m, _d in shown] == ["Capture Off"]

    def test_a_changed_capture_is_not_snapped_back_by_the_key(self):
        """Mirroring first is load-bearing: the change re-emits, and the sync reads the key.

        Mirroring *after* the setting changed would be overwritten by the LED that had not
        moved yet, so the capture would snap straight back to what it was — the tray row would
        move and the setting would not.
        """
        fake = self.use_fake_user32()
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "ScrollLock"
        self.manager.general_config_changed.emit(cfg)
        fake.lock_state[VK_SCROLLLOCK] = True

        self.win._on_capture_toggled(False)

        assert fake.lock_state[VK_SCROLLLOCK] is False
        assert self.manager.browser_config.intercept_all is False

    def test_the_echo_of_our_own_key_press_is_ignored(self):
        """Synthetic input triggers registered hotkeys, so our own press comes back.

        The tray writes the LED by pressing the key, and that press fires our own chord. A
        handler that switched on every press would switch straight back, undoing the tray
        action the user just took.
        """
        fake = self.use_fake_user32()
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "ScrollLock"
        self.manager.general_config_changed.emit(cfg)
        fake.lock_state[VK_SCROLLLOCK] = True

        self.win._on_capture_toggled(False)
        assert fake.key_presses == [VK_SCROLLLOCK], "the LED was moved by a real press here"
        assert self.manager.browser_config.intercept_all is False

        # The synthetic press arrives back as our own hotkey, shortly after.
        fake.lock_state[VK_SCROLLLOCK] = True
        self.win._on_capture_hotkey_triggered(BINDING_CAPTURE)

        assert self.manager.browser_config.intercept_all is False
        assert fake.key_presses == [VK_SCROLLLOCK]

    def test_a_real_press_still_works_after_the_echo(self):
        """The echo window is short, so the next genuine press is not swallowed."""
        fake = self.use_fake_user32()
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "ScrollLock"
        self.manager.general_config_changed.emit(cfg)
        fake.lock_state[VK_SCROLLLOCK] = True

        self.win._on_capture_toggled(False)
        assert self.win._ignore_key_presses_until > time.monotonic()

        # The window is 250 ms, well under a human's next press.
        self.win._ignore_key_presses_until = time.monotonic() - 1
        self._press(VK_SCROLLLOCK, BINDING_CAPTURE)

        assert self.manager.browser_config.intercept_all is True
        assert fake.lock_state[VK_SCROLLLOCK] is True

    def test_a_key_really_owned_by_another_app_still_reports_it(self):
        """The 1409 path must not be dead now that same-key collisions are handled."""
        fake = self.use_fake_user32(register_ok=False)
        ctypes.set_last_error(ERROR_HOTKEY_ALREADY_REGISTERED)
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        cfg.clipboard_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "ScrollLock"
        cfg.clipboard_hotkey_sequence = "NumLock"
        self.manager.general_config_changed.emit(cfg)

        hotkeys = self.win._hotkey
        assert hotkeys.is_binding_registered(BINDING_CAPTURE) is False
        assert hotkeys.is_binding_registered(BINDING_CLIPBOARD) is False
        assert hotkeys.is_any_registered is False
        # Two bindings, two keys, both refused — the shared-key path never ran.
        assert len(fake.register_calls) == 2
        assert "already used by another application" in self.win._status_label.text()

    def test_a_key_left_on_makes_the_capture_on_at_startup(self):
        """The LED is the switch, so a LED left on from a previous session is on.

        Silently: the tray row and the menu checkmark already state the result, and a toast on
        every launch would train the user to ignore toasts.
        """
        fake = self.use_fake_user32()
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "ScrollLock"
        cfg.clipboard_monitor_enabled = False
        fake.lock_state[VK_SCROLLLOCK] = True
        shown = []
        self.patch_notifications(shown)
        self.manager.general_config_changed.emit(cfg)

        assert self.manager.browser_config.intercept_all is True
        assert shown == []

    def test_a_key_left_off_leaves_the_capture_off(self):
        fake = self.use_fake_user32()
        cfg = self.manager.general_config
        cfg.clipboard_hotkey_enabled = True
        cfg.clipboard_hotkey_sequence = "NumLock"
        cfg.clipboard_monitor_enabled = True
        fake.lock_state[VK_NUMLOCK] = False
        self.manager.general_config_changed.emit(cfg)

        assert cfg.clipboard_monitor_enabled is False
        assert self.win._clipboard_monitor.is_active is False

    # -- a capture switch is announced ---------------------------------------

    # The notification is asserted inside the tests that own each switch (the key press, the
    # tray, the startup sync), not in tests of its own. Each `MainWindow` in this suite costs a
    # tray icon, timers and a clipboard thread, and six extra of them abort the whole basic
    # tier in `test_checksum.py` - the toast is part of switching, so it belongs with switching.

    # -- the tray rows show the hotkey ---------------------------------------

    def _tray_rows(self):
        capture = getattr(self.win, "_tray_act_capture", None)
        clipboard = getattr(self.win, "_tray_act_clipboard", None)
        if capture is None or clipboard is None:
            self.skipTest("no system tray on this desktop")
        return capture, clipboard

    def test_the_tray_rows_show_their_hotkeys(self):
        self.use_fake_user32()
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "NumLock"
        cfg.clipboard_hotkey_enabled = True
        cfg.clipboard_hotkey_sequence = "ScrollLock"
        self.manager.general_config_changed.emit(cfg)

        capture, clipboard = self._tray_rows()

        assert capture.text() == "🎯 Download Capture    Num Lock"
        assert clipboard.text() == "📋 Clipboard Capture    Scroll Lock"

    def test_an_unconfigured_hotkey_is_not_shown(self):
        """A user who never wanted a global key must not be shown one for one capture."""
        self.use_fake_user32()
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "ScrollLock"
        cfg.clipboard_hotkey_enabled = False
        cfg.clipboard_hotkey_sequence = "NumLock"
        self.manager.general_config_changed.emit(cfg)

        capture, clipboard = self._tray_rows()

        assert capture.text().endswith("Scroll Lock")
        assert clipboard.text() == "📋 Clipboard Capture"

    def test_a_hotkey_that_failed_to_register_is_shown_as_broken(self):
        """Listing a dead key as if it lived is worse than listing nothing... so it is marked."""
        fake = self.use_fake_user32(register_ok=False)
        ctypes.set_last_error(ERROR_HOTKEY_ALREADY_REGISTERED)
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "ScrollLock"
        cfg.clipboard_hotkey_enabled = False
        self.manager.general_config_changed.emit(cfg)

        capture, clipboard = self._tray_rows()

        assert capture.text() == "🎯 Download Capture    ⚠ Scroll Lock"
        assert clipboard.text() == "📋 Clipboard Capture"
        assert fake.register_calls

    def test_the_tray_rows_stay_in_step_when_the_hotkey_repeats(self):
        """One key on both rows shows it on both, rather than hiding the second capture."""
        self.use_fake_user32()
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        cfg.clipboard_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "ScrollLock"
        cfg.clipboard_hotkey_sequence = "ScrollLock"
        self.manager.general_config_changed.emit(cfg)

        capture, clipboard = self._tray_rows()

        assert capture.text() == "🎯 Download Capture    Scroll Lock"
        assert clipboard.text() == "📋 Clipboard Capture    Scroll Lock"

    def test_the_labels_are_rebuilt_not_appended_to(self):
        """A sync must not grow the row: the suffix belongs to a fresh label each time."""
        self.use_fake_user32()
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        for sequence in ("ScrollLock", "ScrollLock", "NumLock"):
            cfg.capture_hotkey_sequence = sequence
            self.manager.general_config_changed.emit(cfg)

        capture, clipboard = self._tray_rows()

        assert capture.text() == "🎯 Download Capture    Num Lock"
        assert capture.text().count("Capture") == 1

    def test_an_unknown_binding_is_ignored_rather_than_raising(self):
        # The signal cannot carry an unknown name, but an exception escaping a signal slot
        # breaks that connection in Qt - and a broken hotkey connection is silent.
        self.use_fake_user32()
        before_capture = self.manager.browser_config.intercept_all
        before_clipboard = self.manager.general_config.clipboard_monitor_enabled

        self.win._on_capture_hotkey_triggered("nonsense")

        assert self.manager.browser_config.intercept_all is before_capture
        assert self.manager.general_config.clipboard_monitor_enabled is before_clipboard

    def test_a_chord_owned_elsewhere_is_reported_in_the_status_line(self):
        self.use_fake_user32(register_ok=False)
        ctypes.set_last_error(ERROR_HOTKEY_ALREADY_REGISTERED)
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        self.manager.general_config_changed.emit(cfg)

        assert self.win._hotkey.registration(BINDING_CAPTURE).is_registered is False
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

    def test_closing_releases_the_hotkeys(self):
        # A hotkey left registered outlives the process: Windows keeps the chord claimed and
        # the next launch cannot take it, with no way for the user to tell why.
        fake = self.use_fake_user32()
        cfg = self.manager.general_config
        cfg.capture_hotkey_enabled = True
        cfg.clipboard_hotkey_enabled = True
        self.manager.general_config_changed.emit(cfg)
        capture = self.win._hotkey.registration(BINDING_CAPTURE)
        clipboard = self.win._hotkey.registration(BINDING_CLIPBOARD)
        # The manager's accessor, not the registration's: a sharer owns no Win32
        # registration of its own, and that is the whole point of it.
        assert self.win._hotkey.is_binding_registered(BINDING_CAPTURE)
        assert self.win._hotkey.is_binding_registered(BINDING_CLIPBOARD)

        self.win._force_exit = True
        self.win.close()

        # The owner's id is released; the sharer never had one to release, so asking Win32
        # for it again would be a second unregister of nothing.
        assert (None, HOTKEY_ID) in fake.unregister_calls
        assert fake.unregister_calls == [(None, HOTKEY_ID)]
        assert self.win._hotkey.is_any_registered is False
        assert capture.is_registered is False
        assert clipboard.is_registered is False
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
        assert cfg.clipboard_hotkey_enabled is False
        # Scroll Lock is the default for both: it is a lock key nothing types with, so it is
        # the one kind of bare key the hotkey layer will claim system-wide.
        assert cfg.capture_hotkey_sequence == "ScrollLock"
        assert cfg.clipboard_hotkey_sequence == "ScrollLock"
        assert cfg.clipboard_monitor_max_urls == 20

    def test_round_trips_through_qsettings(self):
        cfg = GeneralConfig()
        cfg.clipboard_monitor_enabled = True
        cfg.clipboard_monitor_max_urls = 7
        cfg.capture_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "NumLock"
        cfg.clipboard_hotkey_enabled = True
        cfg.clipboard_hotkey_sequence = "ScrollLock"
        cfg.save()

        loaded = GeneralConfig.load()

        assert loaded.clipboard_monitor_enabled is True
        assert loaded.clipboard_monitor_max_urls == 7
        assert loaded.capture_hotkey_enabled is True
        assert loaded.capture_hotkey_sequence == "NumLock"
        assert loaded.clipboard_hotkey_enabled is True
        assert loaded.clipboard_hotkey_sequence == "ScrollLock"

    def test_round_trips_through_a_dict(self):
        cfg = GeneralConfig()
        cfg.clipboard_monitor_enabled = True
        cfg.capture_hotkey_enabled = True
        cfg.capture_hotkey_sequence = "NumLock"
        cfg.clipboard_hotkey_enabled = True
        cfg.clipboard_hotkey_sequence = "ScrollLock"
        cfg.clipboard_monitor_max_urls = 3

        rebuilt = GeneralConfig.from_dict(cfg.to_dict())

        assert rebuilt.clipboard_monitor_enabled is True
        assert rebuilt.capture_hotkey_enabled is True
        assert rebuilt.capture_hotkey_sequence == "NumLock"
        assert rebuilt.clipboard_hotkey_enabled is True
        assert rebuilt.clipboard_hotkey_sequence == "ScrollLock"
        assert rebuilt.clipboard_monitor_max_urls == 3

    def test_a_zero_or_missing_limit_is_clamped(self):
        assert GeneralConfig.from_dict({"clipboard_monitor_max_urls": 0}
                                       ).clipboard_monitor_max_urls == 1
        assert GeneralConfig.from_dict({}).clipboard_monitor_max_urls == 20

    def test_an_unusable_sequence_falls_back_to_the_default(self):
        """A key that is neither of the two would never register, so it is snapped.

        Refusing instead would leave the capture bound to a key that does nothing, which is
        indistinguishable from a hotkey that stopped working.
        """
        for bad in ("", "   ", "Ctrl+Alt+D", "CapsLock", None, 7):
            with self.subTest(bad=bad):
                cfg = GeneralConfig.from_dict(
                    {"capture_hotkey_sequence": bad, "clipboard_hotkey_sequence": bad}
                )
                assert cfg.capture_hotkey_sequence == "ScrollLock", bad
                assert cfg.clipboard_hotkey_sequence == "ScrollLock", bad

    def test_a_stored_sequence_is_canonicalised(self):
        cfg = GeneralConfig.from_dict({"capture_hotkey_sequence": "scroll lock"})
        assert cfg.capture_hotkey_sequence == "ScrollLock"

    def test_from_dict_coerces_junk(self):
        cfg = GeneralConfig.from_dict(
            {
                "clipboard_monitor_enabled": 1,
                "capture_hotkey_enabled": 0,
                "clipboard_hotkey_enabled": 1,
                # Not either of the two keys, so it snaps to the default rather than staying
                # a value the hotkey layer would refuse to register.
                "clipboard_hotkey_sequence": 7,
                "clipboard_monitor_max_urls": "5",
            }
        )
        assert cfg.clipboard_monitor_enabled is True
        assert cfg.capture_hotkey_enabled is False
        assert cfg.clipboard_hotkey_enabled is True
        assert cfg.clipboard_hotkey_sequence == "ScrollLock"
        assert cfg.clipboard_monitor_max_urls == 5