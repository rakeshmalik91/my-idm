"""Tests for the browser-capture boundary: URL schemes and the minimum-size gate.

Both bugs tracked in ``docs/todo/2026-09-30.md`` came out of the same event, and the
evidence is in the user's own log and database:

    blob:https://github.com/4719d4f4-...   status=queued  size=0
    "HEAD request failed for blob:https://github.com/..."
    "Single-stream attempt 5 failed: blob:https://github.com/... - retrying in 60.0s"

A ``blob:`` URL is a browser-internal object reference. There is no HTTP transport for one,
so it can never be downloaded by an external app - but the Chromium interception engine had
no scheme guard, so it was queued and then ground through the whole retry ladder
(5s, 10s, 20s, 40s, 60s) before failing. The Firefox engine already had the guard, so the
identical page was skipped on Firefox and captured on Chrome.

The same event explains the "10 MB minimum captured a smaller file" report: a ``blob:`` URL
reports no size at all, and every layer's minimum check is guarded by ``size > 0``, so an
unsizeable URL walked straight past a configured threshold.

These tests pin the fixes at all three layers - the extension, the server, and the engine -
because the extension can be stale relative to the app (users load an unpacked copy and it
does not auto-update), so a server-side check is not redundant.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from PySide6.QtWidgets import QApplication

from my_idm import browser_server as bs_module
from my_idm.browser_server import CAPTURABLE_SCHEMES, is_capturable_url
from my_idm.config import BrowserIntegrationConfig, GeneralConfig
from my_idm.database import Database, DownloadEntry
from my_idm.http_engine import HTTPEngine
from my_idm.settings_dialog import SettingsDialog
from tests.fake_http import FakeResponse, FakeSession, run_async

app = QApplication.instance() or QApplication(sys.argv)

BLOB_URL = "blob:https://github.com/4719d4f4-cf3a-4e03-9445-0b63003778bb"


class TestIsCapturableUrl(unittest.TestCase):
    """The scheme allow-list. Everything here is a pure string test."""

    def test_http_and_https_and_magnet_are_capturable(self):
        for url in (
            "https://example.com/a.zip",
            "http://example.com/a.zip",
            "magnet:?xt=urn:btih:da39a3ee",
        ):
            with self.subTest(url=url):
                self.assertTrue(is_capturable_url(url))

    def test_blob_and_data_are_not_capturable(self):
        """The bug: a blob has no HTTP representation, so it can never be fetched."""
        for url in (
            BLOB_URL,
            "blob:https://github.com/4719d4f4-cf3a-4e03-9445-0b63003778bb",
            "data:text/plain;base64,SGVsbG8=",
            "data:application/octet-stream,abc",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_capturable_url(url), url)

    def test_other_browser_internal_schemes_are_not_capturable(self):
        for url in (
            "file:///C:/Windows/System32/cmd.exe",
            "javascript:void(0)",
            "about:blank",
            "chrome-extension://abcdef/page.html",
            "moz-extension://abcdef/page.html",
            "ftp://example.com/a.zip",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_capturable_url(url), url)

    def test_the_scheme_comparison_is_case_insensitive(self):
        """A browser will happily hand us ``BLOB:`` and ``HTTPS://``."""
        self.assertFalse(is_capturable_url("BLOB:https://github.com/x"))
        self.assertTrue(is_capturable_url("HTTPS://example.com/a.zip"))
        self.assertTrue(is_capturable_url("HtTpS://example.com/a.zip"))

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertTrue(is_capturable_url("  https://example.com/a.zip  "))
        self.assertFalse(is_capturable_url("  blob:https://x/y  "))

    def test_empty_and_non_string_input_is_rejected(self):
        self.assertFalse(is_capturable_url(""))
        self.assertFalse(is_capturable_url("   "))
        self.assertFalse(is_capturable_url(None))
        self.assertFalse(is_capturable_url(12345))

    def test_the_check_is_scheme_only_and_says_so(self):
        """The contract is deliberately narrow: reject what cannot be fetched.

        A syntactically broken URL that *does* use a supported scheme ("https" with no
        host) is not this function's problem - it returns True and the engine's own probe
        fails it with a real error. Trying to validate URLs here would duplicate the probe
        and add a second, weaker parser to keep in sync.
        """
        self.assertTrue(
            is_capturable_url("https"),
            "a scheme-only check is the documented contract; malformed URLs are the "
            "engine's to reject, with a real error message",
        )
        self.assertTrue(is_capturable_url("https://x"))

    def test_the_allow_list_is_exactly_what_the_engines_support(self):
        self.assertEqual(set(CAPTURABLE_SCHEMES), {"http", "https", "magnet"})


