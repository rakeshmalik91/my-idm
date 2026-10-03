"""Tests for scanner auto-detection and the `scan_file` dispatcher.

The property that matters: with the *default* configuration on a Linux or macOS machine, a scan
must either run ClamAV or say it did not run. Before the dispatcher existed, every default scan on
those platforms reported "not scanned" because only Defender was ever looked for - which is honest
but leaves the feature inert on exactly the platforms it was written for.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from my_idm import security
from my_idm.security import SecurityConfig, find_clamav, scan_file


class TestFindClamav(unittest.TestCase):
    def test_it_finds_clamdscan_first(self):
        """It talks to a running `clamd` and is far faster on large files."""
        def which(name):
            return "/usr/bin/" + name if name in security._CLAMSCAN_EXECUTABLES else None

        with patch.object(security.shutil, "which", side_effect=which):
            exe, args = find_clamav()
        self.assertTrue(exe.endswith("clamdscan"))
        self.assertIn("%file%", args)

    def test_it_falls_back_to_clamscan(self):
        def which(name):
            return "/usr/bin/clamscan" if name == "clamscan" else None

        with patch.object(security.shutil, "which", side_effect=which):
            exe, _ = find_clamav()
        self.assertTrue(exe.endswith("clamscan"))

    def test_it_reports_nothing_when_neither_is_installed(self):
        with patch.object(security.shutil, "which", return_value=None):
            self.assertEqual(find_clamav(), (None, None))

    def test_the_default_arguments_suppress_the_scan_summary(self):
        """My-IDM builds its own report line from the exit code."""
        self.assertEqual(security._CLAMSCAN_ARGS, "--no-summary %file%")


class ScanDispatcherTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = Path(self._tmp.name) / "payload.bin"
        self.target.write_bytes(b"data")
        # _scan_with_custom refuses a scanner that is not a real file, so the stub must be one.
        self.clam = Path(self._tmp.name) / "clamscan"
        self.clam.write_bytes(b"# stub")

    def scan(self, **cfg):
        with patch.object(security.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            run.return_value.stderr = ""
            verdict, report = scan_file(str(self.target), SecurityConfig(**cfg))
        return verdict, report, run


class TestDefenderMeansSystemScannerOffWindows(ScanDispatcherTestCase):
    """`scanner_type == "defender"` is what every stored configuration says.

    The Settings radio is binary - Defender or Custom - so "defender" is the default on every
    platform, including Linux and macOS where Defender cannot exist. Treating it there as "the
    system scanner" is what makes default settings work off Windows at all; without it, every scan
    on those platforms reports "not scanned" no matter how ClamAV is installed.
    """

    def test_linux_default_uses_clamav(self):
        with patch.object(security.sys, "platform", "linux"), \
             patch.object(security, "find_clamav", return_value=(str(self.clam), "--no-summary %file%")), \
             patch.object(security.shutil, "which", return_value=str(self.clam)):
            verdict, report, run = self.scan()  # scanner_type defaults to "defender"
        self.assertTrue(verdict, report)
        self.assertIn("--no-summary", run.call_args.args[0])

    def test_macos_default_uses_clamav(self):
        with patch.object(security.sys, "platform", "darwin"), \
             patch.object(security, "find_clamav", return_value=(str(self.clam), "--no-summary %file%")), \
             patch.object(security.shutil, "which", return_value=str(self.clam)):
            verdict, report, _ = self.scan()
        self.assertTrue(verdict, report)

    def test_linux_default_with_no_clamav_reports_no_verdict(self):
        with patch.object(security.sys, "platform", "linux"), \
             patch.object(security, "find_clamav", return_value=(None, None)):
            verdict, report, _ = self.scan()
        self.assertIsNone(verdict)
        self.assertIn("clamav", report.lower())

    def test_windows_default_still_means_defender(self):
        """The reinterpretation must not change Windows."""
        with patch.object(security.sys, "platform", "win32"), \
             patch.object(security, "find_windows_defender_path", return_value="MpCmdRun.exe"):
            verdict, report, run = self.scan()
        self.assertTrue(verdict, report)
        self.assertIn("-ScanType", run.call_args.args[0])

    def test_linux_default_never_passes_a_defender_argv(self):
        with patch.object(security.sys, "platform", "linux"), \
             patch.object(security, "find_clamav", return_value=(str(self.clam), "--no-summary %file%")), \
             patch.object(security.shutil, "which", return_value=str(self.clam)):
            _, _, run = self.scan()
        self.assertNotIn("-ScanType", run.call_args.args[0])


class TestClamavExitCodes(ScanDispatcherTestCase):
    """clamdscan/clamscan: 0 clean, 1 infected, 2 error."""

    def _verdict_for(self, code):
        with patch.object(security.sys, "platform", "linux"), \
             patch.object(security, "find_clamav", return_value=(str(self.clam), "--no-summary %file%")), \
             patch.object(security.shutil, "which", return_value=str(self.clam)), \
             patch.object(security.subprocess, "run") as run:
            run.return_value.returncode = code
            run.return_value.stdout = "payload.bin: TEST-SIGNATURE FOUND"
            run.return_value.stderr = ""
            return scan_file(str(self.target), SecurityConfig(scanner_type="auto"))

    def test_zero_is_clean(self):
        verdict, report = self._verdict_for(0)
        self.assertTrue(verdict, report)

    def test_one_is_a_threat(self):
        verdict, report = self._verdict_for(1)
        self.assertFalse(verdict)
        self.assertIn("TEST-SIGNATURE", report)

    def test_two_is_not_clean_either(self):
        """An error is not a clean bill of health.

        Indistinguishable from a threat at this layer, which is the safe direction: the file is
        not declared safe. The exclusions still apply, so a user who has allowed a category keeps
        that behaviour.
        """
        verdict, _ = self._verdict_for(2)
        self.assertFalse(verdict)

    def test_an_excluded_signature_is_still_allowed_on_clamav(self):
        with patch.object(security.sys, "platform", "linux"), \
             patch.object(security, "find_clamav", return_value=(str(self.clam), "--no-summary %file%")), \
             patch.object(security.shutil, "which", return_value=str(self.clam)), \
             patch.object(security.subprocess, "run") as run:
            run.return_value.returncode = 1
            run.return_value.stdout = "payload.bin: TEST-SIGNATURE FOUND"
            run.return_value.stderr = ""
            verdict, report = scan_file(
                str(self.target),
                SecurityConfig(
                    scanner_type="auto", ignored_threat_patterns="TEST-SIGNATURE"
                ),
            )
        self.assertTrue(verdict, report)
        self.assertIn("exclusion", report)


if __name__ == "__main__":
    unittest.main()
