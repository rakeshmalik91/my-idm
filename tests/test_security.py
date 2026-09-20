"""Unit tests for virus and malware scanning (pre- and post-download)."""

import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from unittest.mock import patch
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from my_idm.database import Database, DownloadEntry
from my_idm.manager import DownloadManager
from my_idm.security import (
    SecurityConfig,
    check_url_safety,
    find_windows_defender_path,
    quarantine_or_delete_file,
    scan_file,
)
from my_idm.security_dialog import SecuritySettingsDialog

app = QApplication.instance() or QApplication([])


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

    def test_windows_defender_detection(self):
        path = find_windows_defender_path()
        if os.name == "nt":
            self.assertIsNotNone(path, "Windows Defender MpCmdRun.exe should be present on Windows")
            self.assertTrue(os.path.isfile(path))

    def test_scan_file_clean_with_defender(self):
        path = find_windows_defender_path()
        if not path:
            self.skipTest("Windows Defender not available on this system")

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
            f.write("Clean file content for My-IDM unit test.")
            temp_path = f.name

        try:
            cfg = SecurityConfig(scan_after_download=True, scanner_type="defender")
            is_clean, report = scan_file(temp_path, cfg)
            self.assertTrue(is_clean, f"Expected clean verdict, got: {report}")
            self.assertIn("Clean", report)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_scan_file_disabled(self):
        cfg = SecurityConfig(scan_after_download=False)
        is_clean, report = scan_file("some_dummy_file.zip", cfg)
        self.assertTrue(is_clean)
        self.assertIn("disabled", report.lower())

    def test_quarantine_or_delete_file(self):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False, mode="wb") as f:
            f.write(b"Mock infected data")
            temp_path = f.name

        self.assertTrue(os.path.exists(temp_path))
        res = quarantine_or_delete_file(temp_path)
        self.assertTrue(res)
        self.assertFalse(os.path.exists(temp_path))


class TestManagerSecurityIntegration(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp_dir.name) / "test.db")
        self.db.open()
        self.manager = DownloadManager(self.db)
        self.manager.set_security_config(SecurityConfig())

    def tearDown(self):
        self.manager.stop()
        self.db.close()
        self.tmp_dir.cleanup()

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

    def test_antivirus_preserves_incomplete_status(self):
        """Scanning an incomplete file does not mark it completed."""
        test_file = Path(self.tmp_dir.name) / "incomplete.bin"
        test_file.write_bytes(b"partial content")

        entry = DownloadEntry(
            id="d1",
            url="https://example.com/incomplete.bin",
            filename="incomplete.bin",
            file_path=str(test_file),
            save_path=self.tmp_dir.name,
            total_size=1000000,
            downloaded_size=len(b"partial content"),
            status="paused",
        )
        self.db.add_download(entry)

        with patch("my_idm.manager.scan_file", return_value=(True, "Clean file")):
            self.manager.scan_download_file("d1")
            import time
            time.sleep(0.3)

            updated = self.db.get_download("d1")
            self.assertEqual(updated.status, "paused")
            self.assertNotEqual(updated.status, "completed")


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

    def test_threat_exclusion_silently_allows_matched_threat(self):
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as f:
            f.write(b"mock binary")
            temp_path = f.name

        try:
            cfg = SecurityConfig(
                scan_after_download=True,
                scanner_type="defender",
                ignored_threat_categories=["HackTool", "CrackTool"],
            )
            # Mock subprocess.run simulating Defender detecting HackTool:Win32/AutoKMS
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 2
                mock_run.return_value.stdout = "Threat detected: HackTool:Win32/AutoKMS found in file."
                mock_run.return_value.stderr = ""

                is_clean, report = scan_file(temp_path, cfg)
                self.assertTrue(is_clean, "Threat matching ignored_threat_categories should be allowed as clean")
                self.assertIn("Allowed (matched exclusion 'HackTool')", report)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_threat_not_excluded_is_flagged(self):
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as f:
            f.write(b"mock binary")
            temp_path = f.name

        try:
            cfg = SecurityConfig(
                scan_after_download=True,
                scanner_type="defender",
                ignored_threat_categories=["HackTool"],
            )
            # Mock subprocess.run simulating Trojan:Win32/Wacatac
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 2
                mock_run.return_value.stdout = "Threat detected: Trojan:Win32/Wacatac found in file."
                mock_run.return_value.stderr = ""

                is_clean, report = scan_file(temp_path, cfg)
                self.assertFalse(is_clean, "Threat not matching exclusions must be flagged")
                self.assertIn("Trojan:Win32/Wacatac", report)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_manual_only_scan_timing_skips_auto_scan(self):
        tmp_dir = tempfile.TemporaryDirectory()
        db = None
        mgr = None
        try:
            db = Database(Path(tmp_dir.name) / "test.db")
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
                file_path=str(Path(tmp_dir.name) / "test.bin"),
                save_path=tmp_dir.name,
                total_size=100,
                downloaded_size=100,
                status="downloading",
            )
            db.add_download(entry)

            emitted_statuses = []
            mgr.status_changed.connect(lambda did, st, err: emitted_statuses.append((did, st)))

            with patch.object(mgr, "_handle_completed_scan") as mock_scan:
                mgr._on_http_status("test-dl", "completed", "")
                mock_scan.assert_not_called()
                self.assertIn(("test-dl", "completed"), emitted_statuses)
        finally:
            if mgr:
                mgr.stop()
            if db:
                db.close()
            tmp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()

