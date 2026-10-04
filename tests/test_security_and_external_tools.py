"""Hardened tests for the security (antivirus) and external-tools edge cases.

``test_security.py`` covers the headline paths - a clean URL, a deceptive double
extension, Defender's exit codes, threat exclusions. What it does not cover is the
unhappy half of each decision, which is where this module actually misbehaves:

* the *bare-IP* and *VirusTotal* branches of :func:`check_url_safety` (including the
  ``except`` that swallows a network failure);
* :func:`scan_file` for a scanner that is missing, slow, or returns a code that is neither
  0 nor 2 - including the ambiguous "completed with code 1 but said threat" case;
* :class:`SecurityConfig` round-tripping when the persisted value is a *string* rather
  than a list, which is exactly what a hand-edited ini produces;
* :mod:`external_tools` argument assembly, which silently drops ``-q``/``-l`` when the
  user leaves them on ``Auto``, and its several process-launch fallbacks.

One test class exercises :func:`scan_file` from many threads at once. The manager runs
every post-download scan on a short-lived ``scan-<id>`` daemon thread, so two downloads
finishing at the same moment call this function concurrently; the test proves the
verdicts stay independent rather than leaking between threads.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QSettings, QUrl
from PySide6.QtWidgets import QApplication

from my_idm import external_tools, security
from my_idm.config import ExternalToolsConfig
from my_idm.security import (
    HIGH_RISK_EXTENSIONS,
    KNOWN_THREAT_CATEGORIES,
    SecurityConfig,
    _is_threat_excluded,
    check_url_safety,
    find_windows_defender_path,
    quarantine_or_delete_file,
    scan_file,
)
from my_idm.utils import normalize_path

app = QApplication.instance() or QApplication(sys.argv)


class IsolatedSettingsTestCase(unittest.TestCase):
    """A private ini file so no test can ever touch the live registry settings.

    ``conftest.py`` already redirects bare ``QSettings(org, app)`` into a temp tree; this
    adds the explicit-file form for the tests that need a *known-empty* starting point
    regardless of what a sibling test wrote.
    """

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.settings = QSettings(
            str(Path(holder.name) / "prefs.ini"), QSettings.Format.IniFormat
        )
        self.addCleanup(self.settings.sync)


# ---------------------------------------------------------------------------
# check_url_safety
# ---------------------------------------------------------------------------

class TestUrlSafetyEdgeCases(unittest.TestCase):
    """The branches of :func:`check_url_safety` beyond the two headline checks."""

    def _check(self, url, **config_kw):
        return check_url_safety(url, SecurityConfig(**config_kw))

    # -- short circuits -------------------------------------------------------

    def test_pre_scan_disabled_short_circuits_every_check(self):
        safe, risk, detail = self._check(
            "https://1.2.3.4/evil.pdf.exe", scan_before_download=False
        )
        self.assertTrue(safe)
        self.assertEqual(risk, "clean")
        self.assertIn("disabled", detail)

    def test_magnet_links_are_deferred_to_the_post_download_scan(self):
        safe, risk, _ = self._check("magnet:?xt=urn:btih:da39a3ee")
        self.assertTrue(safe)
        self.assertEqual(risk, "clean")

    def test_torrent_files_are_deferred(self):
        safe, risk, _ = self._check("https://example.com/linux.torrent")
        self.assertTrue(safe)
        self.assertEqual(risk, "clean")

    def test_a_torrent_with_a_deceptive_name_is_still_deferred(self):
        """The torrent short-circuit must win over the double-extension check."""
        safe, risk, _ = self._check("https://example.com/innocent.pdf.exe.torrent")
        self.assertTrue(safe)
        self.assertEqual(risk, "clean")

    def test_surrounding_whitespace_is_stripped_before_parsing(self):
        safe, risk, _ = self._check("   https://1.2.3.4/setup.exe   ")
        self.assertEqual(risk, "warning", "the bare-IP rule must still see the host")

    def test_a_clean_https_url_is_clean(self):
        self.assertEqual(
            self._check("https://releases.ubuntu.com/24.04/ubuntu-desktop-amd64.deb")[1],
            "clean",
        )

    def test_a_legitimate_iso_is_flagged_as_a_warning_known_quirk(self):
        """Documents behaviour rather than blessing it.

        ``.iso`` and ``.img`` are both in ``HIGH_RISK_EXTENSIONS`` because a container can
        embed an executable. The same check therefore warns on every Linux disk image and
        Windows installer ISO, which is a large share of real traffic on this app. A
        "container can hide code" rule wants its own risk level, not the executable one.
        """
        safe, risk, detail = self._check("https://releases.ubuntu.com/24.04/ubuntu.iso")
        self.assertEqual(risk, "warning")
        self.assertIn(".iso", detail)
        self.assertIn(
            ".iso", HIGH_RISK_EXTENSIONS,
            "KNOWN QUIRK: a plain OS image ISO is treated as a high-risk payload",
        )

    # -- deceptive extensions ------------------------------------------------

    def test_a_double_extension_is_dangerous_and_blocking(self):
        safe, risk, detail = self._check("https://example.com/invoice.pdf.exe")
        self.assertFalse(safe, "a double extension is never merely a warning")
        self.assertEqual(risk, "dangerous")
        self.assertIn("double extension", detail.lower())

    def test_a_triple_extension_is_still_dangerous(self):
        safe, risk, _ = self._check("https://example.com/a.pdf.doc.exe")
        self.assertEqual(risk, "dangerous")
        self.assertFalse(safe)

    def test_a_one_character_middle_extension_does_not_match(self):
        """The pattern requires a 2-4 character middle token, so ``a.b.exe`` is clean-ish."""
        safe, risk, _ = self._check("https://example.com/a.b.exe")
        self.assertEqual(risk, "warning", "only the single-extension rule may fire")
        self.assertTrue(safe)

    def test_an_executable_extension_only_warns_by_default(self):
        safe, risk, detail = self._check("https://example.com/setup.exe")
        self.assertTrue(safe, "the default is warn, not block")
        self.assertEqual(risk, "warning")
        self.assertIn(".exe", detail)

    def test_an_executable_extension_blocks_in_strict_mode(self):
        safe, risk, _ = self._check(
            "https://example.com/setup.exe", block_dangerous_urls=True
        )
        self.assertFalse(safe)
        self.assertEqual(risk, "warning")

    def test_disabling_the_warning_downgrades_an_executable_to_clean(self):
        safe, risk, _ = self._check(
            "https://example.com/setup.exe", warn_high_risk_extensions=False
        )
        self.assertTrue(safe)
        self.assertEqual(risk, "clean")

    def test_every_high_risk_extension_is_recognised(self):
        for extension in sorted(HIGH_RISK_EXTENSIONS):
            with self.subTest(extension=extension):
                _, risk, _ = self._check(f"https://example.com/payload{extension}")
                self.assertEqual(risk, "warning", extension)

    def test_the_extension_check_is_case_insensitive(self):
        _, risk, _ = self._check("https://example.com/SETUP.EXE")
        self.assertEqual(risk, "warning")

    def test_a_query_string_extension_does_not_trigger_the_rule(self):
        """``.exe`` in the query is not a filename extension."""
        _, risk, _ = self._check("https://example.com/download?file=setup.exe")
        self.assertEqual(risk, "clean")

    # -- bare IP addresses ---------------------------------------------------

    def test_a_public_bare_ip_warns_but_still_downloads(self):
        safe, risk, detail = self._check("https://203.0.113.7/payload.bin")
        self.assertTrue(safe)
        self.assertEqual(risk, "warning")
        self.assertIn("203.0.113.7", detail)

    def test_loopback_private_and_lan_addresses_are_not_flagged(self):
        for host in ("127.0.0.1", "192.168.1.10", "10.0.0.5", "localhost", "example.com"):
            with self.subTest(host=host):
                _, risk, _ = self._check(f"https://{host}/a.zip")
                self.assertEqual(risk, "clean", host)

    def test_a_public_ip_with_a_port_is_still_detected(self):
        _, risk, _ = self._check("https://198.51.100.9:8443/a.zip")
        self.assertEqual(risk, "warning")

    def test_a_non_numeric_host_is_not_mistaken_for_an_ip(self):
        _, risk, _ = self._check("https://1.2.3.4.5.example.com/a.zip")
        self.assertEqual(risk, "clean")

    # -- VirusTotal ----------------------------------------------------------

    def test_virustotal_is_not_queried_without_a_key(self):
        with patch.object(security.urllib.request, "urlopen") as urlopen:
            self._check("https://example.com/a.zip")
        urlopen.assert_not_called()

    def test_virustotal_is_not_queried_for_a_non_http_url(self):
        with patch.object(security.urllib.request, "urlopen") as urlopen:
            check_url_safety(
                "magnet:?xt=urn:btih:da39a3ee",
                SecurityConfig(virustotal_api_key="k" * 64),
            )
        urlopen.assert_not_called()

    def test_virustotal_malicious_verdicts_block_the_download(self):
        response = MagicMock()
        response.status = 200
        response.read.return_value = (
            b'{"data": {"attributes": {"last_analysis_stats": '
            b'{"malicious": 3, "suspicious": 1}}}}'
        )
        response.__enter__.return_value = response
        with patch.object(security.urllib.request, "urlopen", return_value=response):
            safe, risk, detail = self._check(
                "https://example.com/a.zip", virustotal_api_key="k" * 64
            )
        self.assertFalse(safe)
        self.assertEqual(risk, "dangerous")
        self.assertIn("3 malicious", detail)
        self.assertIn("1 suspicious", detail)

    def test_virustotal_suspicious_only_also_blocks(self):
        response = MagicMock()
        response.status = 200
        response.read.return_value = (
            b'{"data": {"attributes": {"last_analysis_stats": '
            b'{"malicious": 0, "suspicious": 2}}}}'
        )
        response.__enter__.return_value = response
        with patch.object(security.urllib.request, "urlopen", return_value=response):
            safe, risk, _ = self._check(
                "https://example.com/a.zip", virustotal_api_key="k" * 64
            )
        self.assertFalse(safe)
        self.assertEqual(risk, "dangerous")

    def test_virustotal_all_clean_keeps_the_download(self):
        response = MagicMock()
        response.status = 200
        response.read.return_value = (
            b'{"data": {"attributes": {"last_analysis_stats": '
            b'{"malicious": 0, "suspicious": 0}}}}'
        )
        response.__enter__.return_value = response
        with patch.object(security.urllib.request, "urlopen", return_value=response):
            safe, risk, _ = self._check(
                "https://example.com/a.zip", virustotal_api_key="k" * 64
            )
        self.assertTrue(safe)
        self.assertEqual(risk, "clean")

    def test_a_virustotal_outage_fails_open(self):
        """A third-party outage must not stop the user's download."""
        with patch.object(
            security.urllib.request, "urlopen", side_effect=OSError("dns failure")
        ):
            safe, risk, detail = self._check(
                "https://example.com/a.zip", virustotal_api_key="k" * 64
            )
        self.assertTrue(safe)
        self.assertEqual(risk, "clean")
        self.assertIn("passed preliminary", detail)

    def test_a_virustotal_non_200_response_fails_open(self):
        response = MagicMock()
        response.status = 429
        response.__enter__.return_value = response
        with patch.object(security.urllib.request, "urlopen", return_value=response):
            safe, _, _ = self._check(
                "https://example.com/a.zip", virustotal_api_key="k" * 64
            )
        self.assertTrue(safe)

    def test_a_malformed_virustotal_body_fails_open(self):
        response = MagicMock()
        response.status = 200
        response.read.return_value = b"not json"
        response.__enter__.return_value = response
        with patch.object(security.urllib.request, "urlopen", return_value=response):
            safe, _, _ = self._check(
                "https://example.com/a.zip", virustotal_api_key="k" * 64
            )
        self.assertTrue(safe)


