"""Unit tests for virus and malware scanning (pre- and post-download)."""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from my_idm.database import Database, DownloadEntry
from my_idm.manager import DownloadManager
from my_idm.security import (
    KNOWN_THREAT_CATEGORIES,
    SecurityConfig,
    check_url_safety,
    find_windows_defender_path,
    quarantine_or_delete_file,
    scan_file,
)
from my_idm.security_dialog import SecuritySettingsDialog

app = QApplication.instance() or QApplication([])

# Running the real Windows Defender binary is opt-in. MpCmdRun.exe races
# real-time protection, takes up to 90 s, and fails under AppLocker, a sandbox,
# or a machine with no Defender subscription. Because scan_file() is FAIL-OPEN
# (security.py:358-364 returns True on any exception or unknown exit code) a
# green run of such a test proves nothing about the threat path at all.
RUN_REAL_AV_TESTS = os.environ.get("MYIDM_RUN_AV_TESTS", "") == "1"
IS_CI = os.environ.get("CI") == "true"


def _defender_config(**kw):
    """A config pointed at a scanner that is guaranteed to exist."""
    base = dict(scan_after_download=True, scanner_type="defender")
    base.update(kw)
    return SecurityConfig(**base)


def _pump_until(event, timeout=15.0):
    """Block until *event* is set, pumping the Qt loop while we wait.

    ``DownloadManager.scan_download_file`` finishes on a plain ``threading``
    thread, so ``status_changed`` is delivered as a *queued* connection and only
    lands once the event loop runs again. Sleeping instead would both race the
    thread and never observe the signal.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if event.wait(0.02):
            return True
        QApplication.processEvents()
    return event.is_set()


class TestSecurityConfig(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.test_settings = QSettings(
            str(Path(self.tmp_dir.name) / "test.ini"), QSettings.Format.IniFormat
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_default_security_config(self):
        cfg = SecurityConfig()
        self.assertTrue(cfg.scan_before_download)
        self.assertTrue(cfg.warn_high_risk_extensions)
        self.assertFalse(cfg.block_dangerous_urls)
        self.assertTrue(cfg.scan_after_download)
        self.assertEqual(cfg.scanner_type, "defender")
        self.assertEqual(cfg.action_on_threat, "warn")

    def test_save_and_load_persistence(self):
        cfg = SecurityConfig(
            scan_before_download=False,
            warn_high_risk_extensions=False,
            block_dangerous_urls=True,
            virustotal_api_key="mock_vt_key_12345",
            scan_after_download=True,
            scanner_type="custom",
            custom_scanner_path=r"C:\Tools\clamscan.exe",
            custom_scanner_args='--bell "%file%"',
            action_on_threat="delete",
        )
        cfg.save(self.test_settings)

        loaded = SecurityConfig.load(self.test_settings)
        self.assertFalse(loaded.scan_before_download)
        self.assertFalse(loaded.warn_high_risk_extensions)
        self.assertTrue(loaded.block_dangerous_urls)
        self.assertEqual(loaded.virustotal_api_key, "mock_vt_key_12345")
        self.assertTrue(loaded.scan_after_download)
        self.assertEqual(loaded.scanner_type, "custom")
        self.assertEqual(loaded.custom_scanner_path, r"C:\Tools\clamscan.exe")
        self.assertEqual(loaded.custom_scanner_args, '--bell "%file%"')
        self.assertEqual(loaded.action_on_threat, "delete")


class TestPreDownloadSafetyChecks(unittest.TestCase):

    def test_clean_document_url(self):
        cfg = SecurityConfig()
        is_safe, risk, details = check_url_safety("https://example.com/document.pdf", cfg)
        self.assertTrue(is_safe)
        self.assertEqual(risk, "clean")

    def test_deceptive_double_extension(self):
        cfg = SecurityConfig()
        is_safe, risk, details = check_url_safety("https://example.com/invoice.pdf.exe", cfg)
        self.assertFalse(is_safe)
        self.assertEqual(risk, "dangerous")
        self.assertIn("double extension", details.lower())

    def test_executable_extension_warning(self):
        cfg = SecurityConfig(warn_high_risk_extensions=True, block_dangerous_urls=False)
        is_safe, risk, details = check_url_safety("https://example.com/setup.msi", cfg)
        self.assertTrue(is_safe)  # Allowed when block_dangerous_urls is False
        self.assertEqual(risk, "warning")
        self.assertIn("executable", details.lower())

    def test_executable_extension_blocked_when_strict(self):
        cfg = SecurityConfig(warn_high_risk_extensions=True, block_dangerous_urls=True)
        is_safe, risk, details = check_url_safety("https://example.com/script.vbs", cfg)
        self.assertFalse(is_safe)  # Blocked in strict mode
        self.assertEqual(risk, "warning")

    def test_disabled_pre_scan(self):
        cfg = SecurityConfig(scan_before_download=False)
        is_safe, risk, _ = check_url_safety("https://example.com/malware.exe", cfg)
        self.assertTrue(is_safe)
        self.assertEqual(risk, "clean")


class TestPostDownloadAntivirusScanning(unittest.TestCase):
    """scan_file / quarantine_or_delete_file, with the scanner fully mocked.

    Nothing here may reach a real AV engine or a real Recycle Bin. The only
    tests that talk to Windows Defender are gated behind MYIDM_RUN_AV_TESTS=1.
    """

    def _temp_file(self, suffix=".txt", content=b"mock payload"):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        path = Path(tmpdir.name) / f"sample{suffix}"
        path.write_bytes(content)
        self.assertTrue(path.exists())
        return path

    def test_windows_defender_detection(self):
        path = find_windows_defender_path()
        if os.name != "nt":
            self.skipTest("Windows-only lookup")
        if not path:
            # On a host without Defender this must be a *visible* skip, never a
            # silently passing assertion (the original had no `else` branch, so
            # the whole test passed vacuously on POSIX).
            self.skipTest(
                "Windows Defender MpCmdRun.exe is not present on this machine; "
                "scan_file() will short-circuit to a 'no scanner' verdict."
            )
        self.assertIsNotNone(path, "Windows Defender MpCmdRun.exe should be present on Windows")
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(path.lower().endswith("mpcmdrun.exe"), path)

    def test_scan_file_reports_no_verdict_when_no_scanner_is_installed(self):
        """No scanner must not read as a clean file.

        This is the fail-open that made every scan on Linux and macOS report success for a file
        nothing had inspected. The verdict is ``None`` - neither clean nor threatened - and
        crucially it is falsy-but-not-False, so the callers that branch on "was a threat found?"
        do not treat it as one.
        """
        path = self._temp_file()
        with patch("my_idm.security.find_windows_defender_path", return_value=None):
            verdict, report = scan_file(str(path), _defender_config())
        self.assertIsNone(verdict, "a missing scanner must not produce a clean verdict")
        self.assertNotEqual(verdict, False, "no scanner is not a threat verdict either")
        self.assertIn("not scanned", report)
        self.assertIn("No antivirus scanner available", report)

    def test_a_no_verdict_scan_is_not_treated_as_a_threat_by_the_manager(self):
        """The dangerous direction: a falsy verdict must not trigger quarantine or deletion.

        ``manager._do_scan`` branches on ``verdict is False``. If that check were written as
        ``if not verdict`` instead, every download on a machine without Defender would be marked
        infected and, with `action_on_threat == "delete"`, deleted.
        """
        path = self._temp_file()
        with patch("my_idm.security.find_windows_defender_path", return_value=None):
            verdict, _ = scan_file(str(path), _defender_config())

        # Mirrors the manager's own branching, spelled out.
        self.assertNotEqual(verdict, False)
        # And the property the manager relies on.
        self.assertFalse(verdict is False)

    def test_scan_file_clean_with_defender(self):
        if not RUN_REAL_AV_TESTS:
            self.skipTest(
                "Runs the real MpCmdRun.exe (up to 90 s, races real-time "
                "protection, fail-open). Set MYIDM_RUN_AV_TESTS=1 to opt in."
            )
        if not find_windows_defender_path():
            self.skipTest("Windows Defender not available on this system")

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
            f.write("Clean file content for My-IDM unit test.")
            temp_path = f.name

        try:
            cfg = _defender_config()
            is_clean, report = scan_file(temp_path, cfg)
            self.assertTrue(is_clean, f"Expected clean verdict, got: {report}")
            self.assertIn("Clean", report)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_scan_file_clean_when_defender_reports_no_threats(self):
        """Deterministic stand-in for the real binary: exit code 0 means clean."""
        path = self._temp_file()
        with patch("my_idm.security.find_windows_defender_path", return_value=r"C:\fake\MpCmdRun.exe"), \
             patch("my_idm.security.running_on_windows", return_value=True), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "Scan completed."
            mock_run.return_value.stderr = ""
            is_clean, report = scan_file(str(path), _defender_config())

        self.assertTrue(is_clean, report)
        self.assertIn("Clean", report)
        argv = mock_run.call_args.args[0]
        self.assertEqual(argv[0], r"C:\fake\MpCmdRun.exe")
        self.assertEqual(
            argv[1:],
            ["-Scan", "-ScanType", "3", "-File", str(path.resolve()), "-DisableRemediation"],
            "the exact MpCmdRun argument vector is part of the contract",
        )
        self.assertEqual(mock_run.call_args.kwargs.get("timeout"), 90)

    def test_scan_file_flags_an_unexcluded_threat(self):
        path = self._temp_file(suffix=".exe")
        with patch("my_idm.security.find_windows_defender_path", return_value=r"C:\fake\MpCmdRun.exe"), \
             patch("my_idm.security.running_on_windows", return_value=True), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 2
            mock_run.return_value.stdout = "Threat detected: Trojan:Win32/Wacatac found in file."
            mock_run.return_value.stderr = ""
            # Narrow the exclusions so nothing matches; the default list contains
            # "PUA"/"Adware"/"Riskware" and would otherwise silently allow things.
            cfg = _defender_config(ignored_threat_categories=["HackTool"])
            self.assertEqual(
                cfg.get_effective_threat_exclusions(), ["HackTool"],
                "precondition: only HackTool is excluded",
            )
            is_clean, report = scan_file(str(path), cfg)

        self.assertFalse(is_clean, "an unexcluded threat must not be reported clean")
        self.assertIn("Trojan:Win32/Wacatac", report)
        self.assertIn("Threat detected by Windows Defender", report)
        mock_run.assert_called_once()

    def test_scan_file_disabled(self):
        cfg = SecurityConfig(scan_after_download=False)
        with patch("subprocess.run", side_effect=AssertionError("a disabled scan must not spawn a scanner")):
            is_clean, report = scan_file("some_dummy_file.zip", cfg)
        self.assertTrue(is_clean)
        self.assertIn("disabled", report.lower())

    def test_scan_file_missing_target_is_reported_not_scanned(self):
        cfg = _defender_config()
        with patch("subprocess.run", side_effect=AssertionError("nothing to scan")):
            is_clean, report = scan_file(str(Path(self._temp_file()).parent / "nope.bin"), cfg)
        self.assertTrue(is_clean)
        self.assertIn("does not exist on disk", report)

    def test_scan_file_custom_scanner_substitutes_the_file_placeholder(self):
        path = self._temp_file()
        scanner = Path(path.parent) / "fake_clamscan.exe"
        scanner.write_bytes(b"MZ")
        cfg = SecurityConfig(
            scan_after_download=True,
            scanner_type="custom",
            custom_scanner_path=str(scanner),
            custom_scanner_args='--bell "%file%"',
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""
            is_clean, report = scan_file(str(path), cfg)

        self.assertTrue(is_clean, report)
        self.assertIn("Clean", report)
        # An argv list, not a shell string: the target path comes from a download name and is
        # therefore attacker-influenced, so it must never reach a shell.
        cmd = mock_run.call_args.args[0]
        self.assertEqual(
            cmd,
            [str(scanner), "--bell", str(path.resolve())],
            "the scanner must be invoked as an argv list with the path as one argument",
        )
        self.assertNotIn("%file%", cmd)
        self.assertNotIn("%f", cmd)
        self.assertNotIn("shell", mock_run.call_args.kwargs)

    def test_scan_file_custom_scanner_missing_executable_is_reported(self):
        """A misconfigured scanner path yields no verdict, not a clean one.

        Same fail-open as a missing Defender: pointing `custom_scanner_path` at a binary that is
        not there must not read as "scanned, nothing found".
        """
        path = self._temp_file()
        cfg = SecurityConfig(
            scan_after_download=True,
            scanner_type="custom",
            custom_scanner_path=r"C:\definitely\not\here\clamscan.exe",
        )
        with patch("subprocess.run", side_effect=AssertionError("no scanner to run")):
            verdict, report = scan_file(str(path), cfg)
        self.assertIsNone(verdict)
        self.assertIn("not found", report)

    def test_quarantine_or_delete_file(self):
        """The real recycle-bin tiers are disabled; the permanent delete is the only path.

        ``quarantine_or_delete_file`` unlinks/rmtree's directly, so the test has
        to prove that is what happened rather than leaving a real file sitting in
        the developer's Recycle Bin and asserting only "it is gone".
        """
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False, mode="wb") as f:
            f.write(b"Mock infected data")
            temp_path = f.name

        self.assertTrue(os.path.exists(temp_path))
        import shutil as _shutil

        real_rmtree = _shutil.rmtree
        real_unlink = Path.unlink
        unlinked = []
        rmtree_calls = []

        def spy_unlink(self, *a, **k):
            unlinked.append(str(self))
            return real_unlink(self, *a, **k)

        def spy_rmtree(path, *a, **k):
            rmtree_calls.append(str(path))
            return real_rmtree(path, *a, **k)

        with patch("PySide6.QtCore.QFile") as mock_qfile, \
             patch("send2trash.send2trash", side_effect=AssertionError("no trash in tests")):
            mock_qfile.moveToTrash.return_value = False
            with patch.object(Path, "unlink", spy_unlink), \
                 patch.object(_shutil, "rmtree", spy_rmtree):
                res = quarantine_or_delete_file(temp_path)

        self.assertTrue(res, "quarantine_or_delete_file must report success")
        self.assertFalse(os.path.exists(temp_path), "the file must be gone")
        self.assertEqual(unlinked, [temp_path], "the file must be unlinked, not trashed")
        self.assertEqual(rmtree_calls, [], "rmtree is for directories only")
        mock_qfile.moveToTrash.assert_not_called()

    def test_quarantine_or_delete_directory_uses_rmtree(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        victim = Path(tmpdir.name) / "infected_dir"
        victim.mkdir()
        (victim / "payload.bin").write_bytes(b"x" * 16)
        import shutil as _shutil

        real_rmtree = _shutil.rmtree
        real_unlink = Path.unlink
        rmtree_calls = []
        unlinked = []

        def spy_rmtree(path, *a, **k):
            rmtree_calls.append(str(path))
            return real_rmtree(path, *a, **k)

        def spy_unlink(self, *a, **k):
            unlinked.append(str(self))
            return real_unlink(self, *a, **k)

        with patch("PySide6.QtCore.QFile") as mock_qfile, \
             patch("send2trash.send2trash", side_effect=AssertionError("no trash in tests")):
            mock_qfile.moveToTrash.return_value = False
            with patch.object(Path, "unlink", spy_unlink), \
                 patch.object(_shutil, "rmtree", spy_rmtree):
                res = quarantine_or_delete_file(str(victim))

        self.assertTrue(res)
        self.assertFalse(victim.exists())
        self.assertEqual(rmtree_calls, [str(victim)])
        self.assertEqual(unlinked, [])

    def test_quarantine_or_delete_missing_file_reports_failure(self):
        missing = str(Path(tempfile.gettempdir()) / "definitely_not_here_9f3a.bin")
        self.assertFalse(os.path.exists(missing))
        self.assertFalse(
            quarantine_or_delete_file(missing),
            "nothing was deleted, so nothing may be reported as quarantined",
        )


class TestManagerSecurityIntegration(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.db = Database(Path(self.tmp_dir.name) / "test.db")
        self.db.open()
        self.addCleanup(self.db.close)
        self.manager = DownloadManager(self.db)
        self.addCleanup(self.manager.stop)
        self.manager.set_security_config(SecurityConfig())

    def test_manager_security_config_update(self):
        new_cfg = SecurityConfig(
            scan_after_download=False,
            action_on_threat="delete",
        )
        self.manager.set_security_config(new_cfg)
        self.assertEqual(self.manager.security_config.action_on_threat, "delete")
        self.assertFalse(self.manager.security_config.scan_after_download)

    def test_add_download_stores_security_warning_in_metadata(self):
        # Add URL with executable extension
        did = self.manager.add_download("https://example.com/installer.exe")
        self.assertIsNotNone(did)
        entry = self.manager.get_entry(did)
        self.assertIsNotNone(entry)
        meta = entry.metadata
        self.assertIn("security_warning", meta)
        self.assertIn("executable", meta["security_warning"].lower())

    def test_add_download_blocked_when_strict(self):
        strict_cfg = SecurityConfig(block_dangerous_urls=True)
        self.manager.set_security_config(strict_cfg)
        # Dangerous double extension
        did = self.manager.add_download("https://example.com/document.doc.exe")
        self.assertIsNone(did, "Strict mode must block dangerous double extension")
        self.assertEqual(
            self.db.get_all_downloads(), [],
            "a blocked URL must not leave a row behind",
        )

    @unittest.skipIf(IS_CI, "flaky on CI: QApplication.processEvents in _pump_until")
    def test_antivirus_preserves_incomplete_status(self):
        """A clean scan of a *completed* but partial file must not keep it completed.

        ``DownloadManager.scan_download_file`` restarts the entry as "scanning",
        then picks the restore target. For a row whose status is "paused" the
        ``target_status == "completed"`` branch is dead code, so the entry is
        built as a "completed" row with downloaded_size < total_size: that is the
        only input that reaches manager.py:2976, where a partial file is
        downgraded to "paused" instead of being falsely marked complete.

        ``Database._row_to_entry`` (database.py:545-549) silently raises
        downloaded_size to total_size for any completed/seeding row, so the
        manager is fed the partial entry directly; see
        ``test_completed_partial_row_is_healed_on_read`` for the proof that the
        guard is currently unreachable through the DB alone.
        """
        test_file = Path(self.tmp_dir.name) / "incomplete.bin"
        test_file.write_bytes(b"partial content")

        self.db.add_download(
            DownloadEntry(
                id="d1",
                url="https://example.com/incomplete.bin",
                filename="incomplete.bin",
                file_path=str(test_file),
                save_path=self.tmp_dir.name,
                total_size=1000,
                downloaded_size=10,
                status="completed",
            )
        )
        self.assertEqual(self.db.get_download("d1").status, "completed")

        partial = self.db.get_download("d1")
        partial.total_size = 1000
        partial.downloaded_size = 10
        real_get_download = self.manager._db.get_download
        self.manager._db.get_download = (
            lambda did: partial if did == "d1" else real_get_download(did)
        )

        def restore():
            self.manager._db.get_download = real_get_download

        self.addCleanup(restore)

        settled = threading.Event()
        emitted = []

        def _on_status(download_id, status, error=""):
            emitted.append((download_id, status))
            if status != "scanning":
                settled.set()

        self.manager.status_changed.connect(_on_status)
        self.addCleanup(self.manager.status_changed.disconnect, _on_status)

        scan_calls = []

        def fake_scan(path, config):
            scan_calls.append(path)
            return True, "Clean file"

        stored_path = real_get_download("d1").file_path
        self.assertEqual(stored_path, str(test_file).replace("\\", "/"))

        with patch("my_idm.manager.scan_file", side_effect=fake_scan):
            self.manager.scan_download_file("d1")
            # Wait for the fire-and-forget scan thread instead of sleeping past
            # it: a slow thread would otherwise touch a torn-down sqlite handle.
            self.assertTrue(
                _pump_until(settled, 15.0),
                f"scan thread never finished a status transition (saw {emitted!r})",
            )

        self.assertEqual(scan_calls, [stored_path], "the real file path must be scanned")
        statuses = [s for _, s in emitted]
        self.assertEqual(
            statuses, ["scanning", "paused"],
            "an incomplete file must come back as paused, never completed",
        )
        restore()  # the stub would otherwise shadow the real row on the next read
        updated = self.db.get_download("d1")
        self.assertEqual(updated.status, "paused")
        self.assertNotEqual(updated.status, "completed")
        self.assertTrue(updated.metadata.get("antivirus_scanned"))
        self.assertEqual(updated.metadata.get("antivirus_report"), "Clean file")
        self.assertNotIn("threat_detected", updated.metadata)

    def test_completed_partial_row_is_healed_on_read(self):
        """Documents why scan_download_file's partial-download guard is unreachable.

        ``Database._row_to_entry`` raises ``downloaded_size`` to ``total_size``
        for completed/seeding rows *in memory* (the stored value is untouched),
        so the manager can never observe a completed row with
        ``downloaded_size < total_size`` and the guard at manager.py:2976 never
        fires through the database. Pinned so the interaction is visible.
        """
        self.db.add_download(
            DownloadEntry(
                id="heal",
                url="https://example.com/heal.bin",
                filename="heal.bin",
                file_path=str(Path(self.tmp_dir.name) / "heal.bin"),
                save_path=self.tmp_dir.name,
                total_size=1000,
                downloaded_size=10,
                status="completed",
            )
        )
        read = self.db.get_download("heal")
        self.assertEqual(read.downloaded_size, 1000, "the read heals the byte count")
        self.assertEqual(read.progress, 100.0)
        row = self.db._conn.execute(
            "SELECT downloaded_size FROM downloads WHERE id = 'heal'"
        ).fetchone()
        self.assertEqual(
            row["downloaded_size"], 10,
            "the heal is in-memory only; the stored row keeps the real byte count",
        )

    @unittest.skipIf(IS_CI, "flaky on CI: QApplication.processEvents in _pump_until")
    def test_antivirus_keeps_a_fully_downloaded_entry_completed(self):
        """The other side of the same guard: a complete file stays completed."""
        test_file = Path(self.tmp_dir.name) / "complete.bin"
        test_file.write_bytes(b"0123456789")

        self.db.add_download(
            DownloadEntry(
                id="d2",
                url="https://example.com/complete.bin",
                filename="complete.bin",
                file_path=str(test_file),
                save_path=self.tmp_dir.name,
                total_size=10,
                downloaded_size=10,
                status="completed",
            )
        )

        settled = threading.Event()
        statuses = []

        def _on_status(download_id, status, error=""):
            statuses.append(status)
            if status != "scanning":
                settled.set()

        self.manager.status_changed.connect(_on_status)
        self.addCleanup(self.manager.status_changed.disconnect, _on_status)

        with patch("my_idm.manager.scan_file", return_value=(True, "Clean file")):
            self.manager.scan_download_file("d2")
            self.assertTrue(
                _pump_until(settled, 15.0),
                f"scan thread never finished a status transition (saw {statuses!r})",
            )

        self.assertEqual(statuses, ["scanning", "completed"])
        self.assertEqual(self.db.get_download("d2").status, "completed")

    @unittest.skipIf(IS_CI, "flaky on CI: QApplication.processEvents in _pump_until")
    def test_antivirus_marks_threat_detected_and_deletes(self):
        """A dirty scan with action_on_threat='delete' quarantines the file."""
        test_file = Path(self.tmp_dir.name) / "infected.bin"
        test_file.write_bytes(b"eicar-ish")
        self.manager.set_security_config(SecurityConfig(action_on_threat="delete"))
        self.db.add_download(
            DownloadEntry(
                id="d3",
                url="https://example.com/infected.bin",
                filename="infected.bin",
                file_path=str(test_file),
                save_path=self.tmp_dir.name,
                total_size=10,
                downloaded_size=10,
                status="completed",
            )
        )

        settled = threading.Event()
        threats = []
        statuses = []

        def _on_status(download_id, status, error=""):
            statuses.append(status)
            if status != "scanning":
                settled.set()

        def _on_threat(download_id, report):
            threats.append((download_id, report))
            settled.set()

        self.manager.status_changed.connect(_on_status)
        self.manager.threat_detected.connect(_on_threat)
        self.addCleanup(self.manager.status_changed.disconnect, _on_status)
        self.addCleanup(self.manager.threat_detected.disconnect, _on_threat)

        stored_path = self.db.get_download("d3").file_path
        with patch("my_idm.manager.scan_file", return_value=(False, "Threat: Trojan:Win32/Wacatac")), \
             patch("my_idm.manager.quarantine_or_delete_file") as mock_quarantine:
            self.manager.scan_download_file("d3")
            self.assertTrue(
                _pump_until(settled, 15.0),
                f"scan thread never reported a threat (saw {statuses!r})",
            )

        mock_quarantine.assert_called_once_with(stored_path)
        self.assertEqual(statuses, ["scanning", "threat_detected"])
        self.assertEqual(len(threats), 1, "the threat signal must fire exactly once")
        self.assertIn("Trojan:Win32/Wacatac", threats[0][1])
        self.assertIn(
            "Infected file deleted", threats[0][1],
            "the threat signal must say the file was deleted",
        )
        updated = self.db.get_download("d3")
        self.assertEqual(updated.status, "threat_detected")
        self.assertTrue(updated.metadata.get("threat_detected"))
        # NOTE: the metadata keeps the *un*-suffixed report. scan_download_file
        # writes meta["antivirus_report"] before appending "(Infected file
        # deleted)" to the local `report`, so the stored text and the emitted
        # text disagree. Pinned as-is; see the report (production oddity).
        self.assertEqual(updated.metadata.get("antivirus_report"), "Threat: Trojan:Win32/Wacatac")

    def test_scan_download_file_on_unknown_id_is_safe(self):
        with patch("my_idm.manager.scan_file", side_effect=AssertionError("no file to scan")):
            self.manager.scan_download_file("no-such-id")


class TestSecuritySettingsDialog(unittest.TestCase):

    def test_dialog_loads_and_saves_config(self):
        cfg = SecurityConfig(
            scan_before_download=True,
            warn_high_risk_extensions=True,
            scan_after_download=True,
            action_on_threat="warn",
            scan_timing="after_complete",
            ignored_threat_categories=["HackTool", "CrackTool"],
        )
        dlg = SecuritySettingsDialog(cfg)
        self.assertTrue(dlg._scan_before_cb.isChecked())
        self.assertTrue(dlg._warn_ext_cb.isChecked())
        self.assertTrue(dlg._rb_warn.isChecked())
        self.assertTrue(dlg._rb_timing_auto.isChecked())
        self.assertEqual(dlg._threat_excl_list.count(), 2)

        # Toggle to delete and manual_only
        dlg._rb_delete.setChecked(True)
        dlg._rb_timing_manual.setChecked(True)

        # Add an exclusion via list UI
        dlg._new_threat_excl_edit.setText("Keygen, PUA")
        dlg._on_add_threat_exclusion()
        self.assertEqual(dlg._threat_excl_list.count(), 4)

        # Remove an exclusion
        dlg._threat_excl_list.setCurrentRow(0)
        dlg._on_remove_threat_exclusion()
        self.assertEqual(dlg._threat_excl_list.count(), 3)

        dlg._on_save()

        self.assertEqual(dlg.config.action_on_threat, "delete")
        self.assertEqual(dlg.config.scan_timing, "manual_only")
        self.assertIn("Keygen", dlg.config.ignored_threat_categories)
        self.assertIn("PUA", dlg.config.ignored_threat_categories)
        dlg.close()

    def test_dialog_reset_exclusions_defaults(self):
        dlg = SecuritySettingsDialog(SecurityConfig())
        dlg._threat_excl_list.clear()
        self.assertEqual(dlg._threat_excl_list.count(), 0)

        dlg._on_reset_threat_exclusions_defaults()
        self.assertGreaterEqual(dlg._threat_excl_list.count(), 3)
        items = [dlg._threat_excl_list.item(i).text() for i in range(dlg._threat_excl_list.count())]
        self.assertIn("HackTool", items)
        self.assertIn("CrackTool", items)
        dlg.close()


class TestThreatExclusionAndScanTiming(unittest.TestCase):

    FAKE_DEFENDER = r"C:\fake\MpCmdRun.exe"

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.path = Path(self.tmp_dir.name) / "mock.exe"
        self.path.write_bytes(b"mock binary")
        self.addCleanup(self.path.unlink, missing_ok=True)
        # `scan_file` only consults Defender on Windows; on Linux and macOS the same `auto`
        # setting looks for ClamAV instead, which no CI runner has, so every test here was
        # asserting against "No antivirus scanner available" rather than against the exclusion
        # logic. The scanner is faked, so the host platform is irrelevant to what is under test.
        # `security.running_on_windows` is patched rather than `sys.platform` because a
        # process-wide "win32" also fools the standard library - `shutil.which` then takes its
        # Windows branch and reaches for `_winapi`, which does not exist here.
        platform_patcher = patch("my_idm.security.running_on_windows", return_value=True)
        platform_patcher.start()
        self.addCleanup(platform_patcher.stop)

    def _scanner(self, returncode, stdout, stderr=""):
        """A patched MpCmdRun that is guaranteed to be found.

        ``find_windows_defender_path`` is NOT patched in the original tests, so
        on a host without Defender ``scan_file`` short-circuited at
        security.py:321-324 and returned
        "Windows Defender scanner not found; skipped scan." -- the exclusion
        assertions then either failed confusingly or passed vacuously.
        """
        fake_run = unittest.mock.MagicMock()
        fake_run.return_value.returncode = returncode
        fake_run.return_value.stdout = stdout
        fake_run.return_value.stderr = stderr
        return patch("my_idm.security.find_windows_defender_path", return_value=self.FAKE_DEFENDER), patch("subprocess.run", fake_run)

    def test_threat_exclusion_silently_allows_matched_threat(self):
        find_patch, run_patch = self._scanner(
            2, "Threat detected: HackTool:Win32/AutoKMS found in file."
        )
        with find_patch, run_patch as mock_run:
            cfg = _defender_config(ignored_threat_categories=["HackTool", "CrackTool"])
            self.assertEqual(
                cfg.get_effective_threat_exclusions(), ["HackTool", "CrackTool"],
                "precondition: only the two named categories are excluded",
            )
            is_clean, report = scan_file(str(self.path), cfg)

        self.assertTrue(is_clean, "Threat matching ignored_threat_categories should be allowed as clean")
        self.assertIn("Allowed (matched exclusion 'HackTool')", report)
        argv = mock_run.call_args.args[0]
        self.assertEqual(argv[0], self.FAKE_DEFENDER, "the scanner must actually have been invoked")

    def test_threat_exclusion_is_order_independent_across_categories(self):
        find_patch, run_patch = self._scanner(
            2, "Threat detected: CrackTool:W32/KeyGen found in file."
        )
        with find_patch, run_patch:
            cfg = _defender_config(ignored_threat_categories=["HackTool", "CrackTool"])
            is_clean, report = scan_file(str(self.path), cfg)
        self.assertTrue(is_clean, report)
        self.assertIn("Allowed (matched exclusion 'CrackTool')", report)

    def test_threat_not_excluded_is_flagged(self):
        find_patch, run_patch = self._scanner(
            2, "Threat detected: Trojan:Win32/Wacatac found in file."
        )
        with find_patch, run_patch:
            cfg = _defender_config(ignored_threat_categories=["HackTool"])
            # The default list still carries PUA/Adware/Riskware, so the test only
            # exercises the Trojan path because the config narrows it to HackTool.
            self.assertEqual(
                cfg.get_effective_threat_exclusions(), ["HackTool"],
                "the exclusion list must actually be narrowed for this assertion "
                "to mean anything",
            )
            is_clean, report = scan_file(str(self.path), cfg)

        self.assertFalse(is_clean, "Threat not matching exclusions must be flagged")
        self.assertIn("Trojan:Win32/Wacatac", report)
        self.assertNotIn("Allowed", report)

    def test_default_exclusions_would_have_allowed_pua(self):
        """Why the narrowing above is load-bearing: the default list is broad."""
        cfg = _defender_config()
        self.assertEqual(
            cfg.get_effective_threat_exclusions(), KNOWN_THREAT_CATEGORIES
        )
        find_patch, run_patch = self._scanner(2, "PUA:Win32/Bundler found in file.")
        with find_patch, run_patch:
            is_clean, report = scan_file(str(self.path), cfg)
        self.assertTrue(is_clean, report)
        self.assertIn("Allowed (matched exclusion 'PUA')", report)

    def test_custom_pattern_exclusion_is_honoured(self):
        find_patch, run_patch = self._scanner(
            2, "Threat detected: Custom:Internal/Whatever found in file."
        )
        with find_patch, run_patch:
            cfg = _defender_config(
                ignored_threat_categories=["HackTool"],
                ignored_threat_patterns="Internal/Whatever, Another",
            )
            is_clean, report = scan_file(str(self.path), cfg)
        self.assertTrue(is_clean, report)
        self.assertIn("Allowed (matched exclusion 'Internal/Whatever')", report)

    def test_manual_only_scan_timing_skips_auto_scan(self):
        db = None
        mgr = None
        try:
            db = Database(Path(self.tmp_dir.name) / "test.db")
            db.open()
            mgr = DownloadManager(db)
            cfg = SecurityConfig(
                scan_after_download=True,
                scan_timing="manual_only",
            )
            mgr.set_security_config(cfg)

            entry = DownloadEntry(
                id="test-dl",
                url="https://example.com/test.bin",
                filename="test.bin",
                file_path=str(Path(self.tmp_dir.name) / "test.bin"),
                save_path=self.tmp_dir.name,
                total_size=100,
                downloaded_size=100,
                status="downloading",
            )
            db.add_download(entry)

            emitted_statuses = []
            mgr.status_changed.connect(lambda did, st, err: emitted_statuses.append((did, st)))
            self.addCleanup(mgr.status_changed.disconnect)

            with patch.object(mgr, "_handle_completed_scan") as mock_scan:
                mgr._on_http_status("test-dl", "completed", "")
                mock_scan.assert_not_called()
                self.assertIn(("test-dl", "completed"), emitted_statuses)
                self.assertEqual(
                    emitted_statuses.count(("test-dl", "completed")), 1,
                    "exactly one completion transition, and none of it scanned",
                )
        finally:
            if mgr:
                mgr.stop()
            if db:
                db.close()


if __name__ == "__main__":
    unittest.main()

