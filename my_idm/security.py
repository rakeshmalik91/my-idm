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


# Common threat categories that users may want to exclude
KNOWN_THREAT_CATEGORIES = [
    "HackTool",
    "CrackTool",
    "PUA",
    "Adware",
    "Riskware",
]


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

    # Scan timing: "after_complete" (auto-scan on completion) or "manual_only"
    scan_timing: str = "after_complete"

    # Threat exclusions: categories and custom patterns to silently allow.
    # None means "never configured" and falls back to KNOWN_THREAT_CATEGORIES. An empty
    # list means the user explicitly unticked every category, i.e. exclude nothing - the two
    # must stay distinguishable or the dialog cannot express "allow no threats".
    ignored_threat_categories: list = None  # e.g. ["HackTool", "CrackTool"]
    ignored_threat_patterns: str = ""       # comma-separated custom substrings

    def __post_init__(self):
        if isinstance(self.ignored_threat_categories, str):
            # Tolerate a hand-edited config that stored the bare string.
            self.ignored_threat_categories = [
                c.strip() for c in self.ignored_threat_categories.split(",") if c.strip()
            ]

    def get_effective_threat_exclusions(self) -> list[str]:
        """Categories to allow silently.

        Unset (``None``) falls back to the built-in defaults. Any list is honoured as
        written - including an empty one, which means "exclude nothing". Previously a
        falsy list was treated the same as unset, so a user who unticked every category
        silently got the five defaults back and the dialog could not express the choice.
        """
        if self.ignored_threat_categories is None:
            return list(KNOWN_THREAT_CATEGORIES)
        return [c for c in self.ignored_threat_categories if c and c.strip()]

    def to_dict(self) -> dict:
        data = asdict(self)
        # Emit the effective categories so the dict round-trips exactly: a config saved
        # and reloaded compares equal to the one that was saved.
        data["ignored_threat_categories"] = self.get_effective_threat_exclusions()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> SecurityConfig:
        # Missing key -> defaults. Present but empty -> the user excluded nothing, which
        # has to stay distinguishable or the dialog cannot express "allow no threats".
        cats = data.get("ignored_threat_categories")
        if isinstance(cats, str):
            cats = [c.strip() for c in cats.split(",") if c.strip()]
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
            scan_timing=str(data.get("scan_timing", "after_complete")),
            ignored_threat_categories=(
                list(cats) if isinstance(cats, list) else None
            ),
            ignored_threat_patterns=str(data.get("ignored_threat_patterns", "")),
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
        settings.setValue("scan_timing", self.scan_timing)
        settings.setValue("ignored_threat_categories", ",".join(self.get_effective_threat_exclusions()))
        settings.setValue("ignored_threat_patterns", self.ignored_threat_patterns)
        settings.endGroup()

    @classmethod
    def load(cls, settings: Optional[QSettings] = None) -> SecurityConfig:
        if settings is None:
            settings = QSettings("MyIDM", "My-IDM")
        settings.beginGroup("Security")
        val = settings.value("ignored_threat_categories", None)
        if val is None:
            # Never written: fall back to the defaults.
            cats = None
        else:
            # A present-but-empty value means the user explicitly excluded nothing, which
            # has to survive the round trip or un-ticking every box is a no-op.
            cats = [c.strip() for c in str(val).split(",") if c.strip()]
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
            scan_timing=str(settings.value("scan_timing", "after_complete") or "after_complete"),
            ignored_threat_categories=cats,
            ignored_threat_patterns=str(settings.value("ignored_threat_patterns", "") or ""),
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
            # Collect every build first and pick the highest version. os.walk yields
            # directory entries in filesystem order and the loop returned on the first hit,
            # so which Defender build got used depended on NTFS enumeration order - an old
            # build could win over a newer one.
            found = []
            for root, _, files in os.walk(platform_dir):
                if "MpCmdRun.exe" in files:
                    full_path = os.path.join(root, "MpCmdRun.exe")
                    if os.path.isfile(full_path):
                        found.append(full_path)
            if found:
                def _version(path: str):
                    # ...\Platform\<version>\MpCmdRun.exe
                    name = os.path.basename(os.path.dirname(path))
                    parts = []
                    for chunk in name.split("."):
                        if chunk.isdigit():
                            parts.append(int(chunk))
                        else:
                            break
                    return tuple(parts) or (0,)

                found.sort(key=_version, reverse=True)
                return found[0]
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


def _is_threat_excluded(report: str, config: SecurityConfig) -> Optional[str]:
    """Check whether a threat report matches any exclusion category or pattern.

    Returns the matched exclusion string if excluded, or None if not excluded.
    """
    report_lower = report.lower()

    # Check categories / patterns from exclusion list
    for cat in config.get_effective_threat_exclusions():
        if cat.lower() in report_lower:
            return cat

    # Check custom comma-separated patterns
    if config.ignored_threat_patterns:
        patterns = config.ignored_threat_patterns
        if isinstance(patterns, str):
            patterns = [p.strip() for p in patterns.split(",") if p.strip()]
        for pattern in patterns:
            if pattern and pattern.lower() in report_lower:
                return pattern

    return None


def scan_file(file_path: str, config: SecurityConfig) -> tuple[bool, str]:
    """Scan a downloaded file with the configured antivirus scanner.

    Returns (is_clean, report_message).
    Threats matching excluded categories/patterns are treated as clean.
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
                threat_report = f"Threat detected or scanner alert (Exit code {res.returncode}): {out[:200]}"
                excluded = _is_threat_excluded(threat_report, config)
                if excluded:
                    return True, f"Allowed (matched exclusion '{excluded}'): {threat_report}"
                return False, threat_report
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
            threat_report = f"⚠️ Threat detected by Windows Defender!\n{out}"
            excluded = _is_threat_excluded(threat_report, config)
            if excluded:
                return True, f"Allowed (matched exclusion '{excluded}'): {threat_report}"
            return False, threat_report
        else:
            if "found no threats" in out.lower():
                return True, "Clean (Windows Defender: no threats found)"
            if "threat" in out.lower():
                threat_report = f"⚠️ Threat detected by Windows Defender: {out}"
                excluded = _is_threat_excluded(threat_report, config)
                if excluded:
                    return True, f"Allowed (matched exclusion '{excluded}'): {threat_report}"
                return False, threat_report
            return True, f"Windows Defender completed with code {res.returncode}."
    except subprocess.TimeoutExpired:
        log.warning("Windows Defender scan timed out on %s", abs_path)
        return True, "Windows Defender scan timed out."
    except Exception as exc:
        log.error("Windows Defender execution failed: %s", exc)
        return True, f"Scanner error: {exc}"


def quarantine_or_delete_file(file_path: str) -> bool:
    """Delete an infected file from disk.

    Refuses blank and current-directory paths. ``Path("")`` is ``Path(".")``, so an empty
    ``file_path`` - an unresolved download row, a failed path probe returning "" - would
    otherwise ``shutil.rmtree`` the process's working directory, recursively, with no
    prompt. On Windows the CWD is wherever the app was launched from.
    """
    try:
        if not file_path or not str(file_path).strip():
            log.error("Refusing to quarantine a blank path")
            return False
        fp = Path(file_path)
        # Guard the resolved target too: ".", "./" and "sub/.." all name the CWD.
        try:
            resolved = fp.resolve()
        except OSError:
            resolved = fp.absolute()
        cwd = Path.cwd().resolve()
        if resolved == cwd or resolved == cwd.parent:
            log.error("Refusing to quarantine the working directory: %s", file_path)
            return False
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