# ---------------------------------------------------------------------------
# Threat exclusion
# ---------------------------------------------------------------------------

class TestThreatExclusionEdges(unittest.TestCase):
    """``_is_threat_excluded``: the matching rules and the fall-throughs."""

    def test_a_category_match_is_case_insensitive(self):
        config = SecurityConfig(ignored_threat_categories=["HackTool"])
        self.assertEqual(_is_threat_excluded("Detection: HACKTOOL/Gen", config), "HackTool")

    def test_a_custom_pattern_matches_a_report_with_no_category(self):
        config = SecurityConfig(
            ignored_threat_categories=[], ignored_threat_patterns="my-internal-tool"
        )
        self.assertEqual(
            _is_threat_excluded("Found: my-internal-tool.exe", config), "my-internal-tool"
        )

    def test_custom_patterns_are_comma_separated_and_whitespace_tolerant(self):
        config = SecurityConfig(
            ignored_threat_patterns=" alpha , beta ,, gamma ",
        )
        self.assertEqual(_is_threat_excluded("hit alpha here", config), "alpha")
        self.assertEqual(_is_threat_excluded("hit beta here", config), "beta")
        self.assertEqual(_is_threat_excluded("hit gamma here", config), "gamma")
        self.assertIsNone(_is_threat_excluded("hit delta here", config))

    def test_blank_pattern_segments_are_dropped_not_matched_as_empty_strings(self):
        """``"a,,b"`` must not produce an empty pattern, which would match every report."""
        config = SecurityConfig(ignored_threat_patterns="a,,b")
        self.assertIsNone(_is_threat_excluded("zzz", config))

    def test_an_explicitly_empty_category_list_means_exclude_nothing(self):
        """Regression test: un-ticking every category must actually exclude nothing.

        ``get_effective_threat_exclusions`` treated any falsy list as unset and substituted
        the five defaults, so a user who removed every category in the security dialog
        silently got them all back - and the dialog had no way to say "allow no threats".
        ``None`` (never configured) still means the defaults; a list is honoured as written.
        """
        config = SecurityConfig(ignored_threat_categories=[])
        self.assertEqual(config.get_effective_threat_exclusions(), [])
        self.assertIsNone(
            _is_threat_excluded("Threat: HackTool", config),
            "with every category excluded, nothing may be silently allowed",
        )
        self.assertIsNone(_is_threat_excluded("PUA/Win.Gen", config))

    def test_an_unset_category_list_falls_back_to_the_defaults(self):
        config = SecurityConfig(ignored_threat_categories=None)
        self.assertEqual(
            config.get_effective_threat_exclusions(), list(KNOWN_THREAT_CATEGORIES)
        )

    def test_a_blank_only_list_excludes_nothing_too(self):
        """Blanks carry no category, so they cannot stand in for "unset"."""
        config = SecurityConfig(ignored_threat_categories=["", "   "])
        self.assertEqual(config.get_effective_threat_exclusions(), [])

    def test_an_explicit_list_still_narrows_the_defaults(self):
        config = SecurityConfig(ignored_threat_categories=["CrackTool"])
        self.assertEqual(
            _is_threat_excluded("CrackTool hit", config), "CrackTool"
        )
        self.assertIsNone(
            _is_threat_excluded("HackTool hit", config),
            "narrowing the list must not re-admit the other defaults",
        )

    def test_a_known_category_that_is_not_in_the_report_does_not_match(self):
        config = SecurityConfig(ignored_threat_categories=["HackTool"])
        self.assertIsNone(_is_threat_excluded("Trojan:Win32/Emotet", config))

    def test_an_empty_report_matches_nothing(self):
        self.assertIsNone(
            _is_threat_excluded("", SecurityConfig(ignored_threat_categories=["HackTool"]))
        )


class TestSecurityConfigPersistence(IsolatedSettingsTestCase):
    """Round-tripping, including the hand-edited-ini shapes that break naive code."""

    def test_defaults_are_intact_after_a_save_load(self):
        config = SecurityConfig(
            scan_before_download=False,
            block_dangerous_urls=True,
            scanner_type="custom",
            custom_scanner_path="C:/tools/scan.exe",
            action_on_threat="delete",
            scan_timing="manual_only",
            ignored_threat_patterns="foo,bar",
        )
        config.save(self.settings)
        loaded = SecurityConfig.load(self.settings)
        self.assertEqual(loaded.to_dict(), config.to_dict())

    def test_exclusions_are_persisted_as_a_comma_separated_string(self):
        SecurityConfig(ignored_threat_categories=["Alpha", "Beta"]).save(self.settings)
        self.assertEqual(
            self.settings.value("Security/ignored_threat_categories"), "Alpha,Beta"
        )

    def test_a_string_exclusion_list_is_re_parsed(self):
        """A hand-edited ini stores a bare string; it must come back as a list."""
        SecurityConfig().save(self.settings)
        self.settings.setValue("Security/ignored_threat_categories", "One, Two ,Three")
        loaded = SecurityConfig.load(self.settings)
        self.assertEqual(loaded.ignored_threat_categories, ["One", "Two", "Three"])

    def test_an_explicitly_empty_list_survives_a_save_load(self):
        """The round trip that makes the choice stick across a restart."""
        SecurityConfig(ignored_threat_categories=[]).save(self.settings)
        self.assertEqual(
            self.settings.value("Security/ignored_threat_categories"), "",
            "an empty exclusion list persists as an empty value",
        )
        self.assertEqual(
            SecurityConfig.load(self.settings).get_effective_threat_exclusions(), [],
            "a present-but-empty value means exclude nothing, not 'use the defaults'",
        )

    def test_a_missing_key_still_restores_the_defaults(self):
        SecurityConfig().save(self.settings)
        self.settings.remove("Security/ignored_threat_categories")
        self.assertEqual(
            SecurityConfig.load(self.settings).get_effective_threat_exclusions(),
            list(KNOWN_THREAT_CATEGORIES),
            "a never-written key must fall back to the defaults",
        )

    def test_a_whitespace_only_custom_scanner_args_falls_back(self):
        SecurityConfig().save(self.settings)
        self.settings.setValue("Security/custom_scanner_args", "")
        self.assertEqual(SecurityConfig.load(self.settings).custom_scanner_args, '"%file%"')

    def test_from_dict_accepts_a_string_category_field(self):
        config = SecurityConfig.from_dict({"ignored_threat_categories": "A,B"})
        self.assertEqual(config.ignored_threat_categories, ["A", "B"])

    def test_from_dict_with_an_empty_list_excludes_nothing(self):
        config = SecurityConfig.from_dict({"ignored_threat_categories": []})
        self.assertEqual(config.get_effective_threat_exclusions(), [])

    def test_from_dict_with_a_blank_string_excludes_nothing(self):
        config = SecurityConfig.from_dict({"ignored_threat_categories": " , "})
        self.assertEqual(config.get_effective_threat_exclusions(), [])

    def test_from_dict_with_no_key_uses_the_defaults(self):
        config = SecurityConfig.from_dict({})
        self.assertEqual(
            config.get_effective_threat_exclusions(), list(KNOWN_THREAT_CATEGORIES)
        )

    def test_the_default_scanner_args_reference_the_placeholder(self):
        self.assertIn("%file%", SecurityConfig().custom_scanner_args)