class TestExtensionSchemeGuard(unittest.TestCase):
    """The extension source must guard both engines, or the bug returns on one browser.

    Asserted against the file rather than by running JS: the Chromium and Firefox engines
    are separate listener registrations in the same script, and it was exactly the
    asymmetry between them that caused this. A behavioural JS test would need a browser.
    """

    def setUp(self):
        self.source = (
            Path(__file__).resolve().parents[1] / "browser_extension" / "background.js"
        ).read_text(encoding="utf-8")

    def test_both_interception_engines_call_the_guard(self):
        chromium = self.source.count("chrome.downloads.onDeterminingFilename.addListener")
        firefox = self.source.count("chrome.downloads.onCreated.addListener")
        self.assertEqual(chromium, 1, "expected one Chromium interception engine")
        self.assertEqual(firefox, 1, "expected one Firefox interception engine")
        # One definition, one use per engine.
        self.assertEqual(self.source.count("function isCapturableUrl("), 1)
        self.assertEqual(
            self.source.count("!isCapturableUrl("), 2,
            "both the Chromium and the Firefox engine must reject uncapturable schemes; "
            "the Firefox engine had the guard and the Chromium engine did not, which is "
            "why a blob URL was captured on Chrome but skipped on Firefox",
        )

    def test_the_scheme_list_excludes_blob_and_data(self):
        self.assertIn('const CAPTURABLE_SCHEMES = ["http:", "https:", "magnet:"];', self.source)
        self.assertNotIn('"blob:"', self.source.split("CAPTURABLE_SCHEMES")[1].split(";")[0])

    def test_no_engine_still_carries_an_inline_blob_string_check(self):
        """The old Firefox-only `startsWith("blob:")` test must be gone, or the two
        engines drift apart again."""
        self.assertNotIn(
            'downloadUrl.startsWith("blob:")', self.source,
            "the ad-hoc blob check is superseded by isCapturableUrl(); leaving it behind "
            "invites the two engines to diverge again",
        )

    def test_a_declined_capture_is_recorded_rather_than_only_logged(self):
        """A skip must be visible without opening the service-worker console.

        Chrome downloads a declined file itself, so the only symptom is a file that never
        appears in My-IDM and no explanation anywhere the user is looking. That invisibility
        cost real debugging time twice, so the reason is recorded and surfaced in the popup.
        """
        self.assertIn(
            "function recordSkip(", self.source,
            "declines must be recorded, not only written to the console",
        )
        self.assertIn("chrome.storage.local.set(", self.source)
        self.assertIn("lastSkip", self.source)
        self.assertIn("setBadge", self.source, "the skip count must reach the toolbar badge")
        # The Chromium engine must route its refusal through the recorder.
        chromium = self.source.split("onDeterminingFilename.addListener")[1]
        chromium = chromium.split("onCreated.addListener")[0]
        self.assertIn(
            "recordSkip(", chromium,
            "the Chromium engine must record its refusal, not just log it",
        )
        self.assertNotIn(
            "console.log(`[My-IDM] Not capturing", chromium,
            "the bare console.log was replaced by recordSkip, which also stores the reason",
        )

    def test_the_recorded_reason_explains_the_workaround(self):
        """The message has to tell the user what to do, not just that it failed."""
        self.assertIn("right-click the link", self.source)
        self.assertIn("copy the link address", self.source)

    def test_the_reason_is_not_hardcoded_to_one_site(self):
        """A skip fires for any page, so the advice must not name one host.

        Regression: the message originally read "an https://github.com/... link can be
        captured", because it was written while diagnosing a GitHub capture. `blob:` URLs
        come from any page that builds a file in JavaScript, so on every other site the
        popup told the user to look for a link shape that had nothing to do with what they
        were downloading. The site is now read out of the URL itself.

        Only *user-facing* strings are checked: a comment may still use a real host to
        illustrate the URL format.
        """
        code = self._strip_line_comments(self.source)
        for site in ("github.com", "gitlab.com", "example.co.uk", "notion.so"):
            with self.subTest(site=site):
                self.assertNotIn(
                    site, code,
                    f"no site may be baked into the extension's user-facing text ({site})",
                )
        self.assertIn(
            "describeUncapturable", code,
            "the reason must be built from the URL, not hardcoded",
        )
        self.assertIn(
            'raw.slice("blob:".length)', code,
            "the origin is read out of the blob URL so the message can name the site",
        )

    @staticmethod
    def _strip_line_comments(source: str) -> str:
        """Drop `//` comments so a doc example is not mistaken for shipped text.

        Deliberately naive about `//` inside string literals; for this file that only
        affects the URL regex and the ``127.0.0.1`` literals, neither of which contains a
        site name, so the false stripping cannot hide a real violation.
        """
        return "\n".join(
            line.split("//")[0] for line in source.splitlines()
        )

    def test_the_popup_surfaces_the_stored_reason(self):
        popup_js = (
            Path(__file__).resolve().parents[1] / "browser_extension" / "popup.js"
        ).read_text(encoding="utf-8")
        popup_html = (
            Path(__file__).resolve().parents[1] / "browser_extension" / "popup.html"
        ).read_text(encoding="utf-8")
        self.assertIn("lastSkip", popup_js, "the popup must read the recorded reason")
        self.assertIn('id="skipNotice"', popup_html, "and have somewhere to show it")
        self.assertIn('id="skipReason"', popup_html)
        self.assertIn("skip-notice", (Path(__file__).resolve().parents[1]
                                      / "browser_extension" / "styles.css").read_text(
                                          encoding="utf-8"))
    def test_clearing_lives_in_the_popup_not_as_dead_code_in_background(self):
        """A `clearSkips()` with no caller is worse than none: it reads as the feature.

        Regression: the helper existed in background.js and was never invoked, while the
        badge could only climb. Clearing is now the popup's job - it owns both the stored
        keys and the badge text - and the unused helper is gone rather than left as a trap.
        """
        code = self._strip_line_comments(self.source)
        self.assertNotIn(
            "clearSkips", code,
            "the dead helper must be removed, not left looking like the dismiss feature",
        )
        popup_js = (
            Path(__file__).resolve().parents[1] / "browser_extension" / "popup.js"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'chrome.storage.local.remove(["lastSkip", "skipCount"])', popup_js,
            "clearing belongs to the popup, which is the only context that can dismiss it",
        )

    def test_the_dismiss_control_is_styled_and_reachable(self):
        popup_html = (
            Path(__file__).resolve().parents[1] / "browser_extension" / "popup.html"
        ).read_text(encoding="utf-8")
        styles = (
            Path(__file__).resolve().parents[1] / "browser_extension" / "styles.css"
        ).read_text(encoding="utf-8")
        self.assertIn("skip-dismiss", popup_html, "the button carries the styled class")
        self.assertIn(".skip-dismiss", styles, "and that class must exist in the stylesheet")
        self.assertIn("skipNotice.hidden = true", (
            Path(__file__).resolve().parents[1] / "browser_extension" / "popup.js"
        ).read_text(encoding="utf-8"), "dismissing must hide the notice immediately")



