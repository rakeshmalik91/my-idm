"""Antivirus and malware scanning module for My-IDM."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from PySide6.QtCore import QSettings

log = logging.getLogger(__name__)

# Extensions with higher risk of malware or direct execution
HIGH_RISK_EXTENSIONS = {
    ".exe", ".scr", ".bat", ".cmd", ".vbs", ".vbe", ".js", ".jse",
    ".wsf", ".wsh", ".msc", ".msi", ".msp", ".pif", ".hta", ".cpl",
    ".jar", ".gadget", ".iso", ".img", ".ps1",
}


@dataclass
class SecurityConfig:
    """Configuration for pre- and post-download antivirus scanning."""
    # Pre-download checks
    scan_before_download: bool = True
    warn_high_risk_extensions: bool = True
    block_dangerous_urls: bool = False
    virustotal_api_key: str = ""

    # Post-download file scanning
    scan_after_download: bool = True
    scanner_type: str = "defender"  # "defender" or "custom"
    custom_scanner_path: str = ""
    custom_scanner_args: str = '"%file%"'
    action_on_threat: str = "warn"  # "warn" or "delete"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> SecurityConfig:
        return cls(
            scan_before_download=bool(data.get("scan_before_download", True)),
            warn_high_risk_extensions=bool(data.get("warn_high_risk_extensions", True)),
            block_dangerous_urls=bool(data.get("block_dangerous_urls", False)),
            virustotal_api_key=str(data.get("virustotal_api_key", "")),
            scan_after_download=bool(data.get("scan_after_download", True)),
            scanner_type=str(data.get("scanner_type", "defender")),
            custom_scanner_path=str(data.get("custom_scanner_path", "")),
            custom_scanner_args=str(data.get("custom_scanner_args", '"%file%"')),
            action_on_threat=str(data.get("action_on_threat", "warn")),
        )

    def save(self, settings: Optional[QSettings] = None):
        if settings is None:
            settings = QSettings("MyIDM", "My-IDM")
        settings.beginGroup("Security")
        settings.setValue("scan_before_download", self.scan_before_download)
        settings.setValue("warn_high_risk_extensions", self.warn_high_risk_extensions)
        settings.setValue("block_dangerous_urls", self.block_dangerous_urls)
        settings.setValue("virustotal_api_key", self.virustotal_api_key)
        settings.setValue("scan_after_download", self.scan_after_download)
        settings.setValue("scanner_type", self.scanner_type)
        settings.setValue("custom_scanner_path", self.custom_scanner_path)
        settings.setValue("custom_scanner_args", self.custom_scanner_args)
        settings.setValue("action_on_threat", self.action_on_threat)
        settings.endGroup()

    @classmethod
    def load(cls, settings: Optional[QSettings] = None) -> SecurityConfig:
        if settings is None:
            settings = QSettings("MyIDM", "My-IDM")
        settings.beginGroup("Security")
        cfg = cls(
            scan_before_download=settings.value("scan_before_download", True, type=bool),
            warn_high_risk_extensions=settings.value("warn_high_risk_extensions", True, type=bool),
            block_dangerous_urls=settings.value("block_dangerous_urls", False, type=bool),
            virustotal_api_key=str(settings.value("virustotal_api_key", "") or ""),
            scan_after_download=settings.value("scan_after_download", True, type=bool),
            scanner_type=str(settings.value("scanner_type", "defender") or "defender"),
            custom_scanner_path=str(settings.value("custom_scanner_path", "") or ""),
            custom_scanner_args=str(settings.value("custom_scanner_args", '"%file%"') or '"%file%"'),
            action_on_threat=str(settings.value("action_on_threat", "warn") or "warn"),
        )
        settings.endGroup()
        return cfg


def find_windows_defender_path() -> Optional[str]:
    """Locate the Windows Defender command-line scanner (MpCmdRun.exe)."""
    candidates = [
        r"C:\Program Files\Windows Defender\MpCmdRun.exe",
        r"C:\Program Files (x86)\Windows Defender\MpCmdRun.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c

    # Search in Windows Defender Platform folder (Windows 10/11 updates)
    platform_dir = r"C:\ProgramData\Microsoft\Windows Defender\Platform"
    if os.path.isdir(platform_dir):
        try:
            for root, _, files in os.walk(platform_dir):
                if "MpCmdRun.exe" in files:
                    full_path = os.path.join(root, "MpCmdRun.exe")
                    if os.path.isfile(full_path):
                        return full_path
        except Exception as e:
            log.debug("Error searching platform directory: %s", e)

    return shutil.which("MpCmdRun.exe") or shutil.which("mpcmdrun")


def check_url_safety(url: str, config: SecurityConfig) -> tuple[bool, str, str]:
    """Inspect a download URL before downloading.

    Returns (is_safe, risk_level, details).
    risk_level can be 'clean', 'warning', or 'dangerous'.
    """
    if not config.scan_before_download:
        return True, "clean", "Pre-download scan disabled."

    url = url.strip()
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    path = parsed.path.lower()
    hostname = (parsed.hostname or "").lower()

    # Magnet / torrent local file check
    if scheme == "magnet" or url.lower().endswith(".torrent"):
        return True, "clean", "Torrent / Magnet link (will be scanned after download)."

    # 1. Check for deceptive double extensions (e.g., file.pdf.exe)
    if re.search(r"\.[a-z0-9]{2,4}\.(exe|scr|bat|cmd|vbs|pif|msi)$", path):
        details = (
            f"Suspicious double extension detected in '{Path(path).name}'. "
            "This is a common malware deception tactic."
        )
        return False, "dangerous", details

    # 2. Check for executable/script payload extensions
    ext = Path(path).suffix.lower()
    if config.warn_high_risk_extensions and ext in HIGH_RISK_EXTENSIONS:
        details = (
            f"Executable file type '{ext}' detected. Executables can run code directly "
            "on your computer."
        )
        return False if config.block_dangerous_urls else True, "warning", details

    # 3. Check for bare IP addresses hosting files (often used in drive-by downloads)
    if hostname and re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", hostname):
        # Allow loopback/private IPs for local testing
        if not (hostname.startswith("127.") or hostname.startswith("192.168.") or hostname.startswith("10.")):
            details = f"Download is hosted on a bare IP address ({hostname}) rather than a domain name."
            return True, "warning", details

    # 4. Optional VirusTotal API check if key is provided
    if config.virustotal_api_key and url.startswith("http"):
        try:
            import base64
            url_id = base64.urlsafe_b64encode(url.encode()).decode().strip("=")
            req = urllib.request.Request(
                f"https://www.virustotal.com/api/v3/urls/{url_id}",
                headers={"x-apikey": config.virustotal_api_key},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode())
                    stats = data.get("data", {}).get("attributes", {}).get("last_analysis_stats", {})
                    malicious = stats.get("malicious", 0)
                    suspicious = stats.get("suspicious", 0)
                    if malicious > 0 or suspicious > 0:
                        return (
                            False,
                            "dangerous",
                            f"VirusTotal detected {malicious} malicious / {suspicious} suspicious engine verdicts.",
                        )
        except Exception as e:
            log.debug("VirusTotal query failed: %s", e)

    return True, "clean", "URL passed preliminary safety checks."


def scan_file(file_path: str, config: SecurityConfig) -> tuple[bool, str]:
    """Scan a downloaded file with the configured antivirus scanner.

    Returns (is_clean, report_message).
    """
    if not config.scan_after_download:
        return True, "Post-download scan is disabled."

    file_p = Path(file_path)
    if not file_p.exists():
        return True, f"File does not exist on disk: {file_path}"

    abs_path = str(file_p.resolve())

    # 1. Custom scanner
    if config.scanner_type == "custom" and config.custom_scanner_path:
        scanner = config.custom_scanner_path
        if not os.path.isfile(scanner):
            return True, f"Configured custom antivirus executable not found: {scanner}"

        # Replace %file% or %f with target path
        args_template = config.custom_scanner_args or '"%file%"'
        cmd_str = f'"{scanner}" ' + args_template.replace("%file%", abs_path).replace("%f", abs_path)
        log.info("Running custom antivirus scan: %s", cmd_str)

        try:
            res = subprocess.run(
                cmd_str,
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            # Standard exit codes: 0 is clean, non-zero usually indicates threat or error
            if res.returncode == 0:
                return True, f"Clean (Custom Scanner: {Path(scanner).name})"
            else:
                out = (res.stdout + "\n" + res.stderr).strip()
                return False, f"Threat detected or scanner alert (Exit code {res.returncode}): {out[:200]}"
        except subprocess.TimeoutExpired:
            return True, "Custom scan timed out after 60 seconds."
        except Exception as exc:
            log.error("Custom scan failed: %s", exc)
            return True, f"Custom scan execution error: {exc}"

    # 2. Windows Defender (Default)
    defender = find_windows_defender_path()
    if not defender:
        log.warning("Windows Defender (MpCmdRun.exe) not found on this system")
        return True, "Windows Defender scanner not found; skipped scan."

    log.info("Running Windows Defender scan on %s", abs_path)
    cmd = [defender, "-Scan", "-ScanType", "3", "-File", abs_path, "-DisableRemediation"]

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=90,
        )
        out = (res.stdout + "\n" + res.stderr).strip()

        # MpCmdRun return codes:
        # 0: No threat detected
        # 2: Threat detected
        if res.returncode == 0:
            return True, "Clean (Windows Defender verified no threats found)"
        elif res.returncode == 2:
            return False, f"⚠️ Threat detected by Windows Defender!\n{out}"
        else:
            if "found no threats" in out.lower():
                return True, "Clean (Windows Defender: no threats found)"
            if "threat" in out.lower():
                return False, f"⚠️ Threat detected by Windows Defender: {out}"
            return True, f"Windows Defender completed with code {res.returncode}."
    except subprocess.TimeoutExpired:
        log.warning("Windows Defender scan timed out on %s", abs_path)
        return True, "Windows Defender scan timed out."
    except Exception as exc:
        log.error("Windows Defender execution failed: %s", exc)
        return True, f"Scanner error: {exc}"


def quarantine_or_delete_file(file_path: str) -> bool:
    """Delete an infected file from disk."""
    try:
        fp = Path(file_path)
        if fp.exists():
            if fp.is_file():
                fp.unlink()
            elif fp.is_dir():
                shutil.rmtree(fp)
            log.info("Infected file deleted: %s", file_path)
            return True
    except Exception as e:
        log.error("Failed to delete infected file %s: %s", file_path, e)
    return False