# ---------------------------------------------------------------------------
# scan_file
# ---------------------------------------------------------------------------

class TestScanFileEdges(unittest.TestCase):
    """Every scanner outcome, including the ones that are neither clean nor threat."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = Path(self._tmp.name) / "payload.bin"
        self.target.write_bytes(b"data")
        # `scan_file` only reaches the Defender branch when `sys.platform == "win32"`; off
        # Windows it looks for ClamAV instead, which no CI runner has, so every verdict test
        # below silently degraded into "no scanner available". The scanner itself is faked
        # (`subprocess.run` and `find_windows_defender_path`), so what is under test is the
        # verdict logic, not the host - pinning the platform keeps that logic covered on all
        # three runners instead of skipping it off Windows. The one test that wants the POSIX
        # branch still patches the platform itself.
        platform_patcher = patch.object(security.sys, "platform", "win32")
        platform_patcher.start()
        self.addCleanup(platform_patcher.stop)

    def _defender(self, returncode=0, stdout="", stderr=""):
        result = subprocess.CompletedProcess(args=[], returncode=returncode,
                                            stdout=stdout, stderr=stderr)
        return patch.object(subprocess, "run", return_value=result)

    def test_post_scan_disabled_returns_clean_without_touching_the_scanner(self):
        with patch.object(subprocess, "run") as run:
            clean, detail = scan_file(str(self.target), SecurityConfig(scan_after_download=False))
        self.assertTrue(clean)
        run.assert_not_called()
        self.assertIn("disabled", detail)

    def test_a_missing_target_is_reported_not_scanned(self):
        with patch.object(subprocess, "run") as run:
            clean, detail = scan_file(
                str(self.target) + ".gone", SecurityConfig()
            )
        self.assertTrue(clean)
        run.assert_not_called()
        self.assertIn("does not exist", detail)

    def test_a_missing_defender_yields_no_verdict(self):
        """No scanner is not a clean file.

        This used to return `True`, which made every scan on Linux and macOS report success for a
        file nothing had looked at.
        """
        with patch.object(security, "find_windows_defender_path", return_value=None):
            verdict, detail = scan_file(str(self.target), SecurityConfig())
        self.assertIsNone(verdict)
        self.assertNotEqual(verdict, False, "no scanner must not become a threat verdict")
        self.assertIn("not scanned", detail)

    def test_defender_exit_code_zero_is_clean(self):
        with patch.object(security, "find_windows_defender_path", return_value="MpCmdRun.exe"), \
             self._defender(0):
            clean, detail = scan_file(str(self.target), SecurityConfig())
        self.assertTrue(clean)
        self.assertIn("no threats", detail)

    def test_defender_exit_code_two_is_a_threat(self):
        with patch.object(security, "find_windows_defender_path", return_value="MpCmdRun.exe"), \
             self._defender(2, stdout="Trojan:Win32/Emotet"):
            clean, detail = scan_file(str(self.target), SecurityConfig())
        self.assertFalse(clean)
        self.assertIn("Emotet", detail)

    def test_defender_exit_code_two_matching_an_exclusion_is_allowed(self):
        config = SecurityConfig(ignored_threat_categories=["Emotet"])
        with patch.object(security, "find_windows_defender_path", return_value="MpCmdRun.exe"), \
             self._defender(2, stdout="Trojan:Win32/Emotet"):
            clean, detail = scan_file(str(self.target), config)
        self.assertTrue(clean)
        self.assertIn("exclusion", detail)

    def test_an_unknown_exit_code_that_says_found_no_threats_is_clean(self):
        with patch.object(security, "find_windows_defender_path", return_value="MpCmdRun.exe"), \
             self._defender(1, stdout="MpCmdRun.exe: Scan found no threats"):
            clean, detail = scan_file(str(self.target), SecurityConfig())
        self.assertTrue(clean)
        self.assertIn("no threats", detail)

    def test_an_unknown_exit_code_that_mentions_a_threat_is_flagged(self):
        with patch.object(security, "find_windows_defender_path", return_value="MpCmdRun.exe"), \
             self._defender(1, stdout="Threat: Win32/Emotet.A detected"):
            clean, detail = scan_file(
                str(self.target),
                SecurityConfig(ignored_threat_categories=["OnlyThisOne"]),
            )
        self.assertFalse(clean)
        self.assertIn("Emotet", detail)

    def test_an_unknown_exit_code_that_mentions_a_threat_still_honours_exclusions(self):
        with patch.object(security, "find_windows_defender_path", return_value="MpCmdRun.exe"), \
             self._defender(1, stdout="PUA/Win.Gen threat detected"):
            clean, detail = scan_file(str(self.target), SecurityConfig())
        self.assertTrue(
            clean,
            "KNOWN QUIRK: the unknown-exit-code branch honours exclusions just like the "
            "returncode==2 branch, so 'PUA' in the default list silently allows it",
        )
        self.assertIn("exclusion", detail)

    def test_an_unknown_exit_code_with_no_verdict_is_not_guessed_either_way(self):
        """Exit 1 with an unreadable log must not be guessed - and "not guessed" is not clean.

        This is the narrower sibling of the missing-scanner case: Defender *ran*, but its output
        matches neither verdict, so the scan established nothing.
        """
        with patch.object(security, "find_windows_defender_path", return_value="MpCmdRun.exe"), \
             self._defender(1, stdout=""):
            verdict, detail = scan_file(str(self.target), SecurityConfig())
        self.assertIsNone(verdict)
        self.assertNotEqual(verdict, False)
        self.assertIn("exited with code 1", detail)
        self.assertIn("not scanned", detail)

    def test_a_defender_timeout_yields_no_verdict(self):
        with patch.object(security, "find_windows_defender_path", return_value="MpCmdRun.exe"), \
             patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("cmd", 90)):
            verdict, detail = scan_file(str(self.target), SecurityConfig())
        self.assertIsNone(verdict)
        self.assertNotEqual(verdict, False)
        self.assertIn("timed out", detail)

    def test_a_defender_crash_yields_no_verdict(self):
        with patch.object(security, "find_windows_defender_path", return_value="MpCmdRun.exe"), \
             patch.object(subprocess, "run", side_effect=OSError("access denied")):
            verdict, detail = scan_file(str(self.target), SecurityConfig())
        self.assertIsNone(verdict)
        self.assertNotEqual(verdict, False)
        self.assertIn("could not be executed", detail)

    def test_the_defender_command_line_is_the_documented_one(self):
        with patch.object(security, "find_windows_defender_path", return_value="MpCmdRun.exe"), \
             patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                 args=[], returncode=0, stdout="", stderr="")) as run:
            scan_file(str(self.target), SecurityConfig())
        argv = run.call_args.args[0]
        self.assertEqual(argv[1:], ["-Scan", "-ScanType", "3", "-File",
                                    str(self.target.resolve()), "-DisableRemediation"])

    def test_the_defender_scan_uses_the_absolute_path(self):
        with patch.object(security, "find_windows_defender_path", return_value="MpCmdRun.exe"), \
             patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                 args=[], returncode=0, stdout="", stderr="")) as run:
            scan_file(str(self.target), SecurityConfig())
        self.assertEqual(
            run.call_args.args[0][5], str(self.target.resolve()),
            "MpCmdRun must be handed the resolved path, not the caller's relative one",
        )

    # -- custom scanner -------------------------------------------------------

    def _custom(self, target_scanner, **kw):
        return SecurityConfig(scanner_type="custom", custom_scanner_path=str(target_scanner), **kw)

    def test_a_custom_scanner_exit_code_zero_is_clean(self):
        scanner = Path(self._tmp.name) / "scan.exe"
        scanner.write_text("# stub")
        with patch.object(subprocess, "run",
                          return_value=subprocess.CompletedProcess(args=[], returncode=0,
                                                                   stdout="ok", stderr="")):
            clean, detail = scan_file(str(self.target), self._custom(scanner))
        self.assertTrue(clean)
        self.assertIn("Custom Scanner", detail)

    def test_a_custom_scanner_failure_is_a_threat(self):
        scanner = Path(self._tmp.name) / "scan.exe"
        scanner.write_text("# stub")
        with patch.object(subprocess, "run",
                          return_value=subprocess.CompletedProcess(args=[], returncode=1,
                                                                   stdout="INFECTED", stderr="")):
            clean, detail = scan_file(str(self.target), self._custom(scanner))
        self.assertFalse(clean)
        self.assertIn("INFECTED", detail)

    def test_a_custom_scanner_failure_matching_an_exclusion_is_allowed(self):
        scanner = Path(self._tmp.name) / "scan.exe"
        scanner.write_text("# stub")
        config = self._custom(scanner, ignored_threat_patterns="keygen")
        with patch.object(subprocess, "run",
                          return_value=subprocess.CompletedProcess(args=[], returncode=1,
                                                                   stdout="keygen alert", stderr="")):
            clean, detail = scan_file(str(self.target), config)
        self.assertTrue(clean)
        self.assertIn("exclusion", detail)

    def test_a_custom_scanner_timeout_yields_no_verdict(self):
        scanner = Path(self._tmp.name) / "scan.exe"
        scanner.write_text("# stub")
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("c", 60)):
            verdict, detail = scan_file(str(self.target), self._custom(scanner))
        self.assertIsNone(verdict)
        self.assertNotEqual(verdict, False)
        self.assertIn("timed out", detail)

    def test_a_custom_scanner_that_cannot_execute_yields_no_verdict(self):
        scanner = Path(self._tmp.name) / "scan.exe"
        scanner.write_text("# stub")
        with patch.object(subprocess, "run", side_effect=OSError("not a valid win32 app")):
            verdict, detail = scan_file(str(self.target), self._custom(scanner))
        self.assertIsNone(verdict)
        self.assertNotEqual(verdict, False)
        self.assertIn("could not be executed", detail)

    def test_a_custom_scanner_with_no_path_falls_through_to_defender(self):
        with patch.object(security, "find_windows_defender_path", return_value=None):
            verdict, detail = scan_file(
                str(self.target), SecurityConfig(scanner_type="custom", custom_scanner_path="")
            )
        self.assertIn("Defender", detail)
        self.assertIsNone(verdict)
        self.assertNotEqual(verdict, False)

    def test_both_placeholder_spellings_are_substituted(self):
        scanner = Path(self._tmp.name) / "scan.exe"
        scanner.write_text("# stub")
        with patch.object(subprocess, "run",
                          return_value=subprocess.CompletedProcess(args=[], returncode=0,
                                                                   stdout="", stderr="")) as run:
            scan_file(str(self.target), self._custom(scanner, custom_scanner_args="--in %f --out"))
        argv = run.call_args.args[0]
        self.assertEqual(
            argv, [str(scanner), "--in", str(self.target.resolve()), "--out"],
            "the template must be split into separate arguments, not interpolated into a string",
        )

    def test_a_hostile_download_name_cannot_inject_a_shell_command(self):
        """The path is attacker-influenced, so it must never be interpreted by a shell.

        The target path comes from a download's filename, which a crafted torrent controls. This
        used to be interpolated into a `shell=True` string, where a double quote in the name
        closes the surrounding quotes and `&`, `|`, or `$(...)` then execute - arbitrary commands
        as the user, triggered by downloading a file.

        The names are synthesised rather than created on disk: `"`, `|`, `<` and `>` are all
        rejected in Windows filenames, so a test that made a real file could only ever run on
        POSIX and would silently skip where the shell=True code path was most reachable.
        Asserted on the argv rather than by executing anything: the point is that no shell is
        involved, so there is nothing to intercept.
        """
        scanner = Path(self._tmp.name) / "scan.exe"
        scanner.write_text("# stub")

        hostile_names = [
            'payload"&calc.exe&"',
            "payload`calc`",
            "payload$(calc).bin",
            "payload|calc|.bin",
            "payload\ncalc\n",
            'payload";calc;"',
        ]
        for name in hostile_names:
            with self.subTest(name=name):
                victim = Path(self._tmp.name) / name
                with patch.object(Path, "exists", return_value=True), \
                     patch.object(Path, "resolve", return_value=victim), \
                     patch.object(subprocess, "run",
                                  return_value=subprocess.CompletedProcess(
                                      args=[], returncode=0, stdout="", stderr="")) as run:
                    scan_file(str(victim), self._custom(scanner))

                self.assertNotIn("shell", run.call_args.kwargs)
                argv = run.call_args.args[0]
                self.assertIsInstance(argv, list, "the scanner must be invoked with an argv list")
                # The whole name survives as exactly one argument - nothing split, nothing dropped.
                self.assertEqual(argv, [str(scanner), str(victim)])

    def test_a_custom_scanner_argument_template_with_no_placeholder_is_passed_through(self):
        scanner = Path(self._tmp.name) / "scan.exe"
        scanner.write_text("# stub")
        with patch.object(subprocess, "run",
                          return_value=subprocess.CompletedProcess(args=[], returncode=0,
                                                                   stdout="", stderr="")) as run:
            scan_file(str(self.target), self._custom(scanner, custom_scanner_args="--full"))
        self.assertIn("--full", run.call_args.args[0])
        self.assertNotIn("%file%", run.call_args.args[0])


class TestScanFileThreadSafety(unittest.TestCase):
    """``scan_file`` is called from one short-lived daemon thread per completed download.

    Two downloads finishing together therefore run it concurrently. The test proves the
    verdicts do not bleed between threads and that no scanner invocation is lost.
    """

    THREADS = 8

    @staticmethod
    def _key(path):
        """Comparable form of a path the scanner is handed.

        ``scan_file`` passes ``str(Path(file_path).resolve())`` to the scanner, so the argv a
        thread sees is not necessarily string-identical to the path the test created: GitHub's
        Windows runners point ``TEMP`` at the 8.3 short form ``C:\\Users\\RUNNER~1\\...``, which
        ``resolve()`` expands to ``...\\runneradmin\\...``, and macOS ``/tmp`` is a symlink to
        ``/private/tmp``. Looking the target up by value then missed, ``list.index`` raised, and
        ``scan_file`` - correctly - swallowed it into a "no verdict" result, so the test failed
        with a verdict mismatch rather than the real cause.
        """
        return os.path.normcase(str(Path(path).resolve()))

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.targets = []
        for index in range(self.THREADS):
            path = Path(self._tmp.name) / f"payload{index}.bin"
            path.write_bytes(b"data")
            self.targets.append(path)
        self.index_by_key = {self._key(p): i for i, p in enumerate(self.targets)}
        # As in `TestScanFileEdges`: the Defender branch is the one under test, and it is only
        # taken when `sys.platform == "win32"`. Off Windows `scan_file` looks for ClamAV, finds
        # nothing on a runner, and returns no verdict at all.
        platform_patcher = patch.object(security.sys, "platform", "win32")
        platform_patcher.start()
        self.addCleanup(platform_patcher.stop)

    def test_concurrent_scans_stay_independent(self):
        """Half the targets are "infected"; each thread must see only its own verdict.

        The barrier makes every thread reach ``scan_file`` at the same moment, and the
        scanner derives its verdict from the path in its own argv, so a leaked variable or
        a shared result cache would show up as a mismatched verdict on at least one thread.
        """
        start = threading.Barrier(self.THREADS, timeout=10)
        results: dict[int, bool] = {}
        lock = threading.Lock()
        calls: list[str] = []

        def _scanner(argv, **kwargs):
            scanned = argv[5]
            with lock:
                calls.append(scanned)
            returncode = 2 if self.index_by_key[self._key(scanned)] % 2 == 0 else 0
            return subprocess.CompletedProcess(args=argv, returncode=returncode,
                                               stdout="", stderr="")

        errors: list[BaseException] = []

        def _worker(index: int):
            try:
                start.wait()
                clean, _ = scan_file(
                    str(self.targets[index]),
                    # A single non-default exclusion so any cross-thread state is visible.
                    SecurityConfig(ignored_threat_categories=["OnlyThisOne"]),
                )
                with lock:
                    results[index] = clean
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=_worker, args=(i,), daemon=True)
            for i in range(self.THREADS)
        ]
        with patch.object(security, "find_windows_defender_path", return_value="MpCmdRun.exe"), \
             patch.object(subprocess, "run", side_effect=_scanner):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
                self.assertFalse(thread.is_alive(), "a scan thread hung")

        self.assertEqual(errors, [], f"scan threads raised: {errors!r}")
        self.assertEqual(len(results), self.THREADS, "every thread must finish")
        for index, clean in results.items():
            self.assertEqual(clean, index % 2 == 1, f"thread {index} saw the wrong verdict")
        self.assertEqual(len(calls), self.THREADS, "no scanner invocation may be lost")
        self.assertEqual(
            len(set(calls)), self.THREADS,
            "each thread must have scanned its own file, not a shared one",
        )

    def test_concurrent_scans_never_raise_out_of_the_thread(self):
        """A scanner that fails for every thread must be contained in each thread.

        ``scan_file`` catches its own errors, so a crashing scanner turns into a "skipped
        scan" verdict - the manager runs these on daemon threads, where an uncaught
        exception would be lost silently and the download would look unscanned.
        """
        start = threading.Barrier(self.THREADS, timeout=10)
        verdicts: list = []
        lock = threading.Lock()

        def _worker(index: int):
            start.wait()
            clean, detail = scan_file(str(self.targets[index]), SecurityConfig())
            with lock:
                verdicts.append((clean, detail))

        threads = [
            threading.Thread(target=_worker, args=(i,), daemon=True)
            for i in range(self.THREADS)
        ]
        with patch.object(security, "find_windows_defender_path", return_value="MpCmdRun.exe"), \
             patch.object(subprocess, "run", side_effect=OSError("scanner vanished")):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
                self.assertFalse(thread.is_alive())

        self.assertEqual(len(verdicts), self.THREADS)
        for verdict, detail in verdicts:
            # A scanner that crashed on every thread establishes nothing, so every thread must
            # report "no verdict" rather than the clean result this used to assert.
            self.assertIsNone(verdict)
            self.assertNotEqual(verdict, False)
            self.assertIn("could not be executed", detail)


# ---------------------------------------------------------------------------
# Defender discovery and quarantine
# ---------------------------------------------------------------------------

class TestDefenderDiscovery(unittest.TestCase):
    """``find_windows_defender_path`` and its three lookup strategies."""

    def test_a_known_install_path_wins_without_walking_the_platform_dir(self):
        with patch.object(security.os.path, "isfile", return_value=True), \
             patch.object(security.os.path, "isdir") as isdir:
            found = find_windows_defender_path()
        self.assertTrue(found.lower().endswith("mpcmdrun.exe"))
        isdir.assert_not_called()

    def test_the_platform_directory_is_walked_when_no_fixed_path_exists(self):
        walked = r"C:\ProgramData\Platform\MpCmdRun.exe"
        with patch.object(security.os.path, "isfile",
                          side_effect=lambda p: p == walked), \
             patch.object(security.os.path, "isdir", return_value=True), \
             patch.object(security.os, "walk",
                          return_value=[(r"C:\ProgramData\Platform", [], ["MpCmdRun.exe"])]), \
             patch.object(security.shutil, "which", return_value=None):
            found = find_windows_defender_path()
        self.assertEqual(
            found.replace("\\", "/"), "C:/ProgramData/Platform/MpCmdRun.exe",
            "the versioned Platform directory must win when no fixed path exists",
        )

    def test_the_newest_platform_build_wins(self):
        """Defender must be chosen by version, not by filesystem enumeration order."""
        entries = [
            (r"C:\ProgramData\Platform\4.18.1", [], ["MpCmdRun.exe"]),
            (r"C:\ProgramData\Platform\4.20.2", [], ["MpCmdRun.exe"]),
        ]
        with patch.object(security.os.path, "isfile",
                          side_effect=lambda p: "\\Platform" in p), \
             patch.object(security.os.path, "isdir", return_value=True), \
             patch.object(security.os, "walk", return_value=entries):
            found = find_windows_defender_path()
        self.assertIn("4.20.2", found.replace("\\", "/"))

    def test_a_multi_digit_version_orders_numerically_not_lexically(self):
        """Regression: "4.9.0" is newer than "4.18.1" but sorts before it as text."""
        entries = [
            (r"C:\ProgramData\Platform\4.18.1", [], ["MpCmdRun.exe"]),
            (r"C:\ProgramData\Platform\4.9.0", [], ["MpCmdRun.exe"]),
        ]
        with patch.object(security.os.path, "isfile",
                          side_effect=lambda p: "\\Platform" in p), \
             patch.object(security.os.path, "isdir", return_value=True), \
             patch.object(security.os, "walk", return_value=entries):
            found = find_windows_defender_path()
        self.assertIn(
            "4.18.1", found.replace("\\", "/"),
            "versions must be compared component-wise as integers, not as strings",
        )

    def test_a_single_candidate_still_wins(self):
        entries = [(r"C:\ProgramData\Platform\4.20.2", [], ["MpCmdRun.exe"])]
        with patch.object(security.os.path, "isfile",
                          side_effect=lambda p: "\\Platform" in p), \
             patch.object(security.os.path, "isdir", return_value=True), \
             patch.object(security.os, "walk", return_value=entries):
            self.assertIn("4.20.2", find_windows_defender_path())

    def test_a_non_numeric_directory_name_does_not_break_the_search(self):
        entries = [
            (r"C:\ProgramData\Platform\Latest", [], ["MpCmdRun.exe"]),
            (r"C:\ProgramData\Platform\4.20.2", [], ["MpCmdRun.exe"]),
        ]
        with patch.object(security.os.path, "isfile",
                          side_effect=lambda p: "\\Platform" in p), \
             patch.object(security.os.path, "isdir", return_value=True), \
             patch.object(security.os, "walk", return_value=entries):
            found = find_windows_defender_path()
        self.assertIn("4.20.2", found.replace("\\", "/"),
                      "a 'Latest' style directory must not outrank a real version")

    def test_a_failing_platform_walk_falls_back_to_path_lookup(self):
        with patch.object(security.os.path, "isfile", return_value=False), \
             patch.object(security.os.path, "isdir", return_value=True), \
             patch.object(security.os, "walk", side_effect=OSError("access denied")), \
             patch.object(security.shutil, "which", return_value="C:/defender/MpCmdRun.exe"):
            self.assertEqual(find_windows_defender_path(), "C:/defender/MpCmdRun.exe")

    def test_no_defender_anywhere_returns_none(self):
        with patch.object(security.os.path, "isfile", return_value=False), \
             patch.object(security.os.path, "isdir", return_value=False), \
             patch.object(security.shutil, "which", return_value=None):
            self.assertIsNone(find_windows_defender_path())

    def test_the_lowercase_which_name_is_also_tried(self):
        with patch.object(security.os.path, "isfile", return_value=False), \
             patch.object(security.os.path, "isdir", return_value=False), \
             patch.object(security.shutil, "which",
                          side_effect=lambda name: "mpcmdrun" if name == "mpcmdrun" else None):
            self.assertEqual(find_windows_defender_path(), "mpcmdrun")


class TestQuarantine(unittest.TestCase):
    """``quarantine_or_delete_file`` for both target kinds and every failure mode.

    Every test here runs with the working directory moved into the per-test temp tree.
    That is not paranoia: ``quarantine_or_delete_file("")`` resolves to ``Path("")``, which
    is ``Path(".")`` - the *current working directory* - and then ``shutil.rmtree``s it. The
    first version of ``test_an_empty_path_reports_failure`` ran against the repository root
    and deleted its contents. The guard below makes that class of accident impossible.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

        self._cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._cwd)

    def test_a_file_is_deleted(self):
        target = self.root / "bad.bin"
        target.write_bytes(b"x")
        self.assertTrue(quarantine_or_delete_file(str(target)))
        self.assertFalse(target.exists())

    def test_a_directory_tree_is_removed(self):
        target = self.root / "bad-dir"
        (target / "nested").mkdir(parents=True)
        (target / "nested" / "f.bin").write_bytes(b"x")
        self.assertTrue(quarantine_or_delete_file(str(target)))
        self.assertFalse(target.exists())

    def test_a_missing_target_reports_failure(self):
        self.assertFalse(quarantine_or_delete_file(str(self.root / "never-existed")))

    def test_an_empty_path_is_refused_and_the_cwd_survives(self):
        """Regression test: a blank path must never resolve to the working directory.

        ``quarantine_or_delete_file`` did ``Path(file_path)`` with no guard, and
        ``Path("")`` is ``Path(".")`` - the process's *current working directory*. It then
        ``shutil.rmtree``d it, so an empty ``file_path`` - an unresolved download row, a
        failed path probe returning "", anything that can hand the antivirus handler a blank
        string - made the app recursively delete its own launch directory with no prompt and
        no undo. On Windows the CWD is wherever the app was started from.

        The call returned ``False`` either way, because removing "." itself fails with a
        sharing violation; everything *inside* it was already gone by then. The canary is
        therefore the assertion, not the return value.
        """
        canary = self.root / "canary.txt"
        canary.write_text("x", encoding="utf-8")
        (self.root / "victim.bin").write_bytes(b"x")

        self.assertFalse(quarantine_or_delete_file(""))
        self.assertTrue(canary.exists(), "a blank path must not touch the working directory")
        self.assertTrue((self.root / "victim.bin").exists())

    def test_a_whitespace_only_path_is_refused(self):
        canary = self.root / "canary.txt"
        canary.write_text("x", encoding="utf-8")
        self.assertFalse(quarantine_or_delete_file("   "))
        self.assertTrue(canary.exists())

    def test_a_current_directory_path_is_refused(self):
        """"." and "./" name the CWD just as much as "" does."""
        canary = self.root / "canary.txt"
        canary.write_text("x", encoding="utf-8")
        for path in (".", "./", str(self.root / "sub" / "..") if False else "."):
            with self.subTest(path=path):
                self.assertFalse(quarantine_or_delete_file(path))
                self.assertTrue(canary.exists())

    def test_a_real_file_still_gets_deleted(self):
        """The guard must not have neutered the function it protects."""
        target = self.root / "bad.bin"
        target.write_bytes(b"x")
        self.assertTrue(quarantine_or_delete_file(str(target)))
        self.assertFalse(target.exists())

    def test_a_relative_path_is_resolved_against_the_working_directory(self):
        """A relative path must not escape into the CWD either."""
        canary = self.root / "canary.txt"
        canary.write_text("x", encoding="utf-8")
        (self.root / "target.bin").write_bytes(b"x")

        self.assertTrue(quarantine_or_delete_file("target.bin"))
        self.assertTrue(canary.exists(), "only the named file may be removed")
        self.assertFalse((self.root / "target.bin").exists())

    def test_a_failing_delete_reports_failure_and_does_not_raise(self):
        target = self.root / "locked.bin"
        target.write_bytes(b"x")
        with patch.object(Path, "unlink", side_effect=PermissionError("in use")):
            self.assertFalse(quarantine_or_delete_file(str(target)))
        self.assertTrue(target.exists(), "a failed delete must leave the file in place")

    def test_a_failing_tree_delete_reports_failure_and_leaves_the_tree(self):
        target = self.root / "locked-dir"
        (target / "nested").mkdir(parents=True)
        (target / "nested" / "f.bin").write_bytes(b"x")
        with patch("shutil.rmtree", side_effect=PermissionError("in use")):
            self.assertFalse(quarantine_or_delete_file(str(target)))
        self.assertTrue((target / "nested" / "f.bin").exists())

    def test_a_failing_delete_reports_failure_and_does_not_raise(self):
        target = self.root / "locked.bin"
        target.write_bytes(b"x")
        with patch.object(Path, "unlink", side_effect=PermissionError("in use")):
            self.assertFalse(quarantine_or_delete_file(str(target)))
        self.assertTrue(target.exists(), "a failed delete must leave the file in place")