class TestServerSchemeGate(unittest.TestCase):
    """``POST /add`` must refuse an uncapturable URL instead of queueing it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

        self.manager = MagicMock()
        self.manager.add_download_from_browser.return_value = "new-id"
        self.config = BrowserIntegrationConfig(enabled=True, min_file_size_kb=0)
        self.server = bs_module.BrowserServer(self.manager, self.config)

    async def _post(self, payload):
        request = MagicMock()
        request.json = AsyncMock(return_value=payload)
        return await self.server._handle_add(request)

    def _add(self, url, **extra):
        payload = {"url": url, "filename": "x.bin"}
        payload.update(extra)
        return run_async(self._post(payload))

    def test_a_blob_url_is_ignored_without_queuing_anything(self):
        response = self._add(BLOB_URL)
        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.text and __import__("json").loads(response.text)["reason"],
            "unsupported_url_scheme",
        )
        self.manager.add_download_from_browser.assert_not_called()

    def test_the_ignored_response_explains_itself_to_the_user(self):
        body = __import__("json").loads(self._add(BLOB_URL).text)
        self.assertEqual(body["status"], "ignored")
        self.assertIn("browser", body["message"].lower())
        self.assertIn("natively", body["message"].lower())

    def test_data_urls_are_refused_too(self):
        response = self._add("data:application/octet-stream,SGVsbG8=")
        self.assertEqual(
            __import__("json").loads(response.text)["reason"], "unsupported_url_scheme"
        )
        self.manager.add_download_from_browser.assert_not_called()

    def test_an_ordinary_https_url_is_still_accepted(self):
        response = self._add("https://example.com/big.zip")
        self.assertEqual(
            __import__("json").loads(response.text)["status"], "ok"
        )
        self.manager.add_download_from_browser.assert_called_once()

    def test_a_magnet_is_still_accepted(self):
        response = self._add("magnet:?xt=urn:btih:da39a3ee")
        self.assertEqual(__import__("json").loads(response.text)["status"], "ok")
        self.manager.add_download_from_browser.assert_called_once()

    def test_the_scheme_check_runs_before_the_size_probe(self):
        """A blob must not cost a 1.8 s probe before being rejected."""
        with patch.object(
            bs_module.BrowserServer, "_probe_content_length", new=AsyncMock()
        ) as probe:
            self._add(BLOB_URL)
        probe.assert_not_called()


class TestServerMinimumSizeGate(unittest.TestCase):
    """The size gate, including the unknown-size case that let small files through."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.manager = MagicMock()
        self.manager.add_download_from_browser.return_value = "new-id"
        self.config = BrowserIntegrationConfig(enabled=True, min_file_size_kb=10000)
        self.server = bs_module.BrowserServer(self.manager, self.config)

    async def _add(self, payload):
        request = MagicMock()
        request.json = AsyncMock(return_value=payload)
        return await self.server._handle_add(request)

    def _body(self, response):
        return __import__("json").loads(response.text)

    def test_a_known_small_size_is_skipped_at_capture_time(self):
        with patch.object(bs_module.BrowserServer, "_probe_content_length", new=AsyncMock()):
            response = run_async(self._add({"url": "https://e.com/a.zip", "total_bytes": 46000}))
        self.assertEqual(self._body(response)["reason"], "file_size_below_minimum")
        self.manager.add_download_from_browser.assert_not_called()

    def test_a_known_large_size_is_accepted_with_no_pending_check(self):
        response = run_async(self._add({
            "url": "https://e.com/big.zip", "total_bytes": 50 * 1024 * 1024,
        }))
        self.assertEqual(self._body(response)["status"], "ok")
        self.assertEqual(
            self.manager.add_download_from_browser.call_args.kwargs["pending_min_bytes"], 0,
            "a size that already satisfies the minimum must not be re-checked by the engine",
        )

    def test_an_unknown_size_defers_the_minimum_to_the_engine(self):
        """The bug: `total_bytes > 0` meant an unsizeable URL bypassed the threshold.

        Chrome reports ``totalBytes: 0`` whenever the response has no Content-Length, and
        for every blob URL. The capture path could not size it, so the comparison was
        skipped and the download was queued regardless of the configured minimum. The
        threshold is now handed to the engine, which always probes.
        """
        with patch.object(
            bs_module.BrowserServer, "_probe_content_length", new=AsyncMock(return_value=None)
        ):
            response = run_async(self._add({"url": "https://e.com/unknown.zip"}))
        self.assertEqual(self._body(response)["status"], "ok")
        self.assertEqual(
            self.manager.add_download_from_browser.call_args.kwargs["pending_min_bytes"],
            10000 * 1024,
            "the threshold must reach the engine when the capture path cannot size the file",
        )

    def test_a_magnet_is_exempt_from_the_minimum_entirely(self):
        """A magnet has no size to compare, and the setting is about file interception."""
        response = run_async(self._add({"url": "magnet:?xt=urn:btih:da39a3ee"}))
        self.assertEqual(self._body(response)["status"], "ok")
        self.assertEqual(
            self.manager.add_download_from_browser.call_args.kwargs["pending_min_bytes"], 0
        )

    def test_no_minimum_configured_means_no_pending_check(self):
        self.config.min_file_size_kb = 0
        with patch.object(
            bs_module.BrowserServer, "_probe_content_length", new=AsyncMock(return_value=None)
        ):
            run_async(self._add({"url": "https://e.com/unknown.zip"}))
        self.assertEqual(
            self.manager.add_download_from_browser.call_args.kwargs["pending_min_bytes"], 0
        )

    def test_the_probe_result_is_used_when_the_extension_knew_no_size(self):
        with patch.object(
            bs_module.BrowserServer, "_probe_content_length",
            new=AsyncMock(return_value=1000),
        ):
            response = run_async(self._add({"url": "https://e.com/tiny.zip"}))
        self.assertEqual(self._body(response)["reason"], "file_size_below_minimum")
        self.manager.add_download_from_browser.assert_not_called()