# ---------------------------------------------------------------------------
# external_tools
# ---------------------------------------------------------------------------

class TestAnimePaheArgumentAssembly(unittest.TestCase):
    """The CLI argv ``launch_animepahe_cli`` builds.

    The ``Auto`` sentinels matter: they must be omitted entirely, because passing
    ``-q auto`` to the scraper is a different request from "no preference".
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "animepahe-downloader"
        self.repo.mkdir()
        (self.repo / "animepahe_download.py").write_text("# stub\n")
        self.config = ExternalToolsConfig(animepahe_repo_path=str(self.repo))

    def _launch(self, **kw):
        with patch.object(subprocess, "Popen") as popen:
            ok, message, proc = external_tools.launch_animepahe_cli(self.config, **kw)
        return ok, message, proc, (popen.call_args.args[0] if popen.call_args else None)

    def test_the_bare_command_is_unconditional_minus_y(self):
        _, _, _, cmd = self._launch()
        self.assertEqual(cmd[1:], ["-u", str(self.repo / "animepahe_download.py"),
                                   "--my-idm", "-y"])
        self.assertTrue(cmd[0].endswith("python.exe") or cmd[0].endswith("python"))

    def test_auto_quality_and_language_are_omitted(self):
        _, _, _, cmd = self._launch(quality="Auto", lang="Auto")
        self.assertNotIn("-q", cmd)
        self.assertNotIn("-l", cmd)

    def test_an_explicit_quality_is_forwarded(self):
        _, _, _, cmd = self._launch(quality="1080")
        self.assertEqual(cmd[cmd.index("-q") + 1], "1080")

    def test_subtitled_languages_map_to_jap(self):
        for lang in ("Sub", "Japanese", "JPN", "Auto"):
            with self.subTest(lang=lang):
                _, _, _, cmd = self._launch(lang=lang)
                if lang == "Auto":
                    self.assertNotIn("-l", cmd, "Auto must be omitted entirely")
                else:
                    self.assertEqual(cmd[cmd.index("-l") + 1], "jap", lang)

    def test_a_dub_language_maps_to_en(self):
        for lang in ("English Dub", "en", "Auto Dub", "DUB"):
            with self.subTest(lang=lang):
                _, _, _, cmd = self._launch(lang=lang)
                self.assertEqual(cmd[cmd.index("-l") + 1], "en", lang)

    def test_episode_ranges_lose_their_whitespace(self):
        _, _, _, cmd = self._launch(episodes=" 1 - 12 ")
        self.assertEqual(cmd[cmd.index("-ep") + 1], "1-12")

    def test_a_url_is_trimmed(self):
        _, _, _, cmd = self._launch(url="  https://animepahe.com/play/x  ")
        self.assertEqual(cmd[cmd.index("--url") + 1], "https://animepahe.com/play/x")

    def test_extra_args_are_appended_after_y(self):
        _, _, _, cmd = self._launch(extra_args=["--verbose", "--limit=5"])
        self.assertEqual(cmd[-2:], ["--verbose", "--limit=5"])
        self.assertLess(cmd.index("-y"), cmd.index("--verbose"))

    def test_the_my_idm_directory_is_forwarded(self):
        _, _, _, cmd = self._launch(my_idm_dir="C:/app")
        self.assertEqual(cmd[cmd.index("--my-idm-dir") + 1], "C:/app")

    def test_the_console_log_records_the_command(self):
        log_path = self.config.get_console_log_path()
        with patch.object(subprocess, "Popen"):
            external_tools.launch_animepahe_cli(self.config, url="https://a/b")
        text = Path(log_path).read_text(encoding="utf-8")
        self.assertIn("Session Started", text)
        self.assertIn("https://a/b", text)

    def test_the_child_is_started_unbuffered_and_detached_from_stdin(self):
        with patch.object(subprocess, "Popen") as popen:
            external_tools.launch_animepahe_cli(self.config)
        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["stderr"], subprocess.STDOUT)
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["env"]["PYTHONUNBUFFERED"], "1")
        self.assertEqual(kwargs["cwd"], str(self.repo).replace("\\", "/"))

    def test_the_embed_container_handle_reaches_the_child_environment(self):
        with patch.object(subprocess, "Popen") as popen:
            external_tools.launch_animepahe_cli(self.config, container_hwnd=4242)
        self.assertEqual(
            popen.call_args.kwargs["env"]["ANIMEPAHE_EMBED_CONTAINER_HWND"], "4242"
        )

    def test_no_container_handle_means_no_environment_variable(self):
        with patch.object(subprocess, "Popen") as popen:
            external_tools.launch_animepahe_cli(self.config)
        self.assertNotIn("ANIMEPAHE_EMBED_CONTAINER_HWND", popen.call_args.kwargs["env"])

    def test_a_launch_failure_is_reported_not_raised(self):
        with patch.object(subprocess, "Popen", side_effect=OSError("no such file")):
            ok, message, proc = external_tools.launch_animepahe_cli(self.config)
        self.assertFalse(ok)
        self.assertIsNone(proc)
        self.assertIn("Failed to launch", message)

    def test_a_repo_without_the_script_is_rejected(self):
        (self.repo / "animepahe_download.py").unlink()
        ok, message, proc = external_tools.launch_animepahe_cli(self.config)
        self.assertFalse(ok)
        self.assertIsNone(proc)
        self.assertIn("animepahe_download.py", message)

    def test_a_nonexistent_repo_is_rejected(self):
        self.config.animepahe_repo_path = str(self.repo / "gone")
        ok, message, proc = external_tools.launch_animepahe_cli(self.config)
        self.assertFalse(ok)
        self.assertIsNone(proc)
        self.assertIn("does not exist", message)

    def test_an_unset_repo_path_is_rejected_when_nothing_is_auto_detected(self):
        """Clearing the field falls back to hard-coded auto-detection, not to "no repo".

        ``ExternalToolsConfig.get_effective_repo_path`` probes ``D:\\Projects\\animepahe-downloader``
        and ``~/Projects/animepahe-downloader`` when the setting is empty, so a user who
        clears the field does not necessarily get an error - they silently get a checkout
        that happens to exist on this machine.

        The probe is stubbed on ``Path.is_dir``, which is what the implementation actually
        calls. It used to patch ``external_tools.os.path.isdir``, which this code path never
        touches, so the test really asserted "no ``D:\\Projects\\animepahe-downloader`` on this
        machine" and passed on the runner by luck.
        """
        self.config.animepahe_repo_path = ""
        with patch.object(Path, "is_dir", return_value=False):
            ok, message, proc = external_tools.launch_animepahe_cli(self.config)
        self.assertFalse(ok)
        self.assertIsNone(proc)
        self.assertIn("does not exist", message)

    def test_an_unset_repo_path_auto_detects_a_hard_coded_checkout(self):
        """Documents the gotcha: an empty setting activates a machine-specific path.

        ``get_effective_repo_path`` probes ``D:\\Projects\\animepahe-downloader`` first when
        the setting is empty. Clearing the field therefore does not mean "disabled" - it
        means "use whatever the developer happened to have at D:\\Projects", which on the
        author's own machine silently wires the scraper to a checkout the user never chose.

        Both candidates are stubbed as present, so the assertion is about which one wins and
        not about what happens to be on the disk. Unstubbed, this only passed on a machine
        that really has that checkout.
        """
        self.config.animepahe_repo_path = ""
        with patch.object(Path, "is_dir", return_value=True), \
             patch.object(Path, "is_file", return_value=True):
            effective = self.config.get_effective_repo_path()
        self.assertEqual(
            effective, "D:/Projects/animepahe-downloader",
            "KNOWN GOTCHA: clearing animepahe_repo_path falls back to a hard-coded "
            "D:\\Projects path, so the tool can bind to a machine-local checkout",
        )


class TestAnimePaheGuiLaunch(unittest.TestCase):
    """The four GUI entry points, in priority order."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        self.config = ExternalToolsConfig(animepahe_repo_path=str(self.repo))

    def _entry(self, name: str) -> Path:
        path = self.repo / name
        path.write_text("# stub\n")
        return path

    def test_run_pyw_wins_over_every_other_entry_point(self):
        self._entry("run.pyw")
        self._entry("gui.py")
        self._entry("run_gui.bat")
        self._entry("animepahe_download.py")
        with patch.object(subprocess, "Popen") as popen:
            ok, message = external_tools.launch_animepahe_gui(self.config)
        self.assertTrue(ok)
        self.assertIn("run.pyw", message)
        self.assertIn("run.pyw", str(popen.call_args.args[0]))

    def test_gui_py_is_the_second_choice(self):
        self._entry("gui.py")
        self._entry("animepahe_download.py")
        with patch.object(subprocess, "Popen"):
            ok, message = external_tools.launch_animepahe_gui(self.config)
        self.assertTrue(ok)
        self.assertIn("gui.py", message)

    def test_run_bat_is_launched_through_a_shell(self):
        self._entry("run_gui.bat")
        with patch.object(subprocess, "Popen") as popen:
            ok, message = external_tools.launch_animepahe_gui(self.config)
        self.assertTrue(ok)
        self.assertIn("run_gui.bat", message)
        self.assertTrue(popen.call_args.kwargs.get("shell"))
        self.assertIn("cmd.exe", popen.call_args.args[0])

    def test_the_scraper_gui_fallback_is_the_last_resort(self):
        self._entry("animepahe_download.py")
        with patch.object(subprocess, "Popen") as popen:
            ok, message = external_tools.launch_animepahe_gui(self.config)
        self.assertTrue(ok)
        self.assertIn("--gui", popen.call_args.args[0])

    def test_a_repo_with_no_entry_point_reports_the_expectations(self):
        ok, message = external_tools.launch_animepahe_gui(self.config)
        self.assertFalse(ok)
        self.assertIn("run.pyw", message)
        self.assertIn("gui.py", message)

    def test_a_launch_failure_is_reported_not_raised(self):
        self._entry("run.pyw")
        with patch.object(subprocess, "Popen", side_effect=OSError("denied")):
            ok, message = external_tools.launch_animepahe_gui(self.config)
        self.assertFalse(ok)
        self.assertIn("Failed to launch", message)

    def test_a_missing_repo_is_rejected(self):
        self.config.animepahe_repo_path = str(self.repo / "gone")
        ok, message = external_tools.launch_animepahe_gui(self.config)
        self.assertFalse(ok)
        self.assertIn("does not exist", message)

    def test_the_gui_process_is_fully_detached(self):
        self._entry("run.pyw")
        with patch.object(subprocess, "Popen") as popen:
            external_tools.launch_animepahe_gui(self.config)
        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)


class TestPythonwDiscovery(unittest.TestCase):
    """``find_pythonw_executable`` must never return a path that does not exist."""

    def test_pythonw_is_preferred_when_present(self):
        with patch.object(Path, "is_file", return_value=True):
            found = external_tools.find_pythonw_executable()
        self.assertTrue(found.lower().endswith("pythonw.exe"))

    def test_the_interpreter_itself_is_the_fallback(self):
        with patch.object(Path, "is_file", return_value=False):
            self.assertEqual(
                external_tools.find_pythonw_executable(), sys.executable
            )


@unittest.skipUnless(sys.platform == "win32", "requires ctypes.windll, which only exists on Windows")
class TestChildPidEnumeration(unittest.TestCase):
    """``get_child_pids`` snapshot handling and ``find_chrome_hwnd`` window matching.

    ``get_child_pids`` drives a real ``ctypes`` Win32 walk, so only its *contract* is
    tested here: which failure paths are contained, and that the snapshot handle is
    released. The recursion itself is exercised indirectly by
    :class:`TestChromeWindowMatching`, which feeds the window matcher a controlled
    ``GetWindowRect`` and therefore the real callback.

    Windows-only, and unavoidably so: the fakes are installed *onto* ``ctypes.windll``, which
    does not exist on POSIX, so ``patch("ctypes.windll.kernel32", ...)`` raised
    ``AttributeError: module 'ctypes' has no attribute 'windll'`` there. Simulating win32 is not
    an option either - a fake DLL cannot be attached to an attribute the platform does not have.
    """

    def setUp(self):
        self.kernel32 = MagicMock()
        # Never let a mocked enumeration enter the `while True` walk: a MagicMock is
        # truthy for both Process32FirstW and Process32NextW, which would spin forever.
        self.kernel32.CreateToolhelp32Snapshot.return_value = 77
        self.kernel32.Process32FirstW.return_value = False

    def test_no_children_on_a_non_windows_platform(self):
        with patch.object(external_tools.sys, "platform", "linux"):
            self.assertEqual(external_tools.get_child_pids(1234), set())
            self.assertIsNone(external_tools.find_chrome_hwnd(1234))

    def test_embedded_browser_support_follows_the_platform(self):
        """The capability flag and the finder must never disagree.

        The panel surfaces `embedded_browser_supported()` to the user as the reason the Embedded
        Browser tab never appears, while `find_chrome_hwnd` is what actually decides whether it
        attaches. If those two ever diverged, the panel would promise a tab that silently fails
        to populate - the exact defect the flag was added to remove.
        """
        for platform, expected in (("win32", True), ("linux", False), ("darwin", False)):
            with self.subTest(platform=platform):
                with patch.object(external_tools.sys, "platform", platform):
                    self.assertIs(external_tools.embedded_browser_supported(), expected)

        # The direction that matters: where the feature is unsupported the finder must
        # short-circuit, never reach Win32 enumeration. (The reverse does not hold as an equality
        # - on Windows the finder returns None simply when no matching window exists.)
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform):
                with patch.object(external_tools.sys, "platform", platform):
                    self.assertFalse(external_tools.embedded_browser_supported())
                    self.assertIsNone(external_tools.find_chrome_hwnd(1234))

    def test_a_failed_snapshot_is_an_empty_set(self):
        self.kernel32.CreateToolhelp32Snapshot.return_value = -1
        with patch.object(external_tools.sys, "platform", "win32"), \
             patch("ctypes.windll.kernel32", self.kernel32):
            self.assertEqual(external_tools.get_child_pids(1234), set())
        self.kernel32.CloseHandle.assert_not_called()

    def test_a_failing_snapshot_call_is_contained(self):
        self.kernel32.CreateToolhelp32Snapshot.side_effect = OSError("access denied")
        with patch.object(external_tools.sys, "platform", "win32"), \
             patch("ctypes.windll.kernel32", self.kernel32):
            self.assertEqual(external_tools.get_child_pids(1234), set())

    def test_a_failing_first_entry_call_is_contained(self):
        self.kernel32.Process32FirstW.side_effect = OSError("denied")
        with patch.object(external_tools.sys, "platform", "win32"), \
             patch("ctypes.windll.kernel32", self.kernel32):
            self.assertEqual(external_tools.get_child_pids(1234), set())

    def test_an_empty_snapshot_closes_its_handle(self):
        """A leaked snapshot handle leaks kernel memory on every monitor tick."""
        with patch.object(external_tools.sys, "platform", "win32"), \
             patch("ctypes.windll.kernel32", self.kernel32):
            external_tools.get_child_pids(1234)
        self.kernel32.CloseHandle.assert_called_once_with(77)

    def test_a_failing_enumeration_leaks_the_snapshot_handle_known_limitation(self):
        """Documents a real defect rather than blessing it.

        ``CloseHandle(hSnapshot)`` sits after the walk, not in a ``finally``. Any exception
        raised inside the loop - which the surrounding ``except Exception`` is explicitly
        there to swallow - returns with the handle still open. The manager calls
        :func:`get_child_pids` from a periodic monitor tick, so a transient enumeration
        failure leaks a handle per tick. Moving the call into a ``finally`` (or using
        ``with``) would close it.
        """
        self.kernel32.Process32FirstW.side_effect = OSError("transient failure")
        with patch.object(external_tools.sys, "platform", "win32"), \
             patch("ctypes.windll.kernel32", self.kernel32):
            self.assertEqual(external_tools.get_child_pids(1234), set())
        self.kernel32.CloseHandle.assert_not_called()

    def test_a_non_positive_parent_pid_is_not_looked_up(self):
        with patch.object(external_tools.sys, "platform", "win32"), \
             patch("ctypes.windll.kernel32", self.kernel32):
            self.assertEqual(external_tools.get_child_pids(0), set())