class TestServerCapturePausedGate(unittest.TestCase):
    """``POST /add`` must decline politely while capture is paused.

    Pausing capture flips ``intercept_all`` rather than ``enabled``, precisely so this server
    stays up: the extension needs somewhere to be told *why* it was declined. That makes this
    gate the reason a paused capture is a pause rather than a disconnect.
    """

    def setUp(self):
        self.manager = MagicMock()
        self.manager.add_download_from_browser.return_value = "new-id"
        self.config = BrowserIntegrationConfig(enabled=True, intercept_all=False)
        self.server = bs_module.BrowserServer(self.manager, self.config)

    def _add(self, url="https://example.com/big.zip"):
        request = MagicMock()
        request.json = AsyncMock(return_value={"url": url, "filename": "x.bin"})
        return run_async(self.server._handle_add(request))

    def test_the_gate_answers_ignored_not_error(self):
        # 200 + "ignored" on purpose: the extension reads a non-ok status as "My-IDM is
        # broken" and falls back to a browser download, whereas `ignored` is the shape it
        # already understands for "handled, not queued".
        response = self._add()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.text)["status"], "ignored")
        self.assertEqual(json.loads(response.text)["reason"], "capture_paused")

    def test_nothing_is_queued_while_paused(self):
        self._add()
        self.manager.add_download_from_browser.assert_not_called()

    def test_the_pause_runs_before_the_scheme_and_size_gates(self):
        # A paused capture must cost no probe and no parsing work.
        with patch.object(
            bs_module.BrowserServer, "_probe_content_length", new=AsyncMock()
        ) as probe:
            self._add()
        probe.assert_not_called()

    def test_resuming_capture_restores_acceptance(self):
        self.config.intercept_all = True
        response = self._add()
        self.assertEqual(json.loads(response.text)["status"], "ok")
        self.manager.add_download_from_browser.assert_called_once()

    def test_disabling_integration_still_wins_over_the_pause(self):
        # enabled=False is the harder gate and keeps its own 403.
        self.config.enabled = False
        response = self._add()
        self.assertEqual(response.status, 403)

    def test_the_preferences_copy_advertises_the_hard_gate(self):
        # The server gate makes intercept_all a kill switch for *every* capture, so a checkbox
        # that still calls it "automatically intercept" promises something it does not do.
        dlg = SettingsDialog(browser_config=self.config)
        self.addCleanup(dlg.deleteLater)
        tooltip = dlg._browser_intercept_cb.toolTip()
        self.assertIn("declines every capture", tooltip)
        self.assertIn("right-click", tooltip)