@unittest.skipUnless(sys.platform == "win32", "requires ctypes.windll, which only exists on Windows")
class TestChromeWindowMatching(unittest.TestCase):
    """The ``find_chrome_hwnd`` predicate, driven through the real Win32 callback.

    ``EnumWindows`` is faked so that it actually *invokes* the callback with a controlled
    ``GetClassNameW`` / ``GetWindowRect`` / ``GetWindowThreadProcessId``. That makes the
    class-name, size-floor and pid-scoping rules assertable without a real browser, and it
    exercises the genuine ``WNDENUMPROC`` early-exit path. The fakes hang off
    ``ctypes.windll.user32``, so this class is Windows-only.
    """

    HWND = 0x1234

    def _user32(self, class_name, width, height, pid=4242, visited=None):
        user32 = MagicMock()

        def _class_name(hwnd, buf, count):
            buf.value = class_name
            return len(class_name)

        def _thread_pid(hwnd, out):
            ctypes.cast(out, ctypes.POINTER(ctypes.c_ulong))[0] = pid
            return 1

        def _rect(hwnd, rect):
            target = ctypes.cast(rect, ctypes.POINTER(ctypes.c_long * 4))[0]
            target[0], target[1] = 0, 0
            target[2], target[3] = width, height
            return 1

        def _enum(proc, lparam):
            # One real invocation, exactly as Win32 would do.
            keep_going = proc(self.HWND, lparam)
            if visited is not None:
                visited.append(bool(keep_going))
            return 1 if keep_going else 0

        user32.GetClassNameW.side_effect = _class_name
        user32.GetWindowThreadProcessId.side_effect = _thread_pid
        user32.GetWindowRect.side_effect = _rect
        user32.EnumWindows.side_effect = _enum
        return user32

    def _run(self, user32):
        with patch.object(external_tools.sys, "platform", "win32"), \
             patch("ctypes.windll.user32", user32), \
             patch.object(external_tools, "get_child_pids", return_value=set()):
            return external_tools.find_chrome_hwnd(4242)

    def test_a_large_chrome_window_belonging_to_the_tree_is_claimed(self):
        visited: list[bool] = []
        user32 = self._user32("Chrome_WidgetWin_1", 800, 600, visited=visited)
        self.assertEqual(self._run(user32), self.HWND)
        self.assertEqual(visited, [False], "the callback must stop the enumeration on a hit")

    def test_a_window_below_the_size_floor_is_not_claimed(self):
        """The 200x150 floor exists so a splash or tooltip is not mistaken for the browser."""
        user32 = self._user32("Chrome_WidgetWin_1", 100, 50)
        self.assertIsNone(self._run(user32), "a 100x50 window is below the 200x150 floor")

    def test_the_size_floor_is_inclusive(self):
        """Regression: the guard used ``>``, so exactly 200x150 - the documented
        minimum - was silently skipped. A small-but-genuine window was never adopted."""
        user32 = self._user32("Chrome_WidgetWin_1", 200, 150)
        self.assertEqual(self._run(user32), self.HWND)

    def test_one_pixel_below_the_floor_is_not_claimed(self):
        user32 = self._user32("Chrome_WidgetWin_1", 199, 150)
        self.assertIsNone(self._run(user32))
        user32 = self._user32("Chrome_WidgetWin_1", 200, 149)
        self.assertIsNone(self._run(user32))

    def test_a_window_of_a_different_class_is_ignored(self):
        user32 = self._user32("MozillaWindowClass", 800, 600)
        self.assertIsNone(self._run(user32))

    def test_a_window_outside_the_requested_process_tree_is_ignored(self):
        user32 = self._user32("Chrome_WidgetWin_1", 800, 600, pid=9999)
        with patch.object(external_tools.sys, "platform", "win32"), \
             patch("ctypes.windll.user32", user32), \
             patch.object(external_tools, "get_child_pids", return_value={4242}):
            self.assertIsNone(
                external_tools.find_chrome_hwnd(4242),
                "a Chrome window owned by an unrelated pid must not be adopted",
            )

    def test_the_parent_pid_itself_is_always_in_scope(self):
        user32 = self._user32("Chrome_WidgetWin_1", 800, 600, pid=4242)
        with patch.object(external_tools.sys, "platform", "win32"), \
             patch("ctypes.windll.user32", user32), \
             patch.object(external_tools, "get_child_pids", return_value=set()):
            self.assertEqual(external_tools.find_chrome_hwnd(4242), self.HWND)

    def test_no_parent_pid_matches_any_chrome_window(self):
        user32 = self._user32("Chrome_WidgetWin_1", 800, 600)
        with patch.object(external_tools.sys, "platform", "win32"), \
             patch("ctypes.windll.user32", user32):
            self.assertEqual(
                external_tools.find_chrome_hwnd(None), self.HWND,
                "without a parent pid the pid filter is skipped entirely",
            )

    def test_a_failing_enumeration_returns_none_rather_than_raising(self):
        user32 = MagicMock()
        user32.EnumWindows.side_effect = OSError("no window station")
        with patch.object(external_tools.sys, "platform", "win32"), \
             patch("ctypes.windll.user32", user32), \
             patch.object(external_tools, "get_child_pids", return_value=set()):
            self.assertIsNone(external_tools.find_chrome_hwnd(4242))


class TestOpenAndRevealFiles(unittest.TestCase):
    """``open_file_in_default_app`` and ``show_in_folder`` failure paths."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_a_missing_file_is_created_when_asked(self):
        target = self.root / "sub" / "console.log"
        with patch.object(external_tools.QDesktopServices, "openUrl", return_value=True):
            ok, message = external_tools.open_file_in_default_app(target)
        self.assertTrue(ok)
        self.assertTrue(target.exists(), "the placeholder must have been written")
        self.assertIn("Log file created", target.read_text(encoding="utf-8"))

    def test_a_missing_file_is_not_created_when_not_asked(self):
        target = self.root / "sub" / "console.log"
        with patch.object(external_tools.QDesktopServices, "openUrl") as open_url:
            ok, message = external_tools.open_file_in_default_app(
                target, create_if_missing=False
            )
        self.assertFalse(ok)
        self.assertIn("does not exist", message)
        open_url.assert_not_called()
        self.assertFalse(target.exists())

    def test_an_uncreatable_placeholder_reports_the_error(self):
        blocker = self.root / "blocker"
        blocker.write_bytes(b"x")
        with patch.object(external_tools.Path, "mkdir",
                          side_effect=PermissionError("read-only volume")):
            ok, message = external_tools.open_file_in_default_app(blocker / "a.log")
        self.assertFalse(ok)
        self.assertIn("Could not create", message)

    def test_a_directory_is_opened(self):
        """A folder reaches the shell, not the Qt desktop services.

        Both launchers have to be fenced: on Windows ``open_file_in_default_app`` calls
        ``os.startfile(str(p))`` *before* it ever touches ``QDesktopServices``, so patching
        only Qt lets a real Explorer window open on the developer's desktop. The
        startfile-first behaviour itself is asserted separately in
        :meth:`test_a_directory_opens_through_startfile_on_windows`.

        The platform is pinned to win32 because that is the branch under test; ``create=True``
        is needed for the ``startfile`` patch to apply at all off Windows, where the attribute
        does not exist.
        """
        folder = self.root / "folder"
        folder.mkdir()
        with patch.object(external_tools.sys, "platform", "win32"), \
             patch.object(external_tools, "QDesktopServices") as services, \
             patch.object(external_tools.os, "startfile", create=True) as startfile:
            services.openUrl.return_value = True
            ok, message = external_tools.open_file_in_default_app(folder)
        self.assertTrue(ok, message)
        self.assertIn("folder", message)
        services.openUrl.assert_not_called()
        startfile.assert_called_once_with(str(folder.resolve()))

    def test_a_directory_that_cannot_be_opened_reports_the_error(self):
        # Pinned to win32 so the startfile branch is the one that fails. Left on the host
        # platform, this reached the real `QDesktopServices.openUrl` on Linux and macOS, and
        # conftest's hermeticity guard recorded the violation - which then surfaced as an
        # ERROR at the teardown of an unrelated later test.
        folder = self.root / "folder"
        folder.mkdir()
        with patch.object(external_tools.sys, "platform", "win32"), \
             patch.object(external_tools.os, "startfile", create=True,
                          side_effect=OSError("no shell")):
            ok, message = external_tools.open_file_in_default_app(folder)
        self.assertFalse(ok)
        self.assertIn("Failed to open folder", message)

    def test_a_directory_opens_through_startfile_on_windows(self):
        """Windows goes through ``os.startfile``, bypassing Qt's desktop services."""
        folder = self.root / "folder"
        folder.mkdir()
        with patch.object(external_tools.sys, "platform", "win32"), \
             patch.object(external_tools, "QDesktopServices") as services, \
             patch.object(external_tools.os, "startfile", create=True) as startfile:
            ok, message = external_tools.open_file_in_default_app(folder)
        self.assertTrue(ok)
        services.openUrl.assert_not_called()
        startfile.assert_called_once_with(str(folder.resolve()))

    def test_an_existing_file_that_cannot_be_opened_reports_the_error(self):
        target = self.root / "a.bin"
        target.write_bytes(b"x")
        with patch.object(external_tools, "QDesktopServices") as services:
            services.openUrl.return_value = False
            with patch.object(external_tools.sys, "platform", "linux"):
                ok, message = external_tools.open_file_in_default_app(target)
        self.assertFalse(ok)
        self.assertIn("Failed to open", message)

    def test_windows_falls_back_to_startfile_when_openurl_refuses(self):
        target = self.root / "a.bin"
        target.write_bytes(b"x")
        with patch.object(external_tools.sys, "platform", "win32"), \
             patch.object(external_tools, "QDesktopServices") as services, \
             patch.object(external_tools.os, "startfile", create=True) as startfile:
            services.openUrl.return_value = False
            ok, _ = external_tools.open_file_in_default_app(target)
        self.assertTrue(ok, "openUrl refusing must not fail the request on Windows")
        startfile.assert_called_once()

    def test_show_in_folder_falls_back_to_the_parent_for_a_missing_path(self):
        """A vanished file must still open its containing folder rather than fail."""
        target = self.root / "a.bin"
        with patch.object(external_tools, "QDesktopServices") as services, \
             patch.object(external_tools.sys, "platform", "linux"):
            services.openUrl.return_value = True
            ok, _ = external_tools.show_in_folder(target)
        self.assertTrue(ok)
        services.openUrl.assert_called_once()
        self.assertEqual(
            services.openUrl.call_args.args[0].toString(),
            QUrl.fromLocalFile(str(self.root.resolve())).toString(),
            "a missing file must resolve to its containing folder",
        )

    def test_show_in_folder_reports_a_missing_path_with_a_missing_parent(self):
        with patch.object(external_tools, "QDesktopServices"):
            ok, message = external_tools.show_in_folder(self.root / "no" / "such" / "a.bin")
        self.assertFalse(ok)
        self.assertIn("does not exist", message)

    def test_windows_highlights_a_file_rather_than_opening_its_folder(self):
        target = self.root / "a.bin"
        target.write_bytes(b"x")
        with patch.object(external_tools.sys, "platform", "win32"), \
             patch.object(subprocess, "Popen") as popen:
            ok, _ = external_tools.show_in_folder(target)
        self.assertTrue(ok)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[0], "explorer")
        self.assertTrue(argv[1].startswith("/select,"))

    def test_windows_opens_a_folder_directly(self):
        folder = self.root / "folder"
        folder.mkdir()
        with patch.object(external_tools.sys, "platform", "win32"), \
             patch.object(subprocess, "Popen") as popen, \
             patch.object(external_tools.os, "startfile") as startfile:
            ok, _ = external_tools.show_in_folder(folder)
        self.assertTrue(ok)
        popen.assert_not_called()
        startfile.assert_called_once()

    def test_a_failing_reveal_is_reported_not_raised(self):
        target = self.root / "a.bin"
        target.write_bytes(b"x")
        with patch.object(external_tools.sys, "platform", "win32"), \
             patch.object(subprocess, "Popen", side_effect=OSError("explorer missing")):
            ok, message = external_tools.show_in_folder(target)
        self.assertFalse(ok)
        self.assertIn("Failed to open", message)


if __name__ == "__main__":
    unittest.main()