class EngineMinSizeTestCase(unittest.TestCase):
    """The engine's second, authoritative chance at the minimum-size decision."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)
        self.engine = HTTPEngine(self.db)
        self.engine.set_general_config_sync(GeneralConfig())
        self.statuses: list[tuple] = []
        self.engine.set_callbacks(
            progress_cb=lambda *a: None,
            status_cb=lambda *a: self.statuses.append(a),
            filename_cb=lambda *a: None,
        )

    def add_entry(self, pending_min_bytes=None, total_size=0):
        entry = DownloadEntry(
            id="h1",
            url="https://example.com/a.zip",
            filename="a.zip",
            save_path=self.tmp.as_posix(),
            file_path=(self.tmp / "a.zip").as_posix(),
            total_size=total_size,
            status="queued",
        )
        if pending_min_bytes is not None:
            entry.metadata["pending_min_bytes"] = pending_min_bytes
        self.db.add_download(entry)
        return entry

    def run_with_probe(self, probed_total, chunks=(b"x",)):
        self.engine._session = FakeSession(
            heads=[FakeResponse(200, headers={"Content-Length": str(probed_total)})],
            gets=[FakeResponse(200, chunks=list(chunks))],
        )
        import asyncio

        entry = self.db.get_download("h1")
        run_async(self.engine._run_download(entry, asyncio.Event()))
        return self.db.get_download("h1")

    def test_a_small_probe_result_is_refused_before_any_bytes_are_written(self):
        self.add_entry(pending_min_bytes=10 * 1024 * 1024)
        row = self.run_with_probe(46000)
        self.assertEqual(row.status, "error")
        self.assertIn("below the browser-capture minimum", row.error_message)
        self.assertIn("10485760", row.error_message.replace(",", ""))
        self.assertFalse(
            (self.tmp / "a.zip").exists(),
            "nothing may be written when the capture is refused",
        )
        self.assertEqual(self.statuses[-1][1], "error")

    def test_a_large_probe_result_proceeds_and_clears_the_pending_check(self):
        self.add_entry(pending_min_bytes=10 * 1024 * 1024)
        row = self.run_with_probe(50 * 1024 * 1024)
        self.assertEqual(row.status, "completed")
        self.assertNotIn(
            "pending_min_bytes", row.metadata,
            "a satisfied threshold must not be re-checked on every resume",
        )

    def test_a_hand_added_download_is_never_gated(self):
        """The setting means 'minimum size to intercept', not 'minimum size to allow'."""
        self.add_entry(pending_min_bytes=None)
        row = self.run_with_probe(10)
        self.assertEqual(
            row.status, "completed",
            "a 10-byte download the user added by hand must still work",
        )

    def test_an_unknown_probe_size_proceeds_because_it_cannot_be_judged(self):
        """Refusing everything unsizeable would break chunked and gzip responses."""
        self.add_entry(pending_min_bytes=10 * 1024 * 1024)
        self.engine._session = FakeSession(
            heads=[FakeResponse(403)],
            gets=[FakeResponse(200, chunks=[b"x"])],
        )
        import asyncio

        run_async(self.engine._run_download(self.db.get_download("h1"), asyncio.Event()))
        self.assertNotEqual(
            self.db.get_download("h1").status, "error",
            "an unsizeable download must not be refused on a guess",
        )

    def test_the_check_is_skipped_when_no_threshold_was_deferred(self):
        self.add_entry(pending_min_bytes=0)
        row = self.run_with_probe(1)
        self.assertEqual(row.status, "completed")


    def test_the_skip_notice_can_be_dismissed(self):
        """Regression: `clearSkips()` existed but nothing ever called it.

        Both the stored notice and the badge count were write-only, so the toolbar badge
        could only ever climb and a stale warning could never be acknowledged - it stayed
        on screen telling the user a download had not been captured long after they had
        acted on it. The popup now owns clearing, and it clears the badge text as well as
        the storage, because the badge is toolbar state rather than stored data.
        """
        popup_js = (
            Path(__file__).resolve().parents[1] / "browser_extension" / "popup.js"
        ).read_text(encoding="utf-8")
        popup_html = (
            Path(__file__).resolve().parents[1] / "browser_extension" / "popup.html"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'chrome.storage.local.remove(["lastSkip", "skipCount"])', popup_js,
            "dismissing must remove both stored keys",
        )
        self.assertIn(
            "setBadgeText", popup_js,
            "the toolbar badge is separate state and must be cleared too",
        )
        self.assertIn('id="dismissSkipBtn"', popup_html, "there must be a control to click")
        self.assertIn("dismissSkipBtn.addEventListener", popup_js)

if __name__ == "__main__":
    unittest.main()
